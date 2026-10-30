"""SMTP and IMAP transports: settings, MIME parsing, watermarking and sending.

No test opens a socket. IMAP is replaced by a small object that answers the
four commands the client uses, and SMTP by one that records the message it
was asked to carry. The interesting behaviour is what the client does with
the answer — read-only selection, `BODY.PEEK`, watermark advances, redaction
before storage, and errors that never echo the password.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formatdate

import pytest

from agent.mail.config import ImapSettings, SmtpSettings
from agent.mail.imap import ImapClient, ImapError, ingest_parsed, parse_message
from agent.mail.smtp import SentMail, SmtpClient, SmtpError, build_message
from agent.mail.store import MailStore
from agent.memory.hermes_memory import HouseholdMemory

# --- settings ---------------------------------------------------------------


def test_smtp_defaults_to_starttls_on_587():
    settings = SmtpSettings.from_env(
        {
            "INKWELL_SMTP_HOST": "smtp.example.com",
            "INKWELL_SMTP_USER": "avery@example.com",
            "INKWELL_SMTP_PASSWORD": "app-password",
        }
    )
    assert settings.port == 587
    assert settings.starttls is True
    assert settings.ssl is False
    assert settings.from_address == "avery@example.com"
    assert settings.configured is True


def test_smtp_implicit_tls_moves_to_465():
    settings = SmtpSettings.from_env(
        {
            "INKWELL_SMTP_HOST": "smtp.example.com",
            "INKWELL_SMTP_SSL": "1",
            "INKWELL_MAIL_USER": "avery@example.com",
            "INKWELL_MAIL_PASSWORD": "app-password",
        }
    )
    assert settings.port == 465
    assert settings.ssl is True
    assert settings.starttls is False


def test_imap_defaults_to_ssl_on_993():
    settings = ImapSettings.from_env(
        {
            "INKWELL_IMAP_HOST": "imap.example.com",
            "INKWELL_IMAP_USER": "avery@example.com",
            "INKWELL_IMAP_PASSWORD": "app-password",
        }
    )
    assert settings.port == 993
    assert settings.ssl is True
    assert settings.mailbox == "INBOX"
    settings.validate()


def test_shared_account_falls_back_for_both_transports():
    env = {
        "INKWELL_IMAP_HOST": "imap.example.com",
        "INKWELL_SMTP_HOST": "smtp.example.com",
        "INKWELL_MAIL_USER": "avery@example.com",
        "INKWELL_MAIL_PASSWORD": "app-password",
    }
    assert ImapSettings.from_env(env).user == "avery@example.com"
    assert SmtpSettings.from_env(env).password == "app-password"


def test_choosing_ssl_and_starttls_is_refused():
    settings = ImapSettings(
        host="imap.example.com", user="avery@example.com", password="x", ssl=True, starttls=True
    )
    with pytest.raises(ValueError, match="not both"):
        settings.validate()


def test_plaintext_is_refused_without_the_escape_hatch():
    settings = SmtpSettings(
        host="smtp.example.com",
        user="avery@example.com",
        password="x",
        ssl=False,
        starttls=False,
        insecure=False,
    )
    with pytest.raises(ValueError, match="plaintext"):
        settings.validate()


def test_plaintext_is_allowed_only_when_explicitly_insecure():
    settings = SmtpSettings(
        host="127.0.0.1",
        user="avery@example.com",
        password="x",
        ssl=False,
        starttls=False,
        insecure=True,
    )
    settings.validate()


def test_a_bad_port_is_a_configuration_error():
    with pytest.raises(ValueError, match="must be an integer"):
        SmtpSettings.from_env({"INKWELL_SMTP_PORT": "five"})


# --- parsing ----------------------------------------------------------------


def _raw(
    *,
    subject: str = "Your renewal notice",
    body: str = "Hello Avery,\n\nThe policy renews soon.",
    message_id: str | None = "<notice-1@northwind.example>",
    attachments: list[tuple[str, bytes, str, str]] | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = "Northwind Home Insurance <notices@northwind.example>"
    message["To"] = "Avery <avery@example.com>, Jordan <jordan@example.com>"
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    if message_id is not None:
        message["Message-ID"] = message_id
    message.set_content(body)
    for filename, payload, maintype, subtype in attachments or []:
        message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


def test_a_message_is_split_into_the_stored_fields():
    parsed = parse_message(_raw(), uid=4, uidvalidity=7)
    assert parsed.message_id == "<notice-1@northwind.example>"
    assert parsed.uid == 4
    assert parsed.uidvalidity == 7
    assert parsed.sender.startswith("Northwind")
    assert parsed.recipients == ["avery@example.com", "jordan@example.com"]
    assert parsed.subject == "Your renewal notice"
    assert "renews soon" in parsed.body
    assert parsed.attachments == []
    assert parsed.warnings == []


def test_an_attachment_is_decoded_with_its_filename():
    raw = _raw(
        attachments=[
            ("notice.pdf", b"%PDF-1.4 test", "application", "pdf"),
        ]
    )
    parsed = parse_message(raw, uid=5, uidvalidity=7)
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].filename == "notice.pdf"
    assert parsed.attachments[0].content_type == "application/pdf"
    assert parsed.attachments[0].payload == b"%PDF-1.4 test"
    assert parsed.warnings == []


def test_a_message_without_a_message_id_gets_a_stable_fallback():
    parsed = parse_message(_raw(message_id=None), uid=9, uidvalidity=7)
    assert parsed.message_id == "imap:7:9"


def test_html_only_mail_is_read_as_text():
    message = EmailMessage()
    message["From"] = "notices@example.com"
    message["To"] = "avery@example.com"
    message["Subject"] = "Notice"
    message.set_content("<p>The policy renews on 15 December.</p>", subtype="html")
    parsed = parse_message(message.as_bytes(), uid=1, uidvalidity=None)
    assert "15 December" in parsed.body
    assert parsed.message_id == "imap:0:1"


# --- ingestion --------------------------------------------------------------


def _tree(tmp_path) -> tuple[HouseholdMemory, MailStore]:
    memory = HouseholdMemory(tmp_path / "memory.db")
    memory.connect()
    return memory, MailStore(memory.connect())


def test_the_stored_body_is_redacted_but_envelope_metadata_is_kept(tmp_path):
    memory, store = _tree(tmp_path)
    parsed = parse_message(
        _raw(body="Write to avery@example.com or call +1 415 555 0132."),
        uid=1,
        uidvalidity=7,
    )
    result = ingest_parsed(
        parsed, store=store, memory=memory, provider_event_id="imap:7:1", account="imap:x"
    )
    assert result["status"] in {"stored", "processed"}

    stored = store.get_message(parsed.message_id)
    assert stored is not None
    assert stored["sender"].startswith("Northwind")
    assert stored["recipients"] == ["avery@example.com", "jordan@example.com"]
    assert "avery@example.com" not in stored["body"]
    assert "415 555 0132" not in stored["body"]
    assert stored["body_redacted"] is True
    assert stored["metadata"]["transport"] == "imap"
    assert stored["metadata"]["uid"] == 1


def test_ingesting_the_same_message_twice_is_a_duplicate(tmp_path):
    memory, store = _tree(tmp_path)
    parsed = parse_message(_raw(), uid=1, uidvalidity=7)
    first = ingest_parsed(
        parsed, store=store, memory=memory, provider_event_id="imap:7:1", account="imap:x"
    )
    second = ingest_parsed(
        parsed, store=store, memory=memory, provider_event_id="imap:7:1", account="imap:x"
    )
    assert second["status"] == "duplicate"
    assert second["message_id"] == first["message_id"]
    assert store.summary()["messages"] == 1


# --- IMAP client ------------------------------------------------------------


class FakeIMAP:
    """The four commands the client actually uses."""

    def __init__(self, messages: dict[int, bytes], *, uidvalidity: int = 7) -> None:
        self.messages = dict(messages)
        self.uidvalidity = uidvalidity
        self.selected: list[tuple[str, bool]] = []
        self.searches: list[str] = []
        self.fetches: list[tuple[int, str]] = []
        self.logged_in: tuple[str, str] | None = None
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.logged_in = (user, password)
        return "OK", [b"logged in"]

    def select(self, mailbox: str, readonly: bool = False):
        self.selected.append((mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name: str):
        if name == "UIDVALIDITY":
            return "OK", [str(self.uidvalidity).encode()]
        if name == "UIDNEXT":
            return "OK", [str(max(self.messages, default=0) + 1).encode()]
        return "NO", [None]

    def uid(self, command: str, *args):
        if command == "SEARCH":
            criterion = str(args[-1])
            start = int(criterion.split(":")[0])
            self.searches.append(criterion)
            found = sorted(uid for uid in self.messages if uid >= start)
            return "OK", [b" ".join(str(uid).encode() for uid in found)]
        if command == "FETCH":
            uid = int(args[0])
            self.fetches.append((uid, str(args[1])))
            return "OK", [(b"1 (BODY[] {123}", self.messages[uid]), b")"]
        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"bye"]


def _settings() -> ImapSettings:
    return ImapSettings(host="imap.example.com", user="avery@example.com", password="app-password")


def _client(fake: FakeIMAP) -> ImapClient:
    return ImapClient(_settings(), factory=lambda _settings: fake)


def test_the_mailbox_is_opened_read_only_and_fetched_with_peek(tmp_path):
    memory, store = _tree(tmp_path)
    fake = FakeIMAP({1: _raw()})
    _client(fake).sync(store=store, memory=memory)

    assert fake.logged_in == ("avery@example.com", "app-password")
    assert fake.selected == [("INBOX", True)]
    assert fake.fetches == [(1, "(BODY.PEEK[])")]
    assert fake.logged_out is True


def test_a_first_sync_imports_the_recent_window_and_sets_a_watermark(tmp_path):
    memory, store = _tree(tmp_path)
    fake = FakeIMAP(
        {
            1: _raw(message_id="<one@example.com>"),
            2: _raw(message_id="<two@example.com>"),
            3: _raw(message_id="<three@example.com>"),
        }
    )
    report = _client(fake).sync(store=store, memory=memory, limit=20)

    assert report["checked"] == 3
    assert report["last_uid"] == 3
    assert report["uidvalidity"] == 7
    assert [row["status"] for row in report["results"]] == ["stored"] * 3
    state = store.get_sync_state("imap:avery@example.com@imap.example.com", "INBOX")
    assert state is not None
    assert state["last_uid"] == 3
    assert state["uidvalidity"] == 7


def test_a_second_sync_only_fetches_what_is_new(tmp_path):
    memory, store = _tree(tmp_path)
    fake = FakeIMAP({1: _raw(message_id="<one@example.com>")})
    client = _client(fake)
    client.sync(store=store, memory=memory)

    fake.messages[2] = _raw(message_id="<two@example.com>")
    fake.fetches.clear()
    report = client.sync(store=store, memory=memory)

    assert [uid for uid, _ in fake.fetches] == [2]
    assert report["checked"] == 1
    assert report["last_uid"] == 2


def test_a_changed_uidvalidity_is_not_trusted_as_a_watermark(tmp_path):
    memory, store = _tree(tmp_path)
    fake = FakeIMAP(
        {
            1: _raw(message_id="<one@example.com>"),
            2: _raw(message_id="<two@example.com>"),
        }
    )
    account = _settings().account
    store.set_sync_state(account, "INBOX", uidvalidity=99, last_uid=500)

    report = _client(fake).sync(store=store, memory=memory)

    assert report["checked"] == 2
    assert [uid for uid, _ in fake.fetches] == [1, 2]


def test_resetting_the_watermark_re_imports_without_duplicating(tmp_path):
    memory, store = _tree(tmp_path)
    fake = FakeIMAP({1: _raw(message_id="<one@example.com>")})
    client = _client(fake)
    client.sync(store=store, memory=memory)

    store.set_sync_state(_settings().account, "INBOX", uidvalidity=7, last_uid=0)
    report = client.sync(store=store, memory=memory)

    assert [row["status"] for row in report["results"]] == ["duplicate"]
    assert store.summary()["messages"] == 1


def test_a_failed_fetch_is_reported_and_does_not_stop_the_sync(tmp_path):
    memory, store = _tree(tmp_path)

    class Flaky(FakeIMAP):
        def uid(self, command: str, *args):
            if command == "FETCH" and int(args[0]) == 1:
                raise ImapError("could not fetch UID 1 (TimeoutError)")
            return super().uid(command, *args)

    fake = Flaky(
        {
            1: _raw(message_id="<one@example.com>"),
            2: _raw(message_id="<two@example.com>"),
        }
    )
    report = _client(fake).sync(store=store, memory=memory)

    assert report["results"][0]["status"] == "error"
    assert report["results"][1]["status"] == "stored"
    assert report["last_uid"] == 2


# --- SMTP -------------------------------------------------------------------


class FakeSMTP:
    def __init__(
        self,
        *,
        refused: dict[str, tuple[int, bytes]] | None = None,
        auth_error: Exception | None = None,
    ) -> None:
        self.refused = refused or {}
        self.auth_error = auth_error
        self.ehlo_calls = 0
        self.starttls_calls = 0
        self.login_args: tuple[str, str] | None = None
        self.message: EmailMessage | None = None
        self.quit_called = False

    def ehlo(self):
        self.ehlo_calls += 1
        return 250, b"ok"

    def starttls(self, *, context=None):
        self.starttls_calls += 1
        return 220, b"ready"

    def login(self, user: str, password: str):
        self.login_args = (user, password)
        if self.auth_error is not None:
            raise self.auth_error
        return 235, b"authenticated"

    def send_message(self, message: EmailMessage):
        self.message = message
        return self.refused

    def noop(self):
        return 250, b"ok"

    def quit(self):
        self.quit_called = True
        return 221, b"bye"

    def close(self):
        return None


def _smtp_settings(*, ssl: bool = False, starttls: bool = True) -> SmtpSettings:
    return SmtpSettings(
        host="smtp.example.com",
        user="avery@example.com",
        password="app-password",
        sender="avery@example.com",
        ssl=ssl,
        starttls=starttls,
    )


def test_a_message_carries_the_headers_the_store_expects():
    message = build_message(
        sender="avery@example.com",
        to=["jordan@example.com"],
        subject="Renewal",
        text="The policy renews on 15 December.",
    )
    assert message["From"] == "avery@example.com"
    assert message["To"] == "jordan@example.com"
    assert message["Subject"] == "Renewal"
    assert message["Message-ID"].endswith("@example.com>")


def test_a_message_needs_a_recipient_subject_and_body():
    with pytest.raises(ValueError, match="recipient"):
        build_message(sender="a@example.com", to=[], subject="x", text="y")
    with pytest.raises(ValueError, match="subject"):
        build_message(sender="a@example.com", to=["b@example.com"], subject=" ", text="y")
    with pytest.raises(ValueError, match="body"):
        build_message(sender="a@example.com", to=["b@example.com"], subject="x")


def test_send_returns_the_accepted_and_refused_recipients():
    fake = FakeSMTP(refused={"jordan@example.com": (550, b"no such user")})
    client = SmtpClient(_smtp_settings(), factory=lambda _settings: fake)

    sent = client.send(to=["avery@example.com", "jordan@example.com"], subject="Hi", text="Body")

    assert isinstance(sent, SentMail)
    assert sent.accepted == ["avery@example.com"]
    assert sent.refused == {"jordan@example.com": (550, b"no such user")}
    assert fake.login_args == ("avery@example.com", "app-password")
    assert fake.quit_called is True
    assert fake.message is not None
    assert fake.message["Subject"] == "Hi"


def test_send_does_not_reach_the_factory_without_tls():
    called = False

    def factory(_settings):
        nonlocal called
        called = True
        return FakeSMTP()

    settings = SmtpSettings(
        host="smtp.example.com",
        user="avery@example.com",
        password="app-password",
        ssl=False,
        starttls=False,
    )
    with pytest.raises(ValueError, match="plaintext"):
        SmtpClient(settings, factory=factory).send(to=["a@example.com"], subject="Hi", text="x")
    assert called is False


def test_an_authentication_error_never_echoes_the_password():
    secret = "s3cret-app-password"
    fake = FakeSMTP(
        auth_error=smtplib.SMTPAuthenticationError(535, b"bad credentials: " + secret.encode())
    )
    settings = SmtpSettings(
        host="smtp.example.com", user="avery@example.com", password=secret, sender="avery@example.com"
    )
    with pytest.raises(SmtpError) as caught:
        SmtpClient(settings, factory=lambda _settings: fake).send(
            to=["a@example.com"], subject="Hi", text="x"
        )
    assert secret not in str(caught.value)
    assert "535" in str(caught.value)


def test_a_connection_failure_names_the_host_but_not_the_password():
    settings = _smtp_settings()
    settings = SmtpSettings(
        host="127.0.0.1",
        port=1,
        user=settings.user,
        password=settings.password,
        sender=settings.sender,
        timeout=0.2,
    )
    with pytest.raises(SmtpError) as caught:
        SmtpClient(settings).verify()
    assert "127.0.0.1:1" in str(caught.value)
    assert settings.password not in str(caught.value)


def test_verify_logs_in_without_sending():
    fake = FakeSMTP()
    report = SmtpClient(_smtp_settings(), factory=lambda _settings: fake).verify()
    assert report["host"] == "smtp.example.com"
    assert report["noop"] == 250
    assert fake.message is None
    assert fake.quit_called is True
