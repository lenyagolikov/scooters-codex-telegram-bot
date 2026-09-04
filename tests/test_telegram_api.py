from __future__ import annotations

import unittest
from http.client import RemoteDisconnected
from unittest.mock import AsyncMock, Mock

from scooters_codex_telegram_bot.telegram_api import (
    TelegramApi,
    TelegramError,
    markdown_to_telegram_html,
)


class MarkdownToTelegramHtmlTests(unittest.TestCase):
    def test_renders_common_codex_markdown(self) -> None:
        markdown = (
            "## Итог\n\n"
            "**Важно:** используй `request_id`.\n\n"
            "- первый пункт\n"
            "- [документация](https://example.test/docs?a=1&b=2)"
        )

        self.assertEqual(
            markdown_to_telegram_html(markdown),
            '<b>Итог</b>\n\n'
            '<b>Важно:</b> используй <code>request_id</code>.\n\n'
            '• первый пункт\n'
            '• <a href="https://example.test/docs?a=1&amp;b=2">документация</a>',
        )

    def test_escapes_html_and_preserves_fenced_code(self) -> None:
        markdown = "```cpp\nif (a < b && b > 0) {\n    return;\n}\n```"

        self.assertEqual(
            markdown_to_telegram_html(markdown),
            '<pre><code class="language-cpp">'
            'if (a &lt; b &amp;&amp; b &gt; 0) {\n    return;\n}'
            '</code></pre>',
        )

    def test_does_not_create_unsafe_link(self) -> None:
        self.assertEqual(
            markdown_to_telegram_html("[файл](codex://review?id=1)"),
            "файл (codex://review?id=1)",
        )


class TelegramApiTests(unittest.IsolatedAsyncioTestCase):
    def test_remote_disconnect_is_reported_as_retryable_telegram_error(self) -> None:
        api = TelegramApi("not-a-real-token")
        api._open = Mock(  # type: ignore[method-assign]
            side_effect=RemoteDisconnected("peer closed connection")
        )

        with self.assertRaisesRegex(TelegramError, "network error"):
            api._call_sync("getUpdates", {}, 45)

    async def test_formatted_message_uses_html_without_link_preview(self) -> None:
        api = TelegramApi("not-a-real-token")
        api.call = AsyncMock(return_value={"message_id": 42})  # type: ignore[method-assign]

        message_id = await api.send_message(
            101,
            "**Готово.** [Подробности](https://example.test)",
            formatted=True,
        )

        self.assertEqual(message_id, 42)
        api.call.assert_awaited_once_with(
            "sendMessage",
            {
                "chat_id": 101,
                "text": '<b>Готово.</b> <a href="https://example.test">Подробности</a>',
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )


if __name__ == "__main__":
    unittest.main()
