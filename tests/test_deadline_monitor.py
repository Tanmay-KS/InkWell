"""Tests for the Phase 3 deadline monitor.

These tests run entirely offline. They use a temporary SQLite sidecar and
fake notifiers, so the idempotency and failure behavior can be asserted
without a Nebius job, a chat gateway, or a real household database.

The important property is not "does it find a date". `HouseholdMemory`
already owns that query. The monitor's job is to notify *once*, fail
retryably, and keep personal values out of operational output.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory, TrackedState
from jobs.deadline_monitor import JobConfig, main, run_once
from jobs.notifier import command_notifier, stdout_notifier

TODAY = date(2026, 10, 20)


class FakeNotifier:
    """A notifier that records notices and can fail on demand."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.notices: list[dict[str, object]] = []
        self.error = error

    def __call__(self, notice: dict[str, object]) -> None:
        self.notices.append(dict(notice))
        if self.error is not None:
            raise self.error


@pytest.fixture
def memory(tmp_path):
    store = HouseholdMemory(tmp_path / "memory.db")
    yield store
    store.close()


@pytest.fixture
def config(tmp_path):
    return JobConfig(
        look_ahead_days=14,
        notify=True,
        state_path=str(tmp_path / "state" / "job_state.json"),
        household_id="test-house",
    )


def add_item(
    memory: HouseholdMemory,
    *,
    provider: str = "Acme",
    days: int = 3,
    holder: str = "Devin Sullivan",
    reference: str = "REF-123",
    state: TrackedState = TrackedState.NEW,
    kind: str = "subscription",
) -> HouseholdItem:
    item = HouseholdItem(
        item_id=HouseholdItem.make_id(provider, holder, reference),
        kind=kind,
        provider=provider,
        holder=holder,
        reference=reference,
        renewal_date=TODAY + timedelta(days=days),
        state=state,
    )
    memory.upsert(item)
    return item


def test_due_item_is_notified_and_marked_handled(memory, config):
    item = add_item(memory)
    notifier = FakeNotifier()

    flagged = run_once(
        config,
        today=TODAY,
        memory=memory,
        notifier=notifier,
    )

    assert len(flagged) == 1
    assert flagged[0]["item_id"] == item.item_id
    assert flagged[0]["notified"] is True
    assert flagged[0]["days_until"] == 3
    assert memory.was_handled(item.item_id, "notified") is True

    [notice] = notifier.notices
    assert notice["holder"] == "Devin Sullivan"
    assert notice["reference"] == "REF-123"
    assert notice["event"] == "deadline"


def test_the_returned_summary_does_not_contain_personal_fields(memory, config):
    add_item(memory)

    [summary] = run_once(
        config,
        today=TODAY,
        memory=memory,
        notifier=FakeNotifier(),
    )

    assert "holder" not in summary
    assert "reference" not in summary
    assert "amount" not in summary


def test_item_outside_the_window_is_not_flagged(memory, config):
    add_item(memory, provider="Later", days=30)
    notifier = FakeNotifier()

    flagged = run_once(config, today=TODAY, memory=memory, notifier=notifier)

    assert flagged == []
    assert notifier.notices == []


def test_item_on_the_window_boundary_is_flagged(memory, config):
    add_item(memory, provider="Boundary", days=14)

    flagged = run_once(
        config,
        today=TODAY,
        memory=memory,
        notifier=FakeNotifier(),
    )

    assert len(flagged) == 1
    assert flagged[0]["days_until"] == 14


def test_dismissed_item_is_not_flagged(memory, config):
    add_item(memory, provider="Dismissed", state=TrackedState.DISMISSED)
    notifier = FakeNotifier()

    flagged = run_once(config, today=TODAY, memory=memory, notifier=notifier)

    assert flagged == []
    assert notifier.notices == []


def test_handled_item_is_not_flagged_even_without_an_event(memory, config):
    add_item(memory, provider="Already Handled", state=TrackedState.HANDLED)
    notifier = FakeNotifier()

    flagged = run_once(config, today=TODAY, memory=memory, notifier=notifier)

    assert flagged == []
    assert notifier.notices == []


def test_second_run_does_not_notify_again(memory, config):
    add_item(memory)
    notifier = FakeNotifier()

    first = run_once(config, today=TODAY, memory=memory, notifier=notifier)
    second = run_once(config, today=TODAY, memory=memory, notifier=notifier)

    assert len(first) == 1
    assert second == []
    assert len(notifier.notices) == 1


def test_notifier_can_be_injected_through_the_config(memory, config):
    add_item(memory)
    notifier = FakeNotifier()
    config.notifier = notifier

    flagged = run_once(config, today=TODAY, memory=memory)

    assert len(flagged) == 1
    assert len(notifier.notices) == 1


def test_callable_in_extra_is_used_as_a_notifier(memory, config):
    add_item(memory)
    notifier = FakeNotifier()
    config.extra["notifier"] = notifier

    flagged = run_once(config, today=TODAY, memory=memory)

    assert len(flagged) == 1
    assert len(notifier.notices) == 1


def test_notifier_failure_is_retryable(memory, config):
    item = add_item(memory)
    failing = FakeNotifier(error=RuntimeError("gateway down"))

    first = run_once(config, today=TODAY, memory=memory, notifier=failing)

    assert len(first) == 1
    assert first[0]["notified"] is False
    assert memory.was_handled(item.item_id, "notified") is False

    succeeding = FakeNotifier()
    second = run_once(config, today=TODAY, memory=memory, notifier=succeeding)

    assert len(second) == 1
    assert second[0]["notified"] is True
    assert memory.was_handled(item.item_id, "notified") is True
    assert len(succeeding.notices) == 1


