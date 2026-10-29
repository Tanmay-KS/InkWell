"""Tests for the Phase 3 background deadline monitor.

The job's whole value is that it runs when nobody is watching, so the
tests are about the properties that fail silently in production:

* a due item is announced once, not every day;
* a dry run announces nothing and does not consume the real notification;
* a failed delivery is retried, not swallowed;
* the run record advances, because that is the only evidence the schedule
  is actually firing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory, TrackedState
from jobs.deadline_monitor import JobConfig, run_once, state_status

TODAY = date(2026, 10, 20)


def _item(
    *,
    item_id: str = "item-1",
    provider: str = "Meridian Mutual",
    renewal: date | None = None,
    state: TrackedState = TrackedState.NEW,
) -> HouseholdItem:
    return HouseholdItem(
        item_id=item_id,
        kind="insurance",
        provider=provider,
        holder="Devin Sullivan",
        reference="POL-482913-Z",
        renewal_date=renewal or TODAY + timedelta(days=5),
        amount=148.95,
        state=state,
    )


@pytest.fixture
def memory(tmp_path):
    with HouseholdMemory(tmp_path / "memory.db") as store:
        yield store


def _config(tmp_path, **overrides) -> JobConfig:
    return JobConfig(
        state_path=str(tmp_path / "state" / "job_state.json"),
        memory_path=str(tmp_path / "memory.db"),
        **overrides,
    )


def _run(memory, config, **kwargs):
    return run_once(config, today=TODAY, memory=memory, **kwargs)


def test_flags_a_due_item_once(memory, tmp_path):
    memory.upsert(_item())
    config = _config(tmp_path)
    delivered: list[dict] = []

    flagged = _run(memory, config, notifier=delivered.append)

    assert len(flagged) == 1
    assert flagged[0]["provider"] == "Meridian Mutual"
    assert flagged[0]["days_until"] == 5
    assert flagged[0]["notified"] is True
    assert delivered == [{**flagged[0]}]

    # The second pass is the one that matters: a daily job must not repeat
    # itself.
    assert _run(memory, config, notifier=delivered.append) == []
    assert len(delivered) == 1


def test_notification_carries_no_personal_detail(memory, tmp_path):
    memory.upsert(_item())
    delivered: list[dict] = []

    _run(memory, _config(tmp_path), notifier=delivered.append)

    record = delivered[0]
    assert "holder" not in record
    assert "reference" not in record
    assert "Devin" not in str(record)
    assert "POL-482913-Z" not in str(record)


def test_dry_run_does_not_consume_the_notification(memory, tmp_path):
    memory.upsert(_item())
    config = _config(tmp_path, notify=False)

    preview = _run(memory, config)

    assert len(preview) == 1
    assert preview[0]["notified"] is False
    assert not (tmp_path / "state" / "job_state.json").exists()
    assert not memory.was_handled("item-1", "deadline_notified:2026-10-25")

    # The real run still has something to say.
    assert len(_run(memory, _config(tmp_path))) == 1


def test_dismissed_items_are_left_alone(memory, tmp_path):
    memory.upsert(_item(state=TrackedState.DISMISSED))

    assert _run(memory, _config(tmp_path)) == []


def test_far_off_items_are_not_flagged(memory, tmp_path):
    memory.upsert(_item(renewal=TODAY + timedelta(days=45)))

    assert _run(memory, _config(tmp_path)) == []


def test_a_failed_delivery_is_retried_not_swallowed(memory, tmp_path):
    memory.upsert(_item())
    config = _config(tmp_path)

    def broken(_record):
        raise RuntimeError("gateway down")

    flagged = _run(memory, config, notifier=broken)

    assert flagged[0]["notified"] is False
    assert "gateway down" in flagged[0]["error"]
    assert not memory.was_handled("item-1", "deadline_notified:2026-10-25")

    delivered: list[dict] = []
    assert len(_run(memory, config, notifier=delivered.append)) == 1
    assert len(delivered) == 1


def test_a_moved_renewal_is_announced_again(memory, tmp_path):
    memory.upsert(_item())
    config = _config(tmp_path)
    _run(memory, config)

    memory.upsert(_item(renewal=TODAY + timedelta(days=7)))

    assert len(_run(memory, config)) == 1


def test_the_verifier_summary_is_pushed_once(memory, tmp_path):
    memory.upsert(_item(item_id="item-1", renewal=TODAY + timedelta(days=5)))
    memory.upsert(_item(item_id="item-2", renewal=TODAY + timedelta(days=6)))
    config = _config(tmp_path)
    delivered: list[dict] = []

    flagged = _run(memory, config, notifier=delivered.append)

    summary = [record for record in flagged if record.get("kind") == "findings"]
    assert len(summary) == 1
    assert summary[0]["duplicate_count"] == 1
    assert summary[0]["due_count"] == 2
    assert "duplicate" in str(summary[0]["summary"])
    assert summary[0]["notified"] is True

    # The summary is idempotent like the reminders.
    assert _run(memory, config, notifier=delivered.append) == []


def test_the_run_record_advances(memory, tmp_path):
    memory.upsert(_item())
    config = _config(tmp_path)

    _run(memory, config)

    code, message = state_status(config.state_path)
    assert code == 0
    assert "1 checked, 1 flagged" in message


def test_status_distinguishes_never_run_from_stale(tmp_path):
    missing = tmp_path / "never.json"
    code, message = state_status(missing)
    assert code == 1
    assert "never run" in message

    stale = tmp_path / "stale.json"
    stale.write_text(
        '{"last_run": "%s", "checked": 3, "flagged": []}' % (datetime.now(UTC) - timedelta(days=3))
    )
    code, message = state_status(stale)
    assert code == 1
    assert "not firing" in message


def test_status_rejects_a_corrupt_record(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")

    code, message = state_status(broken)
    assert code == 1
    assert "unreadable" in message
