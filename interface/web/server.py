"""Loopback-only HTTP interface for the Ledger household database."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory, TrackedState
from agent.pipeline import process_document
from interface.bot.handlers import BotMessage, LedgerBot

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 15 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}


def _json_item(item, memory: HouseholdMemory) -> dict:
    from agent.memory.update_tracker import FactKind, UpdateTracker

    status = UpdateTracker(memory).current(item.item_id, FactKind.SUBSCRIPTION_STATUS)
    return {
        "item_id": item.item_id,
        "provider": item.provider,
        "kind": item.kind,
        "holder": item.holder,
        "renewal_date": item.renewal_date.isoformat() if item.renewal_date else None,
        # The item's lifecycle is not its subscription status: a freshly
        # imported item is "new" internally but active in the real world.
        "state": status.value if status else (
            "dismissed" if item.state == TrackedState.DISMISSED else "active"
        ),
    }


def _seed_demo(memory: HouseholdMemory) -> None:
    today = date.today()
    samples = [
        ("Northstar Auto", "auto insurance", "Taylor Reed", 4, "active"),
        ("Orchard Fitness", "membership", "Taylor Reed", 16, "active"),
        ("BrightSmile Dental", "dental plan", "Morgan Chen", 29, "active"),
        ("RiverFlix", "subscription", "Taylor Reed", 7, "cancelled"),
    ]
    for provider, kind, holder, days, status in samples:
        renewal = today + timedelta(days=days)
        item_id = HouseholdItem.make_id(provider, holder, f"DEMO-{provider[:3].upper()}")
        memory.upsert(HouseholdItem(
            item_id=item_id,
            kind=kind,
            provider=provider,
            holder=holder,
            renewal_date=renewal,
            state=TrackedState.NEW,
            source="fictional demo fixture",
        ))
        memory.record_subscription_status(
            item_id, status, effective_date=today, source="fictional demo fixture"
        )


class LedgerHandler(BaseHTTPRequestHandler):
    server_version = "LedgerLocal/1.0"

    @property
    def memory(self) -> HouseholdMemory:
        return self.server.memory  # type: ignore[attr-defined]

    def _send_json(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _local_request(self) -> bool:
        host_header = self.headers.get("Host", "")
        parsed_host = urlparse(f"//{host_header}")
        if parsed_host.hostname not in LOOPBACK_HOSTS:
            self.send_error(403, "loopback requests only")
            return False
        if parsed_host.port not in {None, self.server.server_port}:
            self.send_error(403, "unexpected port")
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed_origin = urlparse(origin)
            if (
                parsed_origin.scheme != "http"
                or parsed_origin.hostname not in LOOPBACK_HOSTS
                or parsed_origin.netloc != host_header
            ):
                self.send_error(403, "same-origin requests only")
                return False
        return True

    def do_GET(self) -> None:
        if not self._local_request():
            return
        path = urlparse(self.path).path
        if path == "/api/summary":
            items = self.memory.all_items()
            due = self.memory.due_within(30)
            return self._send_json({
                "items": [_json_item(item, self.memory) for item in items],
                "due": [_json_item(item, self.memory) for item in due],
                "demo": bool(getattr(self.server, "demo_mode", False)),
            })
        target = ROOT / ("index.html" if path == "/" else path.lstrip("/"))
        if target.parent != ROOT or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        content_type = {
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
        }.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if not self._local_request():
            return
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send_json({"error": "invalid Content-Length"}, 400)
        if length <= 0 or length > MAX_UPLOAD:
            return self._send_json({"error": "request size must be between 1 byte and 15 MiB"}, 413)
        body = self.rfile.read(length)
        if path == "/api/chat":
            try:
                data = json.loads(body)
                text = str(data.get("text", "")).strip()
                if not text or len(text) > 2000:
                    return self._send_json({"error": "message must be between 1 and 2000 characters"}, 400)
                reply = LedgerBot(self.memory).on_message(
                    BotMessage(text=text, chat_id="local-web")
                )
                return self._send_json({"reply": reply})
            except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
                return self._send_json({"error": str(exc)}, 400)
        if path == "/api/status":
            try:
                data = json.loads(body)
                item_id = str(data["item_id"])
                status = str(data["status"])
                if status not in {"active", "cancelled", "paused", "expired"}:
                    return self._send_json({"error": "unsupported subscription status"}, 400)
                when = date.fromisoformat(data["effective_date"]) if data.get("effective_date") else date.today()
                item = self.memory.get(item_id)
                if item is None:
                    return self._send_json({"error": "item not found"}, 404)
                fact = self.memory.record_subscription_status(
                    item_id, status, effective_date=when, source="local web interface"
                )
            except (ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
                return self._send_json({"error": str(exc)}, 400)
            return self._send_json({"item": _json_item(item, self.memory), "effective_date": fact.valid_from.isoformat()})
        if path == "/api/upload":
            filename = Path(unquote(self.headers.get("X-Filename", "document.txt"))).name
            suffix = Path(filename).suffix[:12] or ".bin"
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(prefix="ledger-upload-", suffix=suffix, delete=False) as tmp:
                    tmp.write(body)
                    temp_path = Path(tmp.name)
                try:
                    result = process_document(temp_path, memory=self.memory)
                except Exception as exc:  # a bad upload must not kill the server
                    return self._send_json(
                        {"error": f"could not read that document ({type(exc).__name__})"}, 422
                    )
                if not result.ok:
                    return self._send_json({"error": "; ".join(result.warnings) or "no dated obligation found"}, 422)
                if not result.deadlines:
                    return self._send_json({"error": "no dated obligation found"}, 422)
                return self._send_json({
                    "item": _json_item(result.item, self.memory),
                    "deadline": result.deadlines[0].date.isoformat(),
                    "draft": result.action.body,
                })
            finally:
                if temp_path:
                    temp_path.unlink(missing_ok=True)
        return self._send_json({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args) -> None:
        # Requests can contain names, document paths, and item identifiers.
        del fmt, args


def serve(host: str = "127.0.0.1", port: int = 8765, *, demo: bool = False) -> None:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("the local interface must bind to loopback")
    temp_dir = tempfile.TemporaryDirectory(prefix="ledger-demo-") if demo else None
    db_path = (
        Path(temp_dir.name) / "memory.db"
        if temp_dir
        else Path(os.environ.get("LEDGER_MEMORY_DB", str(Path.home() / ".hermes" / "ledger_memory.db")))
    )
    memory = HouseholdMemory(db_path)
    memory.connect()
    if demo:
        _seed_demo(memory)
    server = HTTPServer((host, port), LedgerHandler)
    server.memory = memory  # type: ignore[attr-defined]
    server.demo_mode = demo  # type: ignore[attr-defined]
    print(f"Ledger local interface: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        memory.close()
        if temp_dir:
            temp_dir.cleanup()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", help="use an isolated fictional sample database")
    args = parser.parse_args()
    serve(args.host, args.port, demo=args.demo)
