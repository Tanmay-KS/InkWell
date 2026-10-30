"""Loopback-only HTTP interface for the InkWell household database."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import deque
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from agent.actions.draft_actions import ActionKind, draft_action
from agent.graph import run_document_graph, run_text_graph
from agent.mail.inbound import handle_webhook
from agent.mail.resend import ResendClient, ResendError, WebhookVerificationError
from agent.mail.store import MailStore
from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory, TrackedState
from agent.verifier import inventory_findings
from interface.bot.handlers import BotMessage, InkWellBot

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 15 * 1024 * 1024
MAX_WEBHOOK = 1 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
DEFAULT_RATE_LIMIT = 30
DEFAULT_RATE_WINDOW = 60.0

# Model drafting is opt-in. The demo is deterministic by default; set
# INKWELL_USE_MODEL_DRAFTER=1 to route the Drafter through Nemotron Super.
USE_MODEL_DRAFTER = os.environ.get("INKWELL_USE_MODEL_DRAFTER", "").strip() in {
    "1",
    "true",
    "yes",
}


def _flag(name: str) -> bool:
    """Read a boolean environment flag (`1`, `true`, `yes`)."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


class RateLimiter:
    """A small per-client sliding-window limiter for POST endpoints.

    The demo is loopback-only, but a runaway page or a script can still
    spend model budget or fill the temporary database. The limit is
    deliberately simple and local: no shared state, no dependency.
    """

    def __init__(self, limit: int = DEFAULT_RATE_LIMIT, window_seconds: float = DEFAULT_RATE_WINDOW):
        if limit < 1:
            raise ValueError("rate limit must be at least 1")
        self.limit = limit
        if window_seconds <= 0:
            raise ValueError("rate window must be positive")
        self.window_seconds = float(window_seconds)
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> RateLimiter:
        try:
            limit = int(os.environ.get("INKWELL_RATE_LIMIT", DEFAULT_RATE_LIMIT))
        except ValueError:
            limit = DEFAULT_RATE_LIMIT
        try:
            window = float(os.environ.get("INKWELL_RATE_WINDOW", DEFAULT_RATE_WINDOW))
        except ValueError:
            window = DEFAULT_RATE_WINDOW
        return cls(limit=limit, window_seconds=window)

    def check(self, key: str) -> tuple[bool, int]:
        """Return `(allowed, retry_after_seconds)`.

        `retry_after` is 0 when the request is allowed.
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - events[0])) + 1)
                return False, retry_after
            events.append(now)
            return True, 0

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
    # The two Northstar rows are the deliberate duplicate: same provider,
    # same amount, two days apart. The Verifier reports them; the dashboard
    # opens on that finding. Everything here is fictional.
    samples = [
        ("Northstar Auto", "auto insurance", "Taylor Reed", 4, "active", 148.95),
        ("Northstar Auto", "auto insurance", "Taylor Reed", 6, "active", 148.95),
        ("Orchard Fitness", "membership", "Taylor Reed", 16, "active", 42.00),
        ("BrightSmile Dental", "dental plan", "Morgan Chen", 29, "active", 87.50),
        ("RiverFlix", "subscription", "Taylor Reed", 7, "cancelled", 12.99),
    ]
    for index, (provider, kind, holder, days, status, amount) in enumerate(samples):
        renewal = today + timedelta(days=days)
        reference = f"DEMO-{provider[:3].upper()}-{index}"
        item_id = HouseholdItem.make_id(provider, holder, reference)
        memory.upsert(HouseholdItem(
            item_id=item_id,
            kind=kind,
            provider=provider,
            holder=holder,
            reference=reference,
            renewal_date=renewal,
            amount=amount,
            state=TrackedState.NEW,
            source="fictional demo fixture",
        ))
        memory.record_subscription_status(
            item_id, status, effective_date=today, source="fictional demo fixture"
        )


DEMO_NOTICE = """Meridian Mutual Insurance
Renewal Notice

