from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: int
    chat_id: int
    text: str
    formatted: bool
    attempts: int


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                thread_id TEXT NOT NULL UNIQUE,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                formatted INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_attempt_at TEXT
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def get_thread_id(self, chat_id: int) -> str | None:
        row = self._connection.execute(
            "SELECT thread_id FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return str(row[0]) if row else None

    def get_chat_id(self, thread_id: str) -> int | None:
        row = self._connection.execute(
            "SELECT chat_id FROM chats WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return int(row[0]) if row else None

    def set_thread_id(self, chat_id: int, user_id: int, thread_id: str) -> None:
        self._connection.execute(
            """
            INSERT INTO chats (chat_id, user_id, thread_id)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                user_id = excluded.user_id,
                thread_id = excluded.thread_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (chat_id, user_id, thread_id),
        )
        self._connection.commit()

    def clear_thread(self, chat_id: int) -> None:
        self._connection.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        self._connection.commit()

    def get_update_offset(self) -> int | None:
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'telegram_update_offset'"
        ).fetchone()
        return int(row[0]) if row else None

    def set_update_offset(self, offset: int) -> None:
        self._connection.execute(
            """
            INSERT INTO metadata (key, value) VALUES ('telegram_update_offset', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(offset),),
        )
        self._connection.commit()

    def enqueue_outbox(self, chat_id: int, text: str, *, formatted: bool) -> int:
        cursor = self._connection.execute(
            "INSERT INTO outbox (chat_id, text, formatted) VALUES (?, ?, ?)",
            (chat_id, text, int(formatted)),
        )
        self._connection.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def get_outbox(self, limit: int = 20) -> list[OutboxMessage]:
        rows = self._connection.execute(
            """
            SELECT id, chat_id, text, formatted, attempts
            FROM outbox
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            OutboxMessage(
                id=int(row[0]),
                chat_id=int(row[1]),
                text=str(row[2]),
                formatted=bool(row[3]),
                attempts=int(row[4]),
            )
            for row in rows
        ]

    def mark_outbox_attempt(self, message_id: int) -> None:
        self._connection.execute(
            """
            UPDATE outbox
            SET attempts = attempts + 1, last_attempt_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (message_id,),
        )
        self._connection.commit()

    def delete_outbox(self, message_id: int) -> None:
        self._connection.execute("DELETE FROM outbox WHERE id = ?", (message_id,))
        self._connection.commit()
