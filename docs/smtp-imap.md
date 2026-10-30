# SMTP and IMAP

InkWell's mail side has two independent transports:

| Direction | Transport | Command |
|---|---|---|
| Receive | Resend webhook/API | `python -m agent.mail sync` |
| Receive | any IMAP mailbox | `python -m agent.mail imap-sync` |
| Send | Resend | `python -m agent.mail send --transport resend` |
| Send | any SMTP account | `python -m agent.mail send --transport smtp` |

SMTP and IMAP use the Python standard library only. There is no OAuth flow:
use an app password from the mail provider. The two transports share the
same store, redaction pass, attachment handling and deterministic graph as
the Resend path, so a notice imported over IMAP is processed exactly like
one delivered by webhook.

## Configuration

Copy the SMTP/IMAP block from `.env.example` into `.env`. The minimum for a
single account is:

```dotenv
INKWELL_IMAP_HOST=imap.example.com
INKWELL_IMAP_USER=avery@example.com
INKWELL_IMAP_PASSWORD=app-password

INKWELL_SMTP_HOST=smtp.example.com
INKWELL_SMTP_USER=avery@example.com
INKWELL_SMTP_PASSWORD=app-password
INKWELL_SMTP_FROM=avery@example.com
```

`INKWELL_MAIL_USER` and `INKWELL_MAIL_PASSWORD` are a fallback for both
blocks, so one mailbox that both sends and receives can set only those. If a
per-transport value is present it wins.

Common provider settings:

| Provider | IMAP | SMTP |
|---|---|---|
| Gmail | `imap.gmail.com:993`, SSL, app password | `smtp.gmail.com:587`, STARTTLS, app password |
| Fastmail | `imap.fastmail.com:993`, SSL, app password | `smtp.fastmail.com:587`, STARTTLS, app password |
| Outlook/Microsoft 365 | `outlook.office365.com:993`, SSL | `smtp.office365.com:587`, STARTTLS |
| Proton Mail Bridge | local bridge host/port | local bridge host/port |

Microsoft accounts generally require OAuth for IMAP/SMTP now, and this
client deliberately does not implement it. Gmail and Fastmail app passwords
work without OAuth; the account must have two-factor authentication enabled
first.

TLS defaults are strict:

* IMAP: SSL on 993. Set `INKWELL_IMAP_SSL=0` and
  `INKWELL_IMAP_STARTTLS=1` for STARTTLS on 143.
* SMTP: STARTTLS on 587. Set `INKWELL_SMTP_SSL=1` for implicit TLS on 465.
* Choosing SSL and STARTTLS together is a configuration error.
* A connection with no TLS is refused unless `INKWELL_*_INSECURE=1` is set
  explicitly. That flag exists for a local test server, not for a provider.

## Checking the configuration

```bash
python -m agent.mail doctor            # offline: which transports are configured
python -m agent.mail doctor --online   # log in to each configured transport
```

`doctor --online` calls IMAP `LOGIN`/`SELECT` read-only and SMTP
`LOGIN`/`NOOP`; it does not send a message. It also reports the local row
counts and the Resend key scope. Failures print the host and exception type,
never the password.

## Importing mail over IMAP

```bash
python -m agent.mail imap-sync                # newest 20 in INBOX
python -m agent.mail imap-sync --limit 50
python -m agent.mail imap-sync --mailbox Receipts
```

The import is **read-only**:

* the mailbox is selected with `readonly=True`;
* messages are fetched with `BODY.PEEK[]`, which does not set `\Seen`;
* no message is moved, deleted, labelled or marked;
* progress is a watermark table in the local SQLite sidecar, not a server
  flag.

The watermark is `(UIDVALIDITY, last UID)`. The first pass imports the newest
`--limit` messages (default 20, capped at 200) and records the highest UID it
saw. Later passes search from `last UID + 1`, so a second run is cheap. If
the server reports a new `UIDVALIDITY`, the mailbox was recreated and the old
watermark is discarded rather than applied to unrelated UIDs.

Each message is stored once, keyed by `Message-ID` (or a stable
`imap:<uidvalidity>:<uid>` fallback when the header is missing). Re-importing
the same message records a `duplicate` result and does not add a second row.

What is stored:

* envelope metadata — `From`, `To`, `Subject`, `Date` — as received;
* the body **after redaction**; the raw body is never written;
* attachments, up to `INKWELL_MAIL_MAX_ATTACHMENTS` (default 3) and 15 MiB
  each, passed through the same document graph as an upload.

A malformed body or attachment is reported in the command output and the
message is still recorded. Nothing is retried automatically.

The importer does not run a persistent IDLE connection. To keep a mailbox
current, run `imap-sync` on a timer (systemd user timer or cron). Each run
opens a connection, imports what is new, and closes it.

## Sending over SMTP

Sending is the only path that writes to the outside world, and it stays
behind the same rule as every other outbound action: the user approves the
message first. The command line is the approved call; there is no chat or
web path that invokes it on its own.

```bash
python -m agent.mail send \
  --transport smtp \
  --to jordan@example.com \
  --subject "Renewal confirmed" \
  --text "The policy renews on 15 December."

# auto uses SMTP when it is configured, Resend otherwise
python -m agent.mail send --to jordan@example.com --subject "Hello" --text "..."
```

`--from` overrides `INKWELL_SMTP_FROM`; without either, the SMTP user is the
sender. The command prints the provider's answer and records a copy in the
local store with `direction='outbound'` and `state='sent'`. That copy's body
is redacted, exactly like inbound mail, so the sidecar does not become an
archive of raw correspondence.

A partial refusal (some recipients accepted, some rejected) returns success
with both lists. A refusal of every recipient, an authentication failure or
a connection failure exits non-zero and sends nothing.

```bash
python -m agent.mail list --direction outbound
python -m agent.mail show <message-id>
```

## Security notes

* The password is held in the settings object with `repr=False`, is only
  used for the protocol `LOGIN`, and is never included in an error message.
* The body is redacted before the SQLite write. A redaction failure stores an
  empty body and a warning, rather than falling back to raw text.
* IMAP is read-only by construction, not by configuration. There is no code
  path that sets a flag or deletes a message.
* The transports do not expose a public endpoint. The Resend webhook remains
  the only inbound HTTP route, and it is gated separately by
  `INKWELL_MAIL_PUBLIC`.

## Tests

`tests/test_smtp_imap.py` covers settings parsing and TLS validation, MIME
parsing (plain, HTML, attachments, missing `Message-ID`), redaction before
storage, duplicate handling, read-only selection, `BODY.PEEK`, watermark
advance and `UIDVALIDITY` reset, partial SMTP refusal, TLS refusal and
password-free errors. The suite uses in-process fakes and never opens a
socket.

The transports have not been exercised against a live IMAP or SMTP account
in this workspace; doing so requires the user's host, user name and app
password. `doctor --online` is the first command to run once those exist.
