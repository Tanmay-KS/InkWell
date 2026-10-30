"""Tests for the seed_due_item helper."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from agent.memory.hermes_memory import HouseholdMemory, TrackedState
from jobs.deadline_monitor import JobConfig, run_once
from jobs.seed_due_item import main, seed_due_item


def test_seed_due_item_creates_due_item(tmp_path: Path):
    db_path = tmp_path / "inkwell_memory.db"
    today = date(2026, 10, 20)

    result = seed_due_item(
        memory_path=db_path,
        days_ahead=3,
        today=today,
        provider="Gym Club",
        kind="membership",
        holder="Alice Test",
        reference="MBR-001",
        amount=50.0,
    )

    assert result["status"] == "seeded"
    assert result["days_until"] == 3
    assert result["renewal_date"] == "2026-10-23"

    with HouseholdMemory(db_path) as memory:
        item = memory.get(result["item_id"])
        assert item is not None
        assert item.provider == "Gym Club"
        assert item.holder == "Alice Test"
        assert item.reference == "MBR-001"
        assert item.renewal_date == date(2026, 10, 23)
        assert item.state == TrackedState.NEW

        # Confirm it shows in due_within
        due = memory.due_within(7, today=today)
        assert len(due) == 1
        assert due[0].item_id == result["item_id"]


def test_seed_due_item_runs_with_deadline_monitor(tmp_path: Path):
    db_path = tmp_path / "inkwell_memory.db"
    today = date(2026, 10, 20)

    result = seed_due_item(
        memory_path=db_path,
        days_ahead=5,
        today=today,
    )

    config = JobConfig(
        state_path=str(tmp_path / "job_state.json"),
        memory_path=str(db_path),
    )

    # First monitor pass flags the item
    notified_first: list[dict] = []
    flagged_first = run_once(
        config=config,
        today=today,
        memory=HouseholdMemory(db_path),
        notifier=lambda notice: notified_first.append(notice),
    )
    assert len(flagged_first) == 1
    assert len(notified_first) == 1
    assert flagged_first[0]["item_id"] == result["item_id"]

    # Second monitor pass flags 0 items because handled-event table recorded it
    notified_second: list[dict] = []
    flagged_second = run_once(
        config=config,
        today=today,
        memory=HouseholdMemory(db_path),
        notifier=lambda notice: notified_second.append(notice),
    )
    assert len(flagged_second) == 0
    assert len(notified_second) == 0

    # With clear_events=True, seeding again resets the item for a fresh test
    seed_due_item(
        memory_path=db_path,
        days_ahead=5,
        today=today,
        clear_events=True,
    )
    notified_third: list[dict] = []
    flagged_third = run_once(
        today=today,
        memory=HouseholdMemory(db_path),
        notifier=lambda notice: notified_third.append(notice),
    )
    assert len(flagged_third) == 1
    assert len(notified_third) == 1


def test_seed_cli(tmp_path: Path, capsys):
    db_path = tmp_path / "inkwell_memory.db"
    ret = main([
        "--memory-db", str(db_path),
        "--days-ahead", "4",
        "--today", "2026-10-25",
        "--provider", "Car Insurance Co",
        "--json",
    ])
    assert ret == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["provider"] == "Car Insurance Co"
    assert data["renewal_date"] == "2026-10-29"