Dear Taylor Reed,

Your homeowners policy is scheduled to renew on 2027-03-07.

Premium: $120.00
Service fee: $28.95
Total: $148.95

Policy number: POL-482913-Z
Policy holder: Taylor Reed

To cancel instead, notify us in writing before 2027-03-07.
"""


def _demo_trace_state():
    """A real run over fictional data, with a planted Auditor rejection."""

    def plant_wrong_amount(deadline, item, facts):
        draft = draft_action(deadline, context=item, kind=ActionKind.REFUND)
        if facts.stated_total is not None:
            correct = f"{float(facts.stated_total):.2f}"
            draft.body = draft.body.replace(correct, f"{float(facts.stated_total) + 36:.2f}")
        return draft

    return run_text_graph(
        DEMO_NOTICE,
        action_kind=ActionKind.REFUND,
        drafter=plant_wrong_amount,
    )


class InkWellHandler(BaseHTTPRequestHandler):
    server_version = "InkWellLocal/1.0"

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
        # The loopback server never sets this flag. The Vercel demo state
        # does, and it only ever holds fictional data in a throwaway
        # database; see `interface/web/wsgi.py`.
        if getattr(self.server, "public_demo", False):
            return True
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

    def _limiter(self) -> RateLimiter:
        limiter = getattr(self.server, "rate_limiter", None)
        if limiter is None:
            limiter = RateLimiter.from_env()
            self.server.rate_limiter = limiter  # type: ignore[attr-defined]
        return limiter

    def _rate_limited(self) -> bool:
        allowed, retry_after = self._limiter().check(self.client_address[0])
        if allowed:
            return False
        payload = json.dumps(
            {"error": "too many requests; wait and try again"}
        ).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Retry-After", str(retry_after))
        self.send_header("Cache-Control", "no-store")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)
        return True

    # --- mail ---------------------------------------------------------------

    def _mail_store(self) -> MailStore:
        return MailStore(self.memory.connect())

    def _mail_readable(self) -> bool:
        """Whether the mail read routes are available in this deployment.

        Off on the public demo (see `wsgi.py`) unless explicitly enabled, so
        a throwaway instance never exposes an inbox. The local server is
        unaffected: it is loopback-only and reads the household sidecar.
        """
        return not getattr(self.server, "public_demo", False) or _flag("INKWELL_MAIL_PUBLIC")

    def _resend_webhook(self) -> None:
        """Handle one Resend webhook.

        This is the only POST route reachable from outside loopback, so it
        does not use `_local_request`. The Svix signature is the
        authentication: an unsigned request never reaches the store.
        """
        secret = os.environ.get("RESEND_WEBHOOK_SECRET", "").strip()
        if not secret:
            return self._send_json({"error": "resend webhook is not configured"}, 503)
        if getattr(self.server, "public_demo", False) and not _flag("INKWELL_MAIL_PUBLIC"):
            # A public deployment must not accumulate real mail in a
            # throwaway database, and must not become a readable inbox.
            return self._send_json(
                {"error": "mail ingestion is disabled on the public demo"}, 503
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send_json({"error": "invalid Content-Length"}, 400)
        if length <= 0 or length > MAX_WEBHOOK:
            return self._send_json(
                {"error": "request size must be between 1 byte and 1 MiB"}, 413
            )
        raw = self.rfile.read(length)
        headers = {key: value for key, value in self.headers.items()}
        try:
            result = handle_webhook(
                raw,
                headers,
                store=self._mail_store(),
                client=ResendClient(),
                memory=self.memory,
                secret=secret,
            )
        except WebhookVerificationError as exc:
            return self._send_json({"error": str(exc)}, 400)
        except ResendError:
            # 502 tells Resend to retry. The message is already idempotent,
            # so a retry either completes the fetch or finds a duplicate.
            return self._send_json({"error": "resend request failed; retry"}, 502)
        return self._send_json(result)

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
                "findings": inventory_findings(items),
                "demo": bool(getattr(self.server, "demo_mode", False)),
            })
        if path == "/api/trace":
            run = getattr(self.server, "last_run", None)
            if run is None:
                run = getattr(self.server, "demo_trace", None)
            return self._send_json({
                "run": run,
                "demo": bool(getattr(self.server, "demo_mode", False)),
            })
        if (path == "/api/mail" or path.startswith("/api/mail/")) and not self._mail_readable():
            # The public demo has no household mail, and it must not become
            # a readable inbox if one is ever wired to it.
            self.send_error(404)
            return
        if path == "/api/mail":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = int(query.get("limit", ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            store = self._mail_store()
            return self._send_json({
                "messages": store.list_messages(limit),
                "summary": store.summary(),
                "demo": bool(getattr(self.server, "demo_mode", False)),
            })
        if path.startswith("/api/mail/"):
            message_id = unquote(path[len("/api/mail/") :])
            store = self._mail_store()
            message = store.get_message(message_id)
            if message is None:
                return self._send_json({"error": "message not found"}, 404)
            return self._send_json({
                "message": message,
                "events": store.events_for(message_id),
                "attachments": store.attachments_for(message_id),
            })
        # Keep the dashboard at a stable, human-readable route while the
        # root page explains the product. Static assets remain at the root
        # so the existing dashboard JavaScript keeps its same-origin URL.
        if path == "/app":
            path = "/dashboard.html"
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
        path = urlparse(self.path).path
        if path == "/api/resend/webhook":
            return self._resend_webhook()
        if not self._local_request():
            return
        if self._rate_limited():
            return
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
                reply = InkWellBot(self.memory).on_message(
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
                with tempfile.NamedTemporaryFile(prefix="inkwell-upload-", suffix=suffix, delete=False) as tmp:
                    tmp.write(body)
                    temp_path = Path(tmp.name)
                try:
                    graph_options: dict[str, object] = {}
                    if USE_MODEL_DRAFTER:
                        from agent.model import NebiusClient

                        graph_options = {
                            "use_model_drafter": True,
                            "model_client": NebiusClient(),
                        }
                    state = run_document_graph(
                        temp_path, memory=self.memory, **graph_options
                    )
                except Exception as exc:  # a bad upload must not kill the server
                    return self._send_json(
                        {"error": f"could not read that document ({type(exc).__name__})"}, 422
                    )
                self.server.last_run = state.to_dict()  # type: ignore[attr-defined]
                if state.status == "error" or state.draft is None or state.item is None:
                    return self._send_json(
                        {"error": "; ".join(state.warnings) or "no dated obligation found"},
                        422,
                    )
                return self._send_json({
                    "item": _json_item(state.item, self.memory),
                    "deadline": state.deadlines[0].date.isoformat(),
                    "draft": state.draft.body,
                    "status": state.status,
                    "retries": state.retries,
                    "audit": state.audit_result.to_dict() if state.audit_result else None,
                    "trace": state.trace.to_dicts(),
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
    temp_dir = tempfile.TemporaryDirectory(prefix="inkwell-demo-") if demo else None
    db_path = (
        Path(temp_dir.name) / "memory.db"
        if temp_dir
        else Path(os.environ.get("INKWELL_MEMORY_DB", str(Path.home() / ".hermes" / "inkwell_memory.db")))
    )
    memory = HouseholdMemory(db_path)
    memory.connect()
    if demo:
        _seed_demo(memory)
    server = HTTPServer((host, port), InkWellHandler)
    server.memory = memory  # type: ignore[attr-defined]
    server.demo_mode = demo  # type: ignore[attr-defined]
    server.last_run = None  # type: ignore[attr-defined]
    server.demo_trace = _demo_trace_state().to_dict() if demo else None  # type: ignore[attr-defined]
    print(f"InkWell local interface: http://{host}:{port}")
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
