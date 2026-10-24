"""SQLite store for messages, the latest Briefs, and the phones to wake.

Lives in the Hermes home (``<home>/hermsy/hermsy.db``), so it moves and is
backed up with everything else Hermes keeps.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    text        TEXT NOT NULL,
    source      TEXT NOT NULL,
    job_name    TEXT,
    job_id      TEXT,
    created_at  TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_received ON messages (received_at);
CREATE TABLE IF NOT EXISTS briefs (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT NOT NULL UNIQUE,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    token         TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Message:
    seq: int
    id: str
    title: str
    text: str
    source: str
    job_name: str | None
    job_id: str | None
    created_at: str
    received_at: str

    def to_json(self) -> dict:
        return asdict(self)


class Store:
    """One connection behind a lock: a handful of phones and a few messages a
    day need nothing more."""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        # Names this database: the app keeps its place in the message list
        # per store, so a fresh one is read from the start.
        self.id = self.get("store_id") or self._create_id()

    def _create_id(self) -> str:
        store_id = str(uuid.uuid4())
        self.put("store_id", store_id)
        return store_id

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- settings ------------------------------------------------------------

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- messages ------------------------------------------------------------

    def add_message(
        self,
        *,
        text: str,
        title: str,
        source: str,
        job_name: str | None = None,
        job_id: str | None = None,
        created_at: str | None = None,
    ) -> Message:
        message_id = str(uuid.uuid4())
        received = now()
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO messages (id, title, text, source, job_name, job_id, created_at, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, title, text, source, job_name, job_id, created_at or received, received),
            )
            seq = cursor.lastrowid
        return Message(
            seq=seq,
            id=message_id,
            title=title,
            text=text,
            source=source,
            job_name=job_name,
            job_id=job_id,
            created_at=created_at or received,
            received_at=received,
        )

    def get_message(self, message_id: str) -> Message | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return Message(**dict(row)) if row else None

    def messages_since(self, seq: int, limit: int) -> list[Message]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit)
            ).fetchall()
        return [Message(**dict(row)) for row in rows]

    def prune(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            return self._db.execute("DELETE FROM messages WHERE received_at < ?", (cutoff,)).rowcount

    # -- briefs --------------------------------------------------------------

    def add_brief(self, body: dict) -> dict:
        brief_id = str(uuid.uuid4())
        created = now()
        with self._lock:
            self._db.execute(
                "INSERT INTO briefs (id, body, created_at) VALUES (?, ?, ?)",
                (brief_id, json.dumps(body), created),
            )
            # The latest few are plenty: the app shows only the newest.
            self._db.execute(
                "DELETE FROM briefs WHERE seq NOT IN (SELECT seq FROM briefs ORDER BY seq DESC LIMIT 30)"
            )
        return {"id": brief_id, "created_at": created, **body}

    def latest_brief(self) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM briefs ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {"id": row["id"], "created_at": row["created_at"], **json.loads(row["body"])}

    # -- devices -------------------------------------------------------------

    def upsert_device(self, token: str, platform: str) -> None:
        stamp = now()
        with self._lock:
            self._db.execute(
                "INSERT INTO devices (token, platform, registered_at, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(token) DO UPDATE SET platform = excluded.platform, updated_at = excluded.updated_at",
                (token, platform, stamp, stamp),
            )

    def remove_device(self, token: str) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM devices WHERE token = ?", (token,)).rowcount > 0

    def device_tokens(self) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT token FROM devices").fetchall()
        return [row["token"] for row in rows]
