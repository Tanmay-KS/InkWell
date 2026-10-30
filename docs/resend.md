# Resend: inbound mail and delivery tracking

InkWell can receive email through [Resend](https://resend.com) and turn a
household notice into a tracked item. The webhook path is real; the home
database stays local. This page documents both halves, and the boundary
between them.

## What it does

- An address receives mail at a verified domain or Resend's managed
  `<id>.resend.app` address.
- Resend sends an `email.received` webhook with **metadata only**. InkWell
  verifies the Svix signature, fetches the body with the same API key,
  **redacts it**, and stores the redacted text plus a link to any tracked
  item.
- Delivery events (`email.delivered`, `bounced`, `complained`, …) are
  recorded against the message so a sent draft has a visible outcome.
- Attachments are downloaded, capped at 15 MiB and three files by default,
  and run through the same deterministic document graph as an upload.

InkWell never sends mail from a webhook. Outbound sending is a separate,
approval-gated action (`python -m agent.mail send`).

## The one non-obvious requirement

**Use a full-access API key.** Resend's `GET /emails/receiving/{id}` is not
available to a send-only key, so a send-only key delivers the webhook but
cannot fetch the body. That failure looks like an empty inbox. `doctor`
says so explicitly:

```bash
python -m agent.mail doctor
```

```
RESEND_API_KEY: re_Tex…7Pja (36 chars)
RESEND_WEBHOOK_SECRET: set
domains: 0
webhooks: 1
received: 0
local mail rows: 0 messages, 0 events
states: {}
```

## Configuration

Add to `.env` (never commit it):

```bash
RESEND_API_KEY=re_...
RESEND_WEBHOOK_SECRET=whsec_...
# optional
INKWELL_MAIL_MAX_ATTACHMENTS=3
```

`RESEND_WEBHOOK_SECRET` is the Svix signing secret returned when the
webhook is created. Create the endpoint after the server is reachable:

```bash
python -m agent.mail webhook-create \
  --endpoint https://your-host/api/resend/webhook
```

The response includes `signing_secret`; copy it into `.env` and into the
deployment's environment. Events subscribed by default are `email.received`
plus the sent/delivered/delayed/bounced/complained/opened/clicked events.
Use `--receiving-only` for just `email.received`.

## Receiving address

Resend assigns a managed receiving address to the account, but it is **not
exposed by the API** and there is no domain in the account until one is
added. Copy it from the dashboard:

> Resend dashboard → **Emails** → **Receiving** → **⋯** → **Receiving
> address**

To use your own domain instead, add and verify it in **Domains**, then use
any address at that domain. The receiving address is not a secret, but it
is also not something to publish; mail sent there is real mail.

## Two ways to ingest

### 1. Webhook (real time, private target)

`POST /api/resend/webhook` is the only route reachable from outside
loopback. It does not use the loopback check; the signature is the
authentication:

- `svix-id`, `svix-timestamp`, `svix-signature` are required;
- the signature covers `"{svix-id}.{svix-timestamp}.{raw_body}"`;
- stale timestamps (more than five minutes) are rejected;
- the raw bytes are verified **before** JSON parsing.

Requests are capped at 1 MiB and are idempotent by Resend's `email_id`.
Provider events are deduplicated by `svix-id`. A `502` tells Resend to
retry; the retry either completes the fetch or finds the duplicate.

Point this at a host you control, not at the public demo.

### 2. `sync` (local, no public endpoint)

The local product does not need to expose anything:

```bash
python -m agent.mail sync
```

This lists `GET /emails/receiving`, imports anything new exactly as the
webhook does (fetch, redact, graph, store), and skips messages already
present. It is the honest path for a household machine and for testing
before a webhook exists.

```bash
python -m agent.mail list
python -m agent.mail show <email_id>
```

## Stored data

The mail tables live in the same SQLite sidecar as the household items
(`~/.hermes/inkwell_memory.db`):

- `mail_message` — sender, recipients, subject, **redacted** body, state,
  and the item it produced;
- `mail_event` — provider events, deduplicated by `svix-id`;
- `mail_attachment` — filename, type, size, and processing state, never the
  bytes.

Redaction happens before the row is written. If redaction fails, the body
is stored empty and a warning is recorded; it does not fall back to raw
text. Attachment files are written to a temporary file for parsing and
deleted afterwards.

## The public demo is deliberately closed

The Vercel deployment uses a throwaway database that is recycled per
instance. Real mail must not land there, and the demo must not become a
public inbox:

- `/api/mail` and `/api/mail/<id>` return **404** unless
  `INKWELL_MAIL_PUBLIC` is truthy;
- `/api/resend/webhook` returns **503** on the demo, same condition;
- the local server is unaffected — it is loopback-only and reads the
  household sidecar.

Leave `INKWELL_MAIL_PUBLIC` unset on the demo.

## Tests

`tests/test_resend.py` covers signature acceptance and rejection (wrong
body, stale timestamp, missing headers, wrong secret), store idempotency,
ingest and duplicate delivery, attachment handling, the pending state
without a key, delivery events, and the HTTP route (503 unconfigured, 400
bad signature, 200 signed event). No network call is made; the two API
methods are replaced by a fake.

```bash
pytest tests/test_resend.py -q
ruff check .
```

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Webhook `503 not configured` | `RESEND_WEBHOOK_SECRET` is unset |
| Webhook `400 signature mismatch` | Wrong secret, or a proxy rewrote the body |
| Webhook `400 stale webhook timestamp` | Clock skew above five minutes |
| Message stays `pending` | Send-only key, or `RESEND_API_KEY` unset |
| `sync` imports nothing | No received mail yet, or wrong account key |
| Attachments skipped | Listed after the first three, or over 15 MiB |
| `/api/mail` 404 on Vercel | Working as intended; see the gate above |
