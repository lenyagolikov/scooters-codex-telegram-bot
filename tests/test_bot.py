from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from scooters_codex_telegram_bot.approvals import is_safe_read_only_approval
from scooters_codex_telegram_bot.bot import (
    TELEGRAM_CLIENT_INSTRUCTIONS,
    ActiveTurn,
    TelegramCodexBot,
)
from scooters_codex_telegram_bot.config import Config
from scooters_codex_telegram_bot.state import OutboxMessage
from scooters_codex_telegram_bot.telegram_api import TelegramError

CHAT_ID = 101
USER_ID = 202
THREAD_ID = "thread-1"
TURN_ID = "turn-1"


class FakeTelegram:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.fail_send = fail_send
        self.sent: list[tuple[int, str, dict | None]] = []
        self.formatted: list[bool] = []
        self.cleared: list[tuple[int, int]] = []
        self.callback_answers: list[tuple[str, str]] = []
        self.downloads: list[tuple[str, Path, int]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        formatted: bool = False,
    ) -> int:
        if self.fail_send:
            raise TelegramError("simulated network failure")
        self.sent.append((chat_id, text, reply_markup))
        self.formatted.append(formatted)
        return len(self.sent)

    async def clear_inline_keyboard(self, chat_id: int, message_id: int) -> None:
        self.cleared.append((chat_id, message_id))

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        self.callback_answers.append((callback_id, text))

    async def send_typing(self, chat_id: int) -> None:
        return None

    async def download_file(
        self,
        file_id: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> None:
        self.downloads.append((file_id, destination, max_bytes))
        destination.write_bytes(b"fake telegram voice")


class FakeVoiceTranscriber:
    def __init__(self, text: str = "Сделай ревью PR") -> None:
        self.text = text
        self.paths: list[Path] = []

    async def transcribe(self, audio_path: Path) -> str:
        self.paths.append(audio_path)
        return self.text


class FakeAppServer:
    def __init__(self) -> None:
        self.notification_handler = None
        self.server_request_handler = None
        self.is_healthy = True
        self.responses: dict[str, dict] = {}
        self.requests: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        return self.responses[method]


class FakeState:
    def __init__(self) -> None:
        self.outbox: list[OutboxMessage] = []
        self.next_outbox_id = 1

    def get_chat_id(self, thread_id: str) -> int | None:
        return CHAT_ID if thread_id == THREAD_ID else None

    def get_thread_id(self, chat_id: int) -> str | None:
        return THREAD_ID if chat_id == CHAT_ID else None

    def enqueue_outbox(self, chat_id: int, text: str, *, formatted: bool) -> int:
        message_id = self.next_outbox_id
        self.next_outbox_id += 1
        self.outbox.append(
            OutboxMessage(message_id, chat_id, text, formatted, attempts=0)
        )
        return message_id

    def get_outbox(self, limit: int = 20) -> list[OutboxMessage]:
        return self.outbox[:limit]

    def mark_outbox_attempt(self, message_id: int) -> None:
        self.outbox = [
            OutboxMessage(
                message.id,
                message.chat_id,
                message.text,
                message.formatted,
                message.attempts + 1,
            )
            if message.id == message_id
            else message
            for message in self.outbox
        ]

    def delete_outbox(self, message_id: int) -> None:
        self.outbox = [message for message in self.outbox if message.id != message_id]


def make_bot(
    telegram: FakeTelegram | None = None,
    app_server: FakeAppServer | None = None,
    state: FakeState | None = None,
    voice_transcriber: FakeVoiceTranscriber | None = None,
    *,
    voice_enabled: bool = False,
    auto_approve: bool = False,
) -> TelegramCodexBot:
    config = Config(
        telegram_token="not-a-real-token",
        allowed_user_ids=frozenset({USER_ID}),
        codex_cwd=Path("/tmp"),
        codex_bin="codex",
        codex_model=None,
        reasoning_effort=None,
        state_path=Path("/tmp/not-used.sqlite3"),
        voice_transcription_enabled=voice_enabled,
        auto_approve_safe_read_only=auto_approve,
        auto_approve_read_roots=(Path("/tmp"),),
    )
    return TelegramCodexBot(
        config,
        telegram or FakeTelegram(),  # type: ignore[arg-type]
        app_server or FakeAppServer(),  # type: ignore[arg-type]
        state or FakeState(),  # type: ignore[arg-type]
        voice_transcriber,  # type: ignore[arg-type]
    )


class TelegramCodexBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_message_is_transcribed_and_submitted_as_prompt(self) -> None:
        telegram = FakeTelegram()
        transcriber = FakeVoiceTranscriber("Проверь изменения в scooters-core")
        app_server = FakeAppServer()
        app_server.responses.update(
            {
                "thread/resume": {
                    "thread": {
                        "id": THREAD_ID,
                        "name": "[Telegram] Review",
                        "status": {"type": "idle"},
                    }
                },
                "turn/start": {"turn": {"id": TURN_ID}},
            }
        )
        bot = make_bot(
            telegram=telegram,
            app_server=app_server,
            voice_transcriber=transcriber,
            voice_enabled=True,
        )

        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 404,
                    "voice": {
                        "file_id": "voice-file-1",
                        "duration": 12,
                        "file_size": 1024,
                    },
                }
            }
        )

        self.assertEqual(telegram.downloads[0][0], "voice-file-1")
        self.assertEqual(len(transcriber.paths), 1)
        self.assertFalse(transcriber.paths[0].exists())
        turn_start = next(
            params for method, params in app_server.requests if method == "turn/start"
        )
        self.assertEqual(
            turn_start["input"],
            [{"type": "text", "text": "Проверь изменения в scooters-core"}],
        )

    async def test_voice_message_over_duration_limit_is_rejected(self) -> None:
        telegram = FakeTelegram()
        transcriber = FakeVoiceTranscriber()
        bot = make_bot(
            telegram=telegram,
            voice_transcriber=transcriber,
            voice_enabled=True,
        )

        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 405,
                    "voice": {
                        "file_id": "voice-file-2",
                        "duration": 601,
                        "file_size": 1024,
                    },
                }
            }
        )

        self.assertEqual(telegram.downloads, [])
        self.assertEqual(transcriber.paths, [])
        self.assertIn("слишком длинное", telegram.sent[-1][1])

    async def test_safe_read_only_command_is_auto_approved(self) -> None:
        telegram = FakeTelegram()
        bot = make_bot(telegram=telegram, auto_approve=True)

        result = await bot._handle_codex_request(
            "item/commandExecution/requestApproval",
            {
                "threadId": THREAD_ID,
                "itemId": "command-1",
                "command": "sed -n '1,80p' src/main.py",
                "cwd": "/tmp/project",
                "commandActions": [{"type": "read", "path": "src/main.py"}],
                "availableDecisions": ["accept", "decline"],
            },
        )

        self.assertEqual(result, {"decision": "accept"})
        self.assertEqual(telegram.sent, [])

    def test_read_only_auto_approval_rejects_unsafe_requests(self) -> None:
        roots = (Path("/tmp/project"),)
        base = {
            "command": "sed -n '1,80p' src/main.py",
            "cwd": "/tmp/project",
            "commandActions": [{"type": "read", "path": "src/main.py"}],
            "availableDecisions": ["accept", "decline"],
        }
        cases = [
            {**base, "commandActions": [{"type": "unknown"}]},
            {**base, "networkApprovalContext": {"host": "example.test"}},
            {
                **base,
                "command": "cat .env",
                "commandActions": [{"type": "read", "path": ".env"}],
            },
            {
                **base,
                "commandActions": [{"type": "read", "path": "/etc/hosts"}],
            },
            {
                **base,
                "additionalPermissions": {
                    "fileSystem": {"write": ["/tmp/project"]}
                },
            },
        ]

        for params in cases:
            with self.subTest(params=params):
                self.assertFalse(is_safe_read_only_approval(params, roots))

    async def test_submit_prompt_does_not_send_started_acknowledgement(self) -> None:
        telegram = FakeTelegram()
        app_server = FakeAppServer()
        app_server.responses.update(
            {
                "thread/resume": {
                    "thread": {
                        "id": THREAD_ID,
                        "name": "[Telegram] Review",
                        "status": {"type": "idle"},
                    }
                },
                "turn/start": {"turn": {"id": TURN_ID}},
            }
        )
        bot = make_bot(telegram=telegram, app_server=app_server)

        await bot._submit_prompt(CHAT_ID, USER_ID, 303, "Review this change")

        self.assertEqual(telegram.sent, [])

    async def test_does_not_send_commentary_progress(self) -> None:
        telegram = FakeTelegram()
        bot = make_bot(telegram=telegram)
        active = ActiveTurn(CHAT_ID, THREAD_ID, TURN_ID)
        bot._register_active(active)

        await bot._handle_codex_notification(
            "item/completed",
            {
                "threadId": THREAD_ID,
                "turnId": TURN_ID,
                "item": {
                    "id": "message-1",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "Проверяю изменения и связанные вызовы.",
                },
            },
        )
        self.assertEqual(telegram.sent, [])
        self.assertIn("message-1", active.progress_item_ids)

    async def test_turn_completion_sends_only_formatted_final_answer(self) -> None:
        telegram = FakeTelegram()
        state = FakeState()
        bot = make_bot(telegram=telegram, state=state)
        bot._register_active(ActiveTurn(CHAT_ID, THREAD_ID, TURN_ID))

        await bot._handle_codex_notification(
            "turn/completed",
            {
                "threadId": THREAD_ID,
                "turn": {
                    "id": TURN_ID,
                    "status": "completed",
                    "items": [
                        {
                            "id": "commentary-1",
                            "type": "agentMessage",
                            "phase": "commentary",
                            "text": "Проверяю изменения.",
                        },
                        {
                            "id": "final-1",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "**Готово.** Используй `result`.",
                        },
                    ],
                },
            },
        )

        self.assertEqual(telegram.sent, [])
        self.assertEqual(len(state.outbox), 1)
        self.assertEqual(state.outbox[0].text, "**Готово.** Используй `result`.")

        self.assertTrue(await bot._deliver_outbox_message(state.outbox[0]))

        self.assertEqual(
            telegram.sent,
            [(CHAT_ID, "**Готово.** Используй `result`.", None)],
        )
        self.assertEqual(telegram.formatted, [True])
        self.assertEqual(state.outbox, [])

    async def test_failed_final_delivery_remains_in_outbox_for_retry(self) -> None:
        telegram = FakeTelegram(fail_send=True)
        state = FakeState()
        bot = make_bot(telegram=telegram, state=state)
        message_id = state.enqueue_outbox(CHAT_ID, "Готово", formatted=True)

        self.assertFalse(await bot._deliver_outbox_message(state.outbox[0]))

        self.assertEqual(len(state.outbox), 1)
        self.assertEqual(state.outbox[0].id, message_id)
        self.assertEqual(state.outbox[0].attempts, 1)

    async def test_adds_telegram_prefix_to_thread_title(self) -> None:
        app_server = FakeAppServer()
        app_server.responses["thread/name/set"] = {}
        bot = make_bot(app_server=app_server)

        await bot._ensure_telegram_title(
            THREAD_ID,
            "Провести ревью PR",
            "Этот текст не должен заменить существующее название",
        )

        self.assertEqual(app_server.requests[-1][0], "thread/name/set")
        self.assertEqual(
            app_server.requests[-1][1]["name"],
            "[Telegram] Провести ревью PR",
        )

    async def test_failed_approval_delivery_returns_decline(self) -> None:
        bot = make_bot(telegram=FakeTelegram(fail_send=True))

        result = await bot._handle_codex_request(
            "item/commandExecution/requestApproval",
            {"threadId": THREAD_ID, "command": "true", "reason": "read-only"},
        )

        self.assertEqual(result, {"decision": "decline"})
        self.assertEqual(bot._pending_callbacks, {})

    async def test_thread_params_disable_programmatic_exec_via_instructions(self) -> None:
        bot = make_bot()

        start_params = bot._start_thread_params()
        resume_params = bot._resume_thread_params(THREAD_ID)

        self.assertEqual(
            start_params["developerInstructions"], TELEGRAM_CLIENT_INSTRUCTIONS
        )
        self.assertEqual(
            resume_params["developerInstructions"], TELEGRAM_CLIENT_INSTRUCTIONS
        )
        self.assertIn("Never call those tools", TELEGRAM_CLIENT_INSTRUCTIONS)

    async def test_dynamic_tool_call_returns_retryable_failure(self) -> None:
        bot = make_bot()

        result = await bot._handle_codex_request(
            "item/tool/call",
            {
                "threadId": THREAD_ID,
                "turnId": TURN_ID,
                "callId": "call-1",
                "tool": "exec",
                "arguments": "return tools.exec_command({cmd: 'true'})",
            },
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["contentItems"][0]["type"], "inputText")
        self.assertIn("built-in", result["contentItems"][0]["text"])

    async def test_mcp_url_elicitation_accepts_callback(self) -> None:
        telegram = FakeTelegram()
        bot = make_bot(telegram=telegram)
        task = asyncio.create_task(
            bot._handle_codex_request(
                "mcpServer/elicitation/request",
                {
                    "threadId": THREAD_ID,
                    "serverName": "browser",
                    "mode": "url",
                    "message": "Open authorization page",
                    "elicitationId": "e-1",
                    "url": "https://example.test/auth",
                },
            )
        )
        await asyncio.sleep(0)
        token = next(iter(bot._pending_callbacks))

        await bot._handle_callback(
            {
                "id": "callback-1",
                "from": {"id": USER_ID},
                "message": {"message_id": 1, "chat": {"id": CHAT_ID}},
                "data": f"cb:{token}:accept",
            }
        )

        self.assertEqual(await task, {"action": "accept"})
        self.assertEqual(telegram.cleared, [(CHAT_ID, 1)])

    async def test_mcp_form_collects_text_field_without_user_json(self) -> None:
        telegram = FakeTelegram()
        bot = make_bot(telegram=telegram)
        task = asyncio.create_task(
            bot._handle_codex_request(
                "mcpServer/elicitation/request",
                {
                    "threadId": THREAD_ID,
                    "serverName": "intrasearch",
                    "mode": "form",
                    "message": "Оцени источник",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "comment": {
                                "type": "string",
                                "title": "Комментарий",
                            }
                        },
                        "required": ["comment"],
                    },
                },
            )
        )
        await asyncio.sleep(0)

        self.assertIn(CHAT_ID, bot._pending_text_by_chat)
        self.assertNotIn(
            "Ответь одним JSON-объектом",
            "\n".join(message for _, message, _ in telegram.sent),
        )
        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 10,
                    "text": "Источник полезен",
                }
            }
        )

        self.assertEqual(
            await task,
            {
                "action": "accept",
                "content": {"comment": "Источник полезен"},
            },
        )

    async def test_mcp_form_uses_buttons_for_enum(self) -> None:
        telegram = FakeTelegram()
        bot = make_bot(telegram=telegram)
        task = asyncio.create_task(
            bot._handle_codex_request(
                "mcpServer/elicitation/request",
                {
                    "threadId": THREAD_ID,
                    "serverName": "intrasearch",
                    "mode": "form",
                    "message": "Выбери оценку",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "rating": {
                                "type": "string",
                                "title": "Релевантность",
                                "enum": ["irrelevant", "relevant", "vital"],
                            }
                        },
                        "required": ["rating"],
                    },
                },
            )
        )
        await asyncio.sleep(0)
        token = next(iter(bot._pending_callbacks))

        await bot._handle_callback(
            {
                "id": "callback-form-enum",
                "from": {"id": USER_ID},
                "message": {"message_id": 2, "chat": {"id": CHAT_ID}},
                "data": f"cb:{token}:1",
            }
        )

        self.assertEqual(
            await task,
            {"action": "accept", "content": {"rating": "relevant"}},
        )

    async def test_cancel_mcp_form_does_not_interrupt_codex_turn(self) -> None:
        telegram = FakeTelegram()
        app_server = FakeAppServer()
        bot = make_bot(telegram=telegram, app_server=app_server)
        bot._register_active(ActiveTurn(CHAT_ID, THREAD_ID, TURN_ID))
        task = asyncio.create_task(
            bot._handle_codex_request(
                "mcpServer/elicitation/request",
                {
                    "threadId": THREAD_ID,
                    "serverName": "intrasearch",
                    "mode": "form",
                    "message": "Нужен комментарий",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"comment": {"type": "string"}},
                        "required": ["comment"],
                    },
                },
            )
        )
        await asyncio.sleep(0)

        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 11,
                    "text": "/cancel",
                }
            }
        )

        self.assertEqual(await task, {"action": "cancel"})
        self.assertFalse(
            any(method == "turn/interrupt" for method, _ in app_server.requests)
        )
        self.assertIn(CHAT_ID, bot._active_by_chat)

    async def test_optional_mcp_form_field_can_be_skipped(self) -> None:
        telegram = FakeTelegram()
        bot = make_bot(telegram=telegram)
        task = asyncio.create_task(
            bot._handle_codex_request(
                "mcpServer/elicitation/request",
                {
                    "threadId": THREAD_ID,
                    "serverName": "intrasearch",
                    "mode": "form",
                    "message": "Необязательное пояснение",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"comment": {"type": "string"}},
                    },
                },
            )
        )
        await asyncio.sleep(0)

        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 12,
                    "text": "/skip",
                }
            }
        )

        self.assertEqual(await task, {"action": "accept", "content": {}})

    async def test_interaction_timeout_is_queued_for_reliable_delivery(self) -> None:
        telegram = FakeTelegram()
        state = FakeState()
        bot = make_bot(telegram=telegram, state=state)

        result = await bot._ask_with_buttons(
            CHAT_ID,
            "Подтверди действие",
            kind="approval:command",
            keyboard={"inline_keyboard": []},
            timeout=0.001,
            timeout_message="Ожидание подтверждения истекло.",
        )

        self.assertIsNone(result)
        self.assertEqual(telegram.cleared, [(CHAT_ID, 1)])
        self.assertEqual(len(state.outbox), 1)
        self.assertEqual(state.outbox[0].text, "Ожидание подтверждения истекло.")
        self.assertFalse(state.outbox[0].formatted)

    async def test_request_user_input_returns_selected_label(self) -> None:
        bot = make_bot()
        task = asyncio.create_task(
            bot._handle_codex_request(
                "item/tool/requestUserInput",
                {
                    "threadId": THREAD_ID,
                    "turnId": TURN_ID,
                    "itemId": "item-1",
                    "questions": [
                        {
                            "id": "environment",
                            "header": "Среда",
                            "question": "Где продолжить?",
                            "options": [
                                {"label": "Локально", "description": "На Mac"},
                                {"label": "Codenv", "description": "На VM"},
                            ],
                        }
                    ],
                },
            )
        )
        await asyncio.sleep(0)
        token = next(iter(bot._pending_callbacks))

        await bot._handle_callback(
            {
                "id": "callback-2",
                "from": {"id": USER_ID},
                "message": {"message_id": 1, "chat": {"id": CHAT_ID}},
                "data": f"cb:{token}:1",
            }
        )

        self.assertEqual(
            await task,
            {"answers": {"environment": {"answers": ["Codenv"]}}},
        )

    async def test_status_reconciles_stale_active_turn(self) -> None:
        telegram = FakeTelegram()
        app_server = FakeAppServer()
        app_server.responses["thread/read"] = {
            "thread": {
                "id": THREAD_ID,
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": TURN_ID,
                        "status": "interrupted",
                        "items": [],
                        "durationMs": 1500,
                    }
                ],
            }
        }
        bot = make_bot(telegram=telegram, app_server=app_server)
        bot._register_active(ActiveTurn(CHAT_ID, THREAD_ID, TURN_ID))

        await bot._send_status(CHAT_ID)

        self.assertNotIn(CHAT_ID, bot._active_by_chat)
        self.assertIn("последняя задача прервана", telegram.sent[-1][1])
        self.assertIn("Проверка живого статуса: получено", telegram.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
