"""SQLite-backed conversation history and webhook idempotency."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);

CREATE TABLE IF NOT EXISTS processed (
    message_id  TEXT PRIMARY KEY,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_time ON processed(created_at);
"""


class ConversationStore:
    """Stores per-user chat history and tracks processed message IDs.

    Meta retries webhooks it believes failed, which would otherwise cause the
    bot to reply two or three times to the same message. ``mark_processed``
    returns ``False`` for a duplicate so the caller can drop the delivery.
    """

    def __init__(self, path: str | Path = "chatbot.db") -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def mark_processed(self, message_id: str) -> bool:
        """Record a webhook message ID. Returns False if it was already seen."""
        if not message_id:
            return True
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO processed (message_id, created_at) VALUES (?, ?)",
                    (message_id, time.time()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def add_message(self, user_id: str, role: str, content: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (user_id, role, content, time.time()),
            )
            self._conn.commit()

    def history(self, user_id: str, limit: int = 10) -> list[dict[str, str]]:
        """Return the last ``limit`` messages for a user, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content FROM messages
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (user_id, max(0, limit)),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def reset(self, user_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def prune_processed(self, older_than_seconds: float = 60 * 60 * 24 * 7) -> int:
        """Drop idempotency records older than the retention window."""
        cutoff = time.time() - older_than_seconds
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM processed WHERE created_at < ?", (cutoff,)
            )
            self._conn.commit()
            return cur.rowcount