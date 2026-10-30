"""SQLite storage for received mail and delivery events.

The tables live in the same sidecar as the household items so one file (and
one volume) holds everything the agent knows. They are created on demand
with `CREATE TABLE IF NOT EXISTS`, which keeps the migration story to "open
the database".

The stored body is **redacted**: names, addresses, account numbers and phone
numbers are replaced with placeholders before the row is written. The full
text exists only in memory for the length of one webhook. That is the same
boundary the rest of the product draws — the household store is durable, and
it should not become an archive of raw correspondence.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_message (
    message_id           TEXT PRIMARY KEY,
    direction            TEXT NOT NULL DEFAULT 'inbound',
    internet_message_id  TEXT,
    sender               TEXT,
    recipients           TEXT NOT NULL DEFAULT '[]',
    subject              TEXT,
    body                 TEXT,
    body_redacted        INTEGER NOT NULL DEFAULT 1,
    received_at          TEXT,
    state                TEXT NOT NULL DEFAULT 'received',
    item_id              TEXT,
    metadata             TEXT NOT NULL DEFAULT '{}',
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_mail_received
    ON mail_message (received_at DESC);

CREATE INDEX IF NOT EXISTS idx_mail_state
    ON mail_message (state);

CREATE TABLE IF NOT EXISTS mail_event (
    event_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_event_id  TEXT UNIQUE,
    message_id         TEXT,
    event              TEXT NOT NULL,
    detail             TEXT,
    occurred_at        TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_mail_event_message
    ON mail_event (message_id, created_at);

CREATE TABLE IF NOT EXISTS mail_attachment (
    attachment_id  TEXT PRIMARY KEY,
    message_id     TEXT NOT NULL,
    filename       TEXT,
    content_type   TEXT,
    size           INTEGER,
    item_id        TEXT,
    state          TEXT NOT NULL DEFAULT 'seen',
    detail         TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


class MailStore:
    """The mail tables, on a connection the caller owns."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    # --- writes ------------------------------------------------------------

    def record_message(
        self,
        *,
        message_id: str,
        direction: str = "inbound",
        internet_message_id: str | None = None,
        sender: str | None = None,
        recipients: list[str] | None = None,
        subject: str | None = None,
        body: str | None = None,
        body_redacted: bool = True,
        received_at: str | None = None,
        state: str = "received",
        item_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Insert a message once. Returns True when this call created it."""
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO mail_message (
                message_id, direction, internet_message_id, sender, recipients,
                subject, body, body_redacted, received_at, state, item_id, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                direction,
                internet_message_id,
                sender,
                _json(recipients or []),
                subject,
                body,
                int(body_redacted),
                received_at,
                state,
                item_id,
                _json(metadata or {}),
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def update_message(
        self,
        message_id: str,
        *,
        state: str | None = None,
        item_id: str | None = None,
        body: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        fields: list[str] = []
        values: list[Any] = []
        if state is not None:
            fields.append("state = ?")
            values.append(state)
        if item_id is not None:
            fields.append("item_id = ?")
            values.append(item_id)
        if body is not None:
            fields.append("body = ?")
            values.append(body)
        if metadata is not None:
            fields.append("metadata = ?")
            values.append(_json(metadata))
        if not fields:
            return
        values.append(message_id)
        self.connection.execute(
            f"UPDATE mail_message SET {', '.join(fields)} WHERE message_id = ?",
            values,
        )
        self.connection.commit()

    def add_event(
        self,
        *,
        event: str,
        message_id: str | None = None,
        detail: str | None = None,
        occurred_at: str | None = None,
        provider_event_id: str | None = None,
    ) -> bool:
        """Record one provider event once. Returns True when it is new."""
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO mail_event (
                provider_event_id, message_id, event, detail, occurred_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (provider_event_id, message_id, event, detail, occurred_at),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def record_attachment(
        self,
        *,
        attachment_id: str,
        message_id: str,
        filename: str | None = None,
        content_type: str | None = None,
        size: int | None = None,
        item_id: str | None = None,
        state: str = "seen",
        detail: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO mail_attachment (
                attachment_id, message_id, filename, content_type, size, item_id, state, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (attachment_id) DO UPDATE SET
                item_id = excluded.item_id,
                state = excluded.state,
                detail = excluded.detail
            """,
            (attachment_id, message_id, filename, content_type, size, item_id, state, detail),
        )
        self.connection.commit()

    # --- reads -------------------------------------------------------------

    def exists(self, message_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM mail_message WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM mail_message WHERE message_id = ?", (message_id,)
        ).fetchone()
        return self._message(row) if row is not None else None

    def list_messages(self, limit: int = 20, *, direction: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        if direction:
            rows = self.connection.execute(
                """
                SELECT * FROM mail_message
                WHERE direction = ?
                ORDER BY COALESCE(received_at, created_at) DESC
                LIMIT ?
                """,
                (direction, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM mail_message
                ORDER BY COALESCE(received_at, created_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._message(row) for row in rows]

    def events_for(self, message_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT provider_event_id, event, detail, occurred_at, created_at
            FROM mail_event WHERE message_id = ?
            ORDER BY created_at ASC
            """,
            (message_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def attachments_for(self, message_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT attachment_id, filename, content_type, size, item_id, state, detail, created_at
            FROM mail_attachment WHERE message_id = ?
            ORDER BY created_at ASC
            """,
            (message_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        totals = self.connection.execute(
            "SELECT COUNT(*) FROM mail_message WHERE direction = 'inbound'"
        ).fetchone()[0]
        states = self.connection.execute(
            "SELECT state, COUNT(*) FROM mail_message GROUP BY state"
        ).fetchall()
        events = self.connection.execute("SELECT COUNT(*) FROM mail_event").fetchone()[0]
        return {
            "messages": int(totals),
            "events": int(events),
            "by_state": {row[0]: int(row[1]) for row in states},
        }

    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["recipients"] = _loads(data.get("recipients"), [])
        data["metadata"] = _loads(data.get("metadata"), {})
        data["body_redacted"] = bool(data.get("body_redacted"))
        return data