def test_dry_run_does_not_notify_or_mark_handled(memory, tmp_path):
    item = add_item(memory)
    config = JobConfig(
        look_ahead_days=14,
        notify=False,
        state_path=str(tmp_path / "state.json"),
        household_id="test-house",
    )
    notifier = FakeNotifier()

    flagged = run_once(config, today=TODAY, memory=memory, notifier=notifier)

    assert len(flagged) == 1
    assert flagged[0]["notified"] is False
    assert notifier.notices == []
    assert memory.was_handled(item.item_id, "notified") is False


def test_state_file_is_written_without_personal_values(memory, config):
    add_item(memory)

    run_once(config, today=TODAY, memory=memory, notifier=FakeNotifier())

    path = Path(config.state_path)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "Devin Sullivan" not in text
    assert "REF-123" not in text

    state = json.loads(text)
    assert state["household_id"] == "test-house"
    assert state["due"] == 1
    assert state["flagged"] == 1
    assert state["notified"] == 1
    assert state["failures"] == 0


def test_state_write_failure_does_not_cancel_notification(memory, tmp_path):
    add_item(memory)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    config = JobConfig(
        look_ahead_days=14,
        notify=True,
        state_path=str(blocker / "job_state.json"),
        household_id="test-house",
    )
    notifier = FakeNotifier()

    flagged = run_once(config, today=TODAY, memory=memory, notifier=notifier)

    assert len(flagged) == 1
    assert flagged[0]["notified"] is True
    assert len(notifier.notices) == 1


def test_negative_look_ahead_is_rejected(memory):
    config = JobConfig(look_ahead_days=-1, notify=False)

    with pytest.raises(ValueError):
        run_once(config, today=TODAY, memory=memory)


def test_stdout_notifier_omits_personal_fields(capsys):
    notice = {
        "event": "deadline",
        "household_id": "test-house",
        "item_id": "item-1",
        "kind": "subscription",
        "provider": "Acme",
        "holder": "Devin Sullivan",
        "reference": "REF-123",
        "renewal_date": "2026-10-23",
        "days_until": 3,
        "amount": 12.34,
    }

    stdout_notifier()(notice)
    output = capsys.readouterr().out

    payload = json.loads(output)
    assert payload["event"] == "deadline_notification"
    assert payload["item_id"] == "item-1"
    assert payload["provider"] == "Acme"
    assert "amount" not in payload
    assert "Devin Sullivan" not in output
    assert "REF-123" not in output


def test_command_notifier_pipes_the_full_notice(tmp_path):
    script = tmp_path / "notify.py"
    output = tmp_path / "notice.json"
    script.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(sys.stdin.read(), encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(script), str(output)])
    notice = {
        "event": "deadline",
        "item_id": "item-1",
        "holder": "Devin Sullivan",
        "reference": "REF-123",
    }

    command_notifier(command)(notice)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["item_id"] == "item-1"
    assert payload["holder"] == "Devin Sullivan"
    assert payload["reference"] == "REF-123"


def test_command_notifier_failure_does_not_echo_the_payload():
    command = shlex.join([sys.executable, "-c", "raise SystemExit(7)"])
    notifier = command_notifier(command)

    with pytest.raises(RuntimeError) as caught:
        notifier({"holder": "Devin Sullivan", "reference": "REF-123"})

    assert "7" in str(caught.value)
    assert "Devin Sullivan" not in str(caught.value)
    assert "REF-123" not in str(caught.value)


def test_command_notifier_rejects_an_empty_command():
    with pytest.raises(ValueError):
        command_notifier("   ")


def test_command_notifier_rejects_a_non_positive_timeout():
    with pytest.raises(ValueError):
        command_notifier(sys.executable, timeout=0)


def test_command_notifier_times_out():
    command = shlex.join([sys.executable, "-c", "import time; time.sleep(2)"])
    notifier = command_notifier(command, timeout=0.05)

    with pytest.raises(subprocess.TimeoutExpired):
        notifier({"item_id": "item-1"})


def test_module_entrypoint_does_not_emit_runpy_warning():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "jobs.deadline_monitor", "--help"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "RuntimeWarning" not in result.stderr


def test_main_dry_run_does_not_mark_items(memory, tmp_path, capsys):
    item = add_item(memory)
    state_path = tmp_path / "state.json"

    code = main(
        [
            "--dry-run",
            "--today",
            TODAY.isoformat(),
            "--memory-db",
            str(memory.path),
            "--state-path",
            str(state_path),
        ]
    )

    assert code == 0
    assert memory.was_handled(item.item_id, "notified") is False
    output = capsys.readouterr().out
    assert "flagged 1 item(s)" in output
    assert "Devin Sullivan" not in output
    assert "REF-123" not in output


def test_main_returns_failure_when_notification_fails(memory, tmp_path):
    item = add_item(memory)
    command = shlex.join([sys.executable, "-c", "raise SystemExit(1)"])

    code = main(
        [
            "--notify",
            "--notify-command",
            command,
            "--today",
            TODAY.isoformat(),
            "--memory-db",
            str(memory.path),
            "--state-path",
            str(tmp_path / "state.json"),
        ]
    )

    assert code == 1
    assert memory.was_handled(item.item_id, "notified") is False
