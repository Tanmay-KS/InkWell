"""Seed a due item into the Ledger memory database for testing or live proof.

Phase 3 — Reproducible proof helper.

Seeds a deterministic due item into the SQLite sidecar so the deadline monitor
job can be verified in a real environment (or serverless job container) with
predictable, repeatable input.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory, TrackedState

__all__ = ["seed_due_item"]

DEFAULT_PROVIDER = "Lakeshore Health"
DEFAULT_KIND = "insurance"
DEFAULT_HOLDER = "Devin Sullivan"
DEFAULT_REFERENCE = "POL-772910"
DEFAULT_AMOUNT = 120.00
DEFAULT_DAYS_AHEAD = 5


def seed_due_item(
    *,
    memory_path: str | Path | None = None,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    today: date | None = None,
    provider: str = DEFAULT_PROVIDER,
    kind: str = DEFAULT_KIND,
    holder: str = DEFAULT_HOLDER,
    reference: str = DEFAULT_REFERENCE,
    amount: float = DEFAULT_AMOUNT,
    clear_events: bool = False,
) -> dict[str, Any]:
    """Seed a single due item into the memory store.

    Args:
        memory_path: Path to the SQLite memory database. Defaults to
            LEDGER_MEMORY_DB environment variable or standard sidecar path.
        days_ahead: Number of days from `today` until the item renews.
        today: Reference date (defaults to date.today()).
        provider: Organization name.
        kind: Kind of obligation (e.g. insurance, subscription).
        holder: Person name.
        reference: Policy or account identifier.
        amount: Renewal cost.
        clear_events: Whether to wipe existing handled_event records for this
            item, allowing clean re-testing of notification triggers.

    Returns:
        Dictionary summarizing the seeded item and database location.
    """
    ref_date = today or date.today()
    renewal_date = ref_date + timedelta(days=days_ahead)

    path = (
        Path(memory_path)
        if memory_path
        else Path(os.environ.get("LEDGER_MEMORY_DB", "/data/ledger_memory.db"))
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    item_id = HouseholdItem.make_id(provider, holder, reference)
    item = HouseholdItem(
        item_id=item_id,
        kind=kind,
        provider=provider,
        holder=holder,
        reference=reference,
        renewal_date=renewal_date,
        amount=amount,
        state=TrackedState.NEW,
        source="seed_due_item",
        metadata={"seeded_at": ref_date.isoformat()},
    )

    with HouseholdMemory(path) as memory:
        conn = memory.connect()
        if clear_events:
            conn.execute("DELETE FROM handled_event WHERE item_id = ?", (item_id,))
            conn.commit()

        # Reset item state to NEW if it was previously handled or dismissed
        existing = memory.get(item_id)
        if existing is not None and existing.state != TrackedState.NEW:
            memory.set_state(item_id, TrackedState.NEW)

        memory.upsert(item)
        was_handled = memory.was_handled(item_id, "deadline_notified")

    return {
        "status": "seeded",
        "memory_db": str(path),
        "item_id": item_id,
        "provider": provider,
        "kind": kind,
        "holder": holder,
        "reference": reference,
        "renewal_date": renewal_date.isoformat(),
        "days_until": days_ahead,
        "amount": amount,
        "previously_handled": was_handled,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed a due item into the Ledger memory sidecar."
    )
    parser.add_argument(
        "--memory-db",
        default=os.environ.get("LEDGER_MEMORY_DB", "/data/ledger_memory.db"),
        help="SQLite database path (default: $LEDGER_MEMORY_DB or /data/ledger_memory.db)",
    )
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=DEFAULT_DAYS_AHEAD,
        help=f"Days until renewal (default: {DEFAULT_DAYS_AHEAD})",
    )
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="Reference date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        help=f"Provider name (default: {DEFAULT_PROVIDER})",
    )
    parser.add_argument(
        "--kind",
        default=DEFAULT_KIND,
        help=f"Item kind (default: {DEFAULT_KIND})",
    )
    parser.add_argument(
        "--holder",
        default=DEFAULT_HOLDER,
        help=f"Holder name (default: {DEFAULT_HOLDER})",
    )
    parser.add_argument(
        "--reference",
        default=DEFAULT_REFERENCE,
        help=f"Account or policy reference (default: {DEFAULT_REFERENCE})",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=DEFAULT_AMOUNT,
        help=f"Renewal amount (default: {DEFAULT_AMOUNT})",
    )
    parser.add_argument(
        "--clear-events",
        action="store_true",
        help="Clear prior handled_event records for this item to reset notification state",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as machine-readable JSON",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        summary = seed_due_item(
            memory_path=args.memory_db,
            days_ahead=args.days_ahead,
            today=args.today,
            provider=args.provider,
            kind=args.kind,
            holder=args.holder,
            reference=args.reference,
            amount=args.amount,
            clear_events=args.clear_events,
        )
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            print(
                f"[seed] Seeded {summary['kind']} obligation for {summary['provider']} "
                f"(item_id={summary['item_id']}, renewal={summary['renewal_date']}, "
                f"days_until={summary['days_until']}) into {summary['memory_db']}"
            )
        return 0
    except Exception as exc:
        print(f"Error seeding item: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
