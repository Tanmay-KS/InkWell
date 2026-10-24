#!/usr/bin/env python3
"""Print the decision fields of a Ledger audit JSONL, never the payload.

    python3 sandbox/audit_tail.py --path /var/log/ledger/audit.jsonl --last 10
    python3 sandbox/audit_tail.py --path audit.jsonl --since-line 42
    python3 sandbox/audit_tail.py --path audit.jsonl --forbid 0847-4851

The audit log is meant to be safe to show on screen. This printer is part
of that contract: it prints the timestamp, event type, decision, reason
code and host, and nothing from `raw` or `detail`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True)
    parser.add_argument("--last", type=int, default=0, help="only the last N records")
    parser.add_argument(
        "--since-line",
        type=int,
        default=0,
        help="skip the first N lines (1-based: N=5 starts at line 6)",
    )
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        help="exit 1 if this value appears anywhere in the file",
    )
    parser.add_argument("--json", action="store_true", help="print each safe subset as JSON")
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.exists():
        print(f"audit log not found: {path}", file=sys.stderr)
        return 2
    text = path.read_text(encoding="utf-8")
    for value in args.forbid:
        if value and value in text:
            print(f"FAIL  audit log contains forbidden value: {value}", file=sys.stderr)
            return 1

    lines = [line for line in text.splitlines() if line.strip()]
    if args.since_line:
        lines = lines[args.since_line :]
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    if args.last:
        records = records[-args.last :]

    if not records:
        print("no audit records")
        return 0

    if args.json:
        for record in records:
            print(
                json.dumps(
                    {
                        "timestamp": record.get("timestamp"),
                        "event_type": record.get("event_type"),
                        "decision": record.get("decision"),
                        "reason_code": record.get("reason_code"),
                        "host": record.get("host"),
                    },
                    sort_keys=True,
                )
            )
        return 0

    print(f"{'TIME':<25} {'EVENT':<18} {'DECISION':<10} {'REASON':<48} HOST")
    for record in records:
        print(
            f"{str(record.get('timestamp'))[:25]:<25} "
            f"{str(record.get('event_type'))[:18]:<18} "
            f"{str(record.get('decision'))[:10]:<10} "
            f"{str(record.get('reason_code'))[:48]:<48} "
            f"{record.get('host') or ''}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
