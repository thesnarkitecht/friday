"""Agent-to-agent mail. One SQLite file, so agents in different processes can talk."""

import sqlite3
import time

from .config import HOME


class Bus:
    def __init__(self, path=None):
        self.path = str(path or HOME / "bus.db")
        HOME.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS mail (id INTEGER PRIMARY KEY, ts REAL, sender TEXT,"
                " recipient TEXT, body TEXT, read INTEGER DEFAULT 0)"
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def send(self, sender: str, recipient: str, body: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO mail (ts, sender, recipient, body) VALUES (?, ?, ?, ?)",
                (time.time(), sender, recipient, body),
            )

    def inbox(self, name: str) -> list[tuple[str, str]]:
        """Unread (sender, body) pairs for `name`, marked read in the same transaction."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")  # two readers must not both take the same mail
            rows = db.execute(
                "SELECT id, sender, body FROM mail WHERE recipient = ? AND read = 0 ORDER BY id", (name,)
            ).fetchall()
            db.executemany("UPDATE mail SET read = 1 WHERE id = ?", [(r[0],) for r in rows])
        return [(r[1], r[2]) for r in rows]
