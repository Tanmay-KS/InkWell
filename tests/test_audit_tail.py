"""The audit printer is part of the safety contract.

The audit log must be safe to put on a screen during the demo. The printer
therefore shows only the decision fields and refuses to print a file that
contains a value it was told to forbid.
"""

from __future__ import annotations

import json

import audit_tail


def write_log(path, records):
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def sample_records():
    return [
        {
            "timestamp": "2026-10-24T12:00:00+00:00",
            "event_type": "router.allowed",
            "decision": "allow",
            "reason_code": "no_personal_detail",
            "host": "api.tavily.com",
            "detail": "not printed",
            "raw": {"secret": "not printed"},
        },
        {
            "timestamp": "2026-10-24T12:00:01+00:00",
            "event_type": "router.denied",
            "decision": "deny",
            "reason_code": "bulk_personal_detail_to_third_party",
            "host": "api.tavily.com",
        },
    ]


def test_last_limits_the_rows(tmp_path, capsys):
    path = write_log(tmp_path / "audit.jsonl", sample_records())
    assert audit_tail.main(["--path", str(path), "--last", "1"]) == 0
    out = capsys.readouterr().out
    assert "router.denied" in out
    assert "router.allowed" not in out


def test_since_line_skips_earlier_records(tmp_path, capsys):
    path = write_log(tmp_path / "audit.jsonl", sample_records())
    assert audit_tail.main(["--path", str(path), "--since-line", "1"]) == 0
    out = capsys.readouterr().out
    assert "router.denied" in out
    assert "router.allowed" not in out


def test_forbid_fails_on_a_value_in_the_log(tmp_path, capsys):
    path = write_log(
        tmp_path / "audit.jsonl",
        [{"event_type": "router.allowed", "detail": "account 0847-4851"}],
    )
    assert audit_tail.main(["--path", str(path), "--forbid", "0847-4851"]) == 1
    assert "forbidden value" in capsys.readouterr().err


def test_forbid_passes_when_the_value_is_absent(tmp_path):
    path = write_log(tmp_path / "audit.jsonl", sample_records())
    assert audit_tail.main(["--path", str(path), "--forbid", "0847-4851"]) == 0


def test_the_table_never_prints_detail_or_raw(tmp_path, capsys):
    path = write_log(tmp_path / "audit.jsonl", sample_records())
    assert audit_tail.main(["--path", str(path)]) == 0
    out = capsys.readouterr().out
    assert "not printed" not in out
    assert "secret" not in out


def test_json_mode_prints_only_the_safe_subset(tmp_path, capsys):
    path = write_log(tmp_path / "audit.jsonl", sample_records())
    assert audit_tail.main(["--path", str(path), "--json"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0] == {
        "decision": "allow",
        "event_type": "router.allowed",
        "host": "api.tavily.com",
        "reason_code": "no_personal_detail",
        "timestamp": "2026-10-24T12:00:00+00:00",
    }
    assert all("raw" not in line and "detail" not in line for line in lines)


def test_a_missing_log_is_an_evaluation_error(tmp_path, capsys):
    assert audit_tail.main(["--path", str(tmp_path / "missing.jsonl")]) == 2
    assert "not found" in capsys.readouterr().err
