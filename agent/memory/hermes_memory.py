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

**The memory layer is also the redactor's source of truth for names.**
The household picture knows who the household is, so the redactor is
*told* the names rather than having to recognise them in prose. That is
the difference between catching a name in a labelled field and catching
it anywhere at all — see `docs/redaction-audit.md`, where the deterministic
pass gets names to 83.5% and the household path gets them to 100%.
"""

from __future__ import annotations

import hashlib
import json
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

    @staticmethod
    def make_id(provider: str, holder: str | None, reference: str | None) -> str:
        """A stable id for an obligation.

        Derived from what the obligation *is*, not from when it was seen,
        so the same notice arriving twice is the same obligation and does
        not produce a duplicate. Without this, a household that receives
        two copies of a renewal notice gets told about it twice.
        """
        key = "|".join(
            [
                (provider or "").strip().lower(),
                (holder or "").strip().lower(),
                (reference or "").strip().lower(),
            ]
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return f"item-{digest}"


class HouseholdMemory:
    """The household picture plus the handled log."""

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

            CREATE INDEX IF NOT EXISTS idx_event_item
                ON handled_event (item_id, event);

            CREATE TABLE IF NOT EXISTS fact (
                subject      TEXT NOT NULL,
                kind         TEXT NOT NULL,
                value        TEXT NOT NULL,
                valid_from   TEXT NOT NULL,
                valid_to     TEXT,
                source       TEXT,
                confidence  REAL NOT NULL,
                metadata     TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (subject, kind, valid_from)
            );
            CREATE INDEX IF NOT EXISTS idx_fact_current
                ON fact (subject, kind, valid_to);
            """
        )
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> HouseholdMemory:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- API ---------------------------------------------------------------

    def upsert(self, item: HouseholdItem) -> HouseholdItem:
        """Insert or update a household item.

        A re-seen item keeps its state. That is the whole point: a
        document arriving twice must not reset an obligation the user
        has already dismissed back to "new" and have the agent bring it
        up again.
        """
        conn = self.connect()
        existing = self.get(item.item_id)

        if existing is not None and existing.state is not TrackedState.NEW:
            # Keep the state the user set.
            item.state = existing.state

        conn.execute(
            """
            INSERT INTO household_item (
                item_id, kind, provider, holder, reference, renewal_date,
                amount, state, source, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                kind         = excluded.kind,
                provider     = excluded.provider,
                holder       = excluded.holder,
                reference    = excluded.reference,
                renewal_date = excluded.renewal_date,
                amount       = excluded.amount,
                state        = excluded.state,
                source       = excluded.source,
                metadata     = excluded.metadata,
                updated_at   = datetime('now')
            """,
            (
                item.item_id,
                item.kind,
                item.provider,
                item.holder,
                item.reference,
                item.renewal_date.isoformat() if item.renewal_date else None,
                item.amount,
                item.state.value,
                item.source,
                json.dumps(item.metadata, sort_keys=True, default=str),
            ),
        )
        conn.commit()
        return item

    def get(self, item_id: str) -> HouseholdItem | None:
        """Fetch one item by id."""
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM household_item WHERE item_id = ?", (item_id,)
        ).fetchone()
        return _row_to_item(row) if row else None

    def all_items(self) -> list[HouseholdItem]:
        """Every item, newest renewal first."""
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT * FROM household_item
            ORDER BY renewal_date IS NULL, renewal_date, provider
            """
        ).fetchall()
        return [_row_to_item(row) for row in rows]

    def due_within(self, days: int, *, today: date | None = None) -> list[HouseholdItem]:
        """Items renewing within `days` that we have not dismissed.

        This is the query the daily brief and the background deadline
        monitor both run.
        """
        today = today or date.today()
        horizon = today + _days(days)
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT * FROM household_item
            WHERE renewal_date IS NOT NULL
              AND renewal_date <= ?
              AND state != ?
              AND NOT EXISTS (
                  SELECT 1 FROM fact
                  WHERE fact.subject = household_item.item_id
                    AND fact.kind = 'subscription_status'
                    AND fact.value IN ('cancelled', 'paused', 'expired')
                    AND fact.valid_from <= ?
                    AND (fact.valid_to IS NULL OR fact.valid_to >= ?)
              )
            ORDER BY renewal_date
            """,
            (horizon.isoformat(), TrackedState.DISMISSED.value,
             today.isoformat(), today.isoformat()),
        ).fetchall()
        return [_row_to_item(row) for row in rows]

    def set_state(self, item_id: str, state: TrackedState) -> HouseholdItem | None:
        """Move an item through its lifecycle."""
        conn = self.connect()
        conn.execute(
            "UPDATE household_item SET state = ?, updated_at = datetime('now') WHERE item_id = ?",
            (state.value, item_id),
        )
        conn.commit()
        return self.get(item_id)

    def record_subscription_status(
        self,
        item_id: str,
        status: str,
        *,
        effective_date: date | None = None,
        source: str | None = None,
        confidence: float = 1.0,
    ):
        """Record an active/cancelled status change with its history."""
        from agent.memory.update_tracker import Fact, FactKind, UpdateTracker

        if status not in {"active", "cancelled", "paused", "expired"}:
            raise ValueError("unsupported subscription status")
        fact = UpdateTracker(self).record(Fact(
            subject=item_id,
            kind=FactKind.SUBSCRIPTION_STATUS,
            value=status,
            valid_from=effective_date or date.today(),
            source=source,
            confidence=confidence,
        ))
        self.record_event(
            item_id, "subscription_status", f"{status} effective {fact.valid_from.isoformat()}"
        )
        return fact

    def record_event(self, item_id: str, event: str, detail: str | None = None) -> None:
        """Append to the handled log.

        The handled log is what stops the agent repeating itself. It is
        append-only on purpose: an audit trail that can be edited is not an
        audit trail.
        """
        conn = self.connect()
        conn.execute(
            "INSERT INTO handled_event (item_id, event, detail) VALUES (?, ?, ?)",
            (item_id, event, detail),
        )
        conn.commit()

    def was_handled(self, item_id: str, event: str) -> bool:
        """Whether a given event was already recorded for an item."""
        conn = self.connect()
        row = conn.execute(
            "SELECT 1 FROM handled_event WHERE item_id = ? AND event = ? LIMIT 1",
            (item_id, event),
        ).fetchone()
        return row is not None

    def events_for(self, item_id: str) -> list[dict[str, object]]:
        """The handled log for one item, oldest first."""
        conn = self.connect()
        rows = conn.execute(
            "SELECT event, detail, created_at FROM handled_event WHERE item_id = ? ORDER BY event_id",
            (item_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def household_names(self) -> list[str]:
        """The names in the household, for the redactor.

        This is the connection between the memory layer and the privacy
        router. The redactor can only recognise a name in prose if it is
        told the name, and the memory layer is what knows it.
        """
        conn = self.connect()
        rows = conn.execute(
            "SELECT DISTINCT holder FROM household_item WHERE holder IS NOT NULL AND holder != ''"
        ).fetchall()
        return sorted(str(row["holder"]) for row in rows)

    def household_summary(self) -> str:
        """A short narrative description of the household picture.

        This is the text that goes into Hermes's `MEMORY.md`. It is written
        for the agent, not for the user: dense, factual, and free of
        anything that does not need to be remembered.
        """
        items = self.all_items()
        if not items:
            return "No obligations are being tracked yet."

        names = self.household_names()
        lines = []
        if names:
            lines.append(f"Household members: {', '.join(names)}.")
        else:
            lines.append("Household members: not yet known.")

        by_provider: dict[str, list[HouseholdItem]] = {}
        for item in items:
            by_provider.setdefault(item.provider, []).append(item)

        lines.append("")
        lines.append("Tracked obligations:")
        for provider, provider_items in sorted(by_provider.items()):
            lines.append(f"  {provider}:")
            for item in provider_items:
                holder = f" ({item.holder})" if item.holder else ""
                when = item.renewal_date.isoformat() if item.renewal_date else "no date"
                amount = f", {item.amount:.2f}" if item.amount is not None else ""
                lines.append(
                    f"    - {item.kind}{holder}, renews {when}{amount} — {item.state.value}"
                )

        dismissed = [i for i in items if i.state is TrackedState.DISMISSED]
        if dismissed:
            lines.append("")
            lines.append("Dismissed — do not raise these again:")
            for item in dismissed:
                lines.append(f"  - {item.provider} ({item.kind})")

        return "\n".join(lines)


def _days(count: int):
    from datetime import timedelta

    return timedelta(days=count)


def _row_to_item(row: sqlite3.Row) -> HouseholdItem:
    """Rebuild a `HouseholdItem` from a database row."""
    try:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    except (json.JSONDecodeError, TypeError):
        metadata = {}

    renewal = row["renewal_date"]
    return HouseholdItem(
        item_id=row["item_id"],
        kind=row["kind"],
        provider=row["provider"],
        holder=row["holder"],
        reference=row["reference"],
        renewal_date=date.fromisoformat(renewal) if renewal else None,
        amount=row["amount"],
        state=TrackedState(row["state"]),
        source=row["source"],
        metadata=metadata,
    )
