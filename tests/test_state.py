from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scooters_codex_telegram_bot.state import StateStore


class StateStoreOutboxTests(unittest.TestCase):
    def test_outbox_survives_store_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = StateStore(path)
            message_id = state.enqueue_outbox(101, "**Готово**", formatted=True)
            state.close()

            reopened = StateStore(path)
            messages = reopened.get_outbox()
            reopened.close()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].id, message_id)
        self.assertEqual(messages[0].chat_id, 101)
        self.assertEqual(messages[0].text, "**Готово**")
        self.assertTrue(messages[0].formatted)
        self.assertEqual(messages[0].attempts, 0)

    def test_attempt_and_delete_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite3")
            message_id = state.enqueue_outbox(101, "Ответ", formatted=False)

            state.mark_outbox_attempt(message_id)
            self.assertEqual(state.get_outbox()[0].attempts, 1)

            state.delete_outbox(message_id)
            self.assertEqual(state.get_outbox(), [])
            state.close()


if __name__ == "__main__":
    unittest.main()
