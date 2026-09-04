from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from . import __version__

LOGGER = logging.getLogger(__name__)
APP_SERVER_STREAM_LIMIT = 16 * 1024 * 1024
APP_SERVER_DISABLED_FEATURES = (
    "code_mode",
    "code_mode_buffered_exec",
    "code_mode_only",
)
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class AppServerError(RuntimeError):
    pass


class CodexAppServer:
    def __init__(self, codex_bin: str) -> None:
        self._codex_bin = codex_bin
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_request_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self.notification_handler: NotificationHandler | None = None
        self.server_request_handler: ServerRequestHandler | None = None

    async def start(self) -> None:
        feature_args = [
            argument
            for feature in APP_SERVER_DISABLED_FEATURES
            for argument in ("--disable", feature)
        ]
        self._process = await asyncio.create_subprocess_exec(
            self._codex_bin,
            "app-server",
            *feature_args,
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=APP_SERVER_STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_loop(), name="codex-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="codex-stderr")
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "scooters-codex-telegram-bot",
                    "title": "Scooters Codex Telegram Bot",
                    "version": __version__,
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized", {})

    async def stop(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self._fail_pending(AppServerError("Codex app-server stopped"))

    @property
    def is_healthy(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    async def wait_until_stopped(self) -> None:
        if self._reader_task is None:
            raise AppServerError("Codex app-server reader is not running")
        try:
            await asyncio.shield(self._reader_task)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise AppServerError("Codex app-server reader stopped") from error
        raise AppServerError("Codex app-server reader stopped unexpectedly")

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write({"id": request_id, "method": method, "params": params})
        return await future

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def _write(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise AppServerError("Codex app-server is not running")
        data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        async with self._write_lock:
            self._process.stdin.write(data)
            await self._process.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    LOGGER.warning("Ignored non-JSON app-server output")
                    continue

                if "method" in message and "id" in message:
                    asyncio.create_task(self._handle_server_request(message))
                elif "method" in message:
                    if self.notification_handler is not None:
                        try:
                            await self.notification_handler(
                                str(message["method"]), message.get("params") or {}
                            )
                        except Exception:
                            LOGGER.exception(
                                "Notification handler failed: %s", message.get("method")
                            )
                elif "id" in message:
                    self._handle_response(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.exception("Codex app-server reader failed")
            self._fail_pending(AppServerError(str(error)))
            raise
        finally:
            if self._process.returncode is not None:
                self._fail_pending(
                    AppServerError(
                        f"Codex app-server exited with code {self._process.returncode}"
                    )
                )

    def _handle_response(self, message: dict[str, Any]) -> None:
        future = self._pending.pop(message["id"], None)
        if future is None or future.done():
            return
        if "error" in message:
            error = message["error"]
            future.set_exception(
                AppServerError(str(error.get("message", "Unknown app-server error")))
            )
        else:
            future.set_result(message.get("result"))

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = message["id"]
        try:
            if self.server_request_handler is None:
                raise AppServerError(f"Unsupported server request: {message['method']}")
            result = await self.server_request_handler(
                str(message["method"]), message.get("params") or {}
            )
            await self._write({"id": request_id, "result": result})
        except Exception as error:
            LOGGER.exception("Server request failed: %s", message.get("method"))
            await self._write(
                {
                    "id": request_id,
                    "error": {"code": -32000, "message": str(error)},
                }
            )

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            while line := await self._process.stderr.readline():
                LOGGER.info("codex: %s", line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
