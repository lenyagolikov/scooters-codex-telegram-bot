from __future__ import annotations

import asyncio
import json
import re
import socket
from html import escape
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPSHandler, Request, build_opener


class TelegramError(RuntimeError):
    pass


class _FamilyHTTPSConnection(HTTPSConnection):
    """HTTPS connection restricted to one address family."""

    address_family = socket.AF_UNSPEC

    def connect(self) -> None:
        addresses = socket.getaddrinfo(
            self.host,
            self.port,
            family=self.address_family,
            type=socket.SOCK_STREAM,
        )
        last_error: OSError | None = None
        raw_socket: socket.socket | None = None
        for family, socket_type, protocol, _, socket_address in addresses:
            candidate = socket.socket(family, socket_type, protocol)
            candidate.settimeout(min(float(self.timeout), 3.0))
            try:
                candidate.connect(socket_address)
                raw_socket = candidate
                break
            except OSError as error:
                last_error = error
                candidate.close()

        if raw_socket is None:
            if last_error is not None:
                raise last_error
            raise OSError("No IPv6 address available for Telegram API")

        server_hostname = self.host
        if self._tunnel_host:
            self.sock = raw_socket
            self._tunnel()
            server_hostname = self._tunnel_host
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=server_hostname)
        self.sock.settimeout(self.timeout)


class _IPv4HTTPSConnection(_FamilyHTTPSConnection):
    address_family = socket.AF_INET


class _IPv6HTTPSConnection(_FamilyHTTPSConnection):
    address_family = socket.AF_INET6


class _IPv4HTTPSHandler(HTTPSHandler):
    def https_open(self, request: Request) -> Any:
        return self.do_open(
            _IPv4HTTPSConnection,
            request,
            context=self._context,
        )


class _IPv6HTTPSHandler(HTTPSHandler):
    def https_open(self, request: Request) -> Any:
        return self.do_open(
            _IPv6HTTPSConnection,
            request,
            context=self._context,
        )


class TelegramApi:
    def __init__(self, token: str, ip_family: str = "auto") -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._file_base_url = f"https://api.telegram.org/file/bot{token}"
        self._ip_family = ip_family

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int = 45,
    ) -> Any:
        last_error: TelegramError | None = None
        for attempt in range(3):
            try:
                return await asyncio.to_thread(
                    self._call_sync, method, payload or {}, timeout
                )
            except TelegramError as error:
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(attempt + 1)
        assert last_error is not None
        raise last_error

    def _call_sync(self, method: str, payload: dict[str, Any], timeout: int) -> Any:
        request = Request(
            f"{self._base_url}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._open(request, timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise TelegramError(
                f"Telegram request {method} failed with HTTP {error.code}"
            ) from error
        except (HTTPException, OSError) as error:
            raise TelegramError(f"Telegram request {method} failed: network error") from error

        if not body.get("ok"):
            description = body.get("description", "unknown Telegram error")
            raise TelegramError(f"Telegram request {method} failed: {description}")
        return body.get("result")

    def _open(self, request: Request, timeout: int) -> Any:
        if self._ip_family == "ipv4":
            opener = build_opener(_IPv4HTTPSHandler())
        elif self._ip_family == "ipv6":
            opener = build_opener(_IPv6HTTPSHandler())
        else:
            opener = build_opener()
        return opener.open(request, timeout=timeout)

    async def get_updates(self, offset: int | None, poll_timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": poll_timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self.call("getUpdates", payload, timeout=poll_timeout + 15)
        return list(result)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        formatted: bool = False,
    ) -> int | None:
        chunks = split_message(text)
        last_message_id: int | None = None
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": markdown_to_telegram_html(chunk) if formatted else chunk,
            }
            if formatted:
                payload["parse_mode"] = "HTML"
                payload["link_preview_options"] = {"is_disabled": True}
            if index == len(chunks) - 1 and reply_markup is not None:
                payload["reply_markup"] = reply_markup
            result = await self.call("sendMessage", payload)
            if isinstance(result, dict) and result.get("message_id") is not None:
                last_message_id = int(result["message_id"])
        return last_message_id

    async def clear_inline_keyboard(self, chat_id: int, message_id: int) -> None:
        await self.call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
        )

    async def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        await self.call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    async def send_typing(self, chat_id: int) -> None:
        await self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    async def download_file(
        self,
        file_id: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> None:
        result = await self.call("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not result.get("file_path"):
            raise TelegramError("Telegram did not return a file path")
        await asyncio.to_thread(
            self._download_file_sync,
            str(result["file_path"]),
            destination,
            max_bytes,
        )

    def _download_file_sync(
        self,
        file_path: str,
        destination: Path,
        max_bytes: int,
    ) -> None:
        request = Request(f"{self._file_base_url}/{file_path}", method="GET")
        total_bytes = 0
        try:
            with self._open(request, 60) as response, destination.open("wb") as output:
                while chunk := response.read(64 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise TelegramError("Telegram voice message is too large")
                    output.write(chunk)
        except HTTPError as error:
            raise TelegramError(
                f"Telegram file download failed with HTTP {error.code}"
            ) from error
        except (HTTPException, OSError) as error:
            raise TelegramError("Telegram file download failed: network error") from error


def split_message(text: str, limit: int = 3900) -> list[str]:
    if not text:
        return ["(пустой ответ)"]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


_FENCED_CODE_RE = re.compile(
    r"```(?P<language>[A-Za-z0-9_+.-]*)[ \t]*\n(?P<code>.*?)```",
    re.DOTALL,
)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]+)\]\(([^)\s]+)\)")


def markdown_to_telegram_html(text: str) -> str:
    """Render common Codex Markdown as Telegram-safe HTML."""
    rendered_tokens: list[str] = []

    def protect(rendered: str) -> str:
        token = f"\x00TG{len(rendered_tokens)}\x00"
        rendered_tokens.append(rendered)
        return token

    def render_fenced_code(match: re.Match[str]) -> str:
        language = match.group("language")
        code = escape(match.group("code").rstrip("\n"))
        if language:
            safe_language = re.sub(r"[^A-Za-z0-9_+-]", "", language)
            return protect(
                f'<pre><code class="language-{safe_language}">{code}</code></pre>'
            )
        return protect(f"<pre>{code}</pre>")

    def render_inline_code(match: re.Match[str]) -> str:
        return protect(f"<code>{escape(match.group(1))}</code>")

    def render_link(match: re.Match[str]) -> str:
        label = escape(match.group(1))
        url = match.group(2)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return protect(f"{label} ({escape(url)})")
        return protect(f'<a href="{escape(url, quote=True)}">{label}</a>')

    protected = _FENCED_CODE_RE.sub(render_fenced_code, text)
    protected = _INLINE_CODE_RE.sub(render_inline_code, protected)
    protected = _LINK_RE.sub(render_link, protected)
    rendered = escape(protected)

    rendered = re.sub(
        r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$",
        r"<b>\1</b>",
        rendered,
    )
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", rendered, flags=re.DOTALL)
    rendered = re.sub(r"~~(.+?)~~", r"<s>\1</s>", rendered, flags=re.DOTALL)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", rendered)
    rendered = re.sub(r"(?m)^[ \t]*[-*][ \t]+", "• ", rendered)

    for index, token_value in enumerate(rendered_tokens):
        rendered = rendered.replace(f"\x00TG{index}\x00", token_value)
    return rendered
