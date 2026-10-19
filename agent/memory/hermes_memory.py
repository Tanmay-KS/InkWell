"""The household memory store.

Phase 1 — Person B.

Two layers:

* **Hermes memory** — the agent's own persistent memory. `MEMORY.md` holds
  the household picture; `USER.md` holds who the user is. Hermes handles
  the lifecycle: writing, recall across sessions, and the periodic nudges
  that keep the memory from going stale.
* **Structured sidecar** — a small queryable store for the facts that need
  to be looked up precisely: which account, which date, which state. Memory
  files are good for recall and bad for "every subscription that renews in
  the next 30 days".

The sidecar is intentionally small and boring: SQLite on the data volume.
It must never be a bind mount — see the friction log, F-008.

State machine for a tracked item:

    new ──▶ acknowledged ──▶ handled
              │                  │
              └──▶ dismissed ◀────┘

`dismissed` is the interesting one: the user said "I know, leave it alone",
and the agent must not bring it back.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path

# The Hermes data volume. HERMES_HOME is /opt/data inside the sandbox image.
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

# The structured sidecar lives beside the Hermes state, on the same volume.
SIDECAR_PATH = Path(os.environ.get("LEDGER_MEMORY_DB", HERMES_HOME / "ledger_memory.db"))


class TrackedState(StrEnum):
    """Lifecycle of a tracked obligation."""

    NEW = "new"
    ACKNOWLEDGED = "acknowledged"
    HANDLED = "handled"
    DISMISSED = "dismissed"


@dataclass(slots=True)
class HouseholdItem:
    """One thing the household is subscribed to, enrolled in, or owes.

    Attributes:
        item_id: Stable identifier. Derived from the source document, so
            the same document seen twice produces the same id.
        kind: What sort of thing it is — "insurance", "subscription",
            "membership", "licence", "medical".
        provider: Who it is with.
        holder: Whose it is. A household has more than one person and a
            renewal notice is usually about exactly one of them.
        reference: The account, policy, or member number. This is a
            personal detail and is treated as one everywhere it appears.
        renewal_date: The date it renews or lapses, if known.
        amount: The amount, if known.
        state: Where it is in the lifecycle.
        source: Where we learned about it — a file path, a message id.
        metadata: Everything else.
    """

    item_id: str
    kind: str
    provider: str
    holder: str | None = None
    reference: str | None = None
    renewal_date: date | None = None
    amount: float | None = None
    state: TrackedState = TrackedState.NEW
    source: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class HouseholdMemory:
    """The household picture plus the handled log.

    Phase 1 implementation: SQLite for the structured sidecar, Hermes's
    memory files for the narrative picture.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else SIDECAR_PATH
        self._conn: sqlite3.Connection | None = None

    # --- lifecycle ---------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Open (and if needed create) the sidecar database.

        The directory must exist and be writable by the runtime user. On the
        sandbox that means the named volume, owned by UID 10000.
        """
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        conn = self._conn
        assert conn is not None
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS household_item (
                item_id      TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,
                provider     TEXT NOT NULL,
                holder       TEXT,
                reference    TEXT,
                renewal_date TEXT,
                amount       REAL,
                state        TEXT NOT NULL DEFAULT 'new',
                source       TEXT,
                metadata     TEXT NOT NULL DEFAULT '{}',
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_item_renewal
                ON household_item (renewal_date);
            CREATE INDEX IF NOT EXISTS idx_item_state
                ON household_item (state);

            CREATE TABLE IF NOT EXISTS handled_event (
                event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id     TEXT NOT NULL,
                event       TEXT NOT NULL,
                detail      TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (item_id) REFERENCES household_item (item_id)
            );
            """
        )
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # --- API ---------------------------------------------------------------

    def upsert(self, item: HouseholdItem) -> HouseholdItem:
        """Insert or update a household item.

        Phase 1 implementation.
        """
        raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")

    def get(self, item_id: str) -> HouseholdItem | None:
        """Fetch one item by id."""
        raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")

    def due_within(self, days: int, *, today: date | None = None) -> list[HouseholdItem]:
        """Items renewing within `days` that we have not dismissed.

        This is the query the daily brief and the background deadline
        monitor both run.
        """
        raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")

    def record_event(self, item_id: str, event: str, detail: str | None = None) -> None:
        """Append to the handled log.

        The handled log is what stops the agent repeating itself. It is
        append-only on purpose: an audit trail that can be edited is not an
        audit trail.
        """
        raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")

    def was_handled(self, item_id: str, event: str) -> bool:
        """Whether a given event was already recorded for an item."""
        raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")

    def household_summary(self) -> str:
        """A short narrative description of the household picture.

        This is the text that goes into Hermes's `MEMORY.md`. It is written
        for the agent, not for the user: dense, factual, and free of
        anything that does not need to be remembered.

        Phase 1 implementation.
        """
        raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")
