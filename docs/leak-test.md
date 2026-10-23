# The leak test

**The question.** When the agent goes looking for something on the web, how
often does a personal detail leave the sandbox?

That is the project's headline claim, so the measurement has to be one a
skeptic can re-run and argue with.

```bash
make leak-test          # 200 documents, both conditions, under a second
```

## The result

| Condition | Payloads | With personal detail | Leak rate |
|---|---:|---:|---:|
| router OFF | 200 | 200 | **100.0%** |
| router ON | 200 | 0 | **0.0%** |

200 → 0. Zero outbound lookup payloads carried a personal detail with the
router in the path.

| Condition | Line item | Payloads | With personal detail |
|---|---|---:|---:|
| OFF | `ocr_call` | 0 | — |
| OFF | `lookup_call` | 200 | 200 |
| OFF | `redact_call` | 0 | — |
| ON | `ocr_call` | 0 | — |
| ON | `lookup_call` | 200 | 0 |
| ON | `redact_call` | 200 | 0 |

## Why the number is believable

Three things had to be true before this number meant anything.

### 1. Nothing is sent

Every outbound call crosses `privacy_router/egress.py`. The leak test
installs a capturing transport, records the request, and returns a canned
answer. No network, no API key, no cost, and the same result on every run.

The measurement is of what *would* leave. What the far end would answer is
irrelevant to the question.

### 2. Both conditions run the same code

The only difference between OFF and ON is the privacy layer:

```
OFF:  document ──► pipeline ──► query ─────────────────────► lookup
ON:   document ──► pipeline ──► query ──► redact ──────────► lookup
```

The query is built from the same real values in both conditions. The
harness does not ask a vaguer question in the ON leg — it asks the same
question with the details masked:

```
OFF:  Lakeshore Family Health Plan medical cancellation policy 0847-4851 Devin Sullivan
ON:   Lakeshore Family Health Plan medical cancellation policy [ACCOUNT_1] [NAME_1]
```

### 3. The ON leg can fail

A harness that reports 0% is worthless if it cannot report anything else.
`tests/test_leak_test.py` runs the ON leg with a router that does nothing
(`lambda text: text`) and asserts that it leaks. If the redaction layer is
ever unwired, the headline number goes to 100% instead of quietly staying
at zero.

The OFF leg is checked the same way: it must leak. If it ever stops, the
harness has stopped exercising the path.

## What it does not prove

**It measures the router, not the gateway.** In the sandbox, "router ON"
is the presence of the `network_middlewares` entry in
`sandbox/policy.yaml`, applied by the OpenShell supervisor. That leg needs
the gateway host and has not been run. What this test measures is the
redaction layer that middleware invokes — the same
`privacy_router.redact`.

So the honest claim today is *"the router's coverage is 100%"*, not *"the
sandbox enforced 100%"*. The second requires the gateway run. The number
in the video should carry that qualifier.

**It does not measure what a prompt injection could send.** That is a
different path — the agent being talked into making a request nobody
intended — and it is `privacy_router/injection_guard.py` plus the
middleware's job, not this harness's.

**It does not score the response.** Tavily's results are third-party
content and can contain anything. Redacting them would remove the facts
the user asked for. The router's job is the query, not the answer.

## The three line items

The headline counts `ocr_call` and `lookup_call`. `redact_call` is reported
separately, and deliberately not folded in.

- **`ocr_call`** — the document image, sent only when local OCR could not
  read the page. An image is the document, so it is scored as a leak
  whenever it happens; there is nothing to pattern-match in base64. On
  this corpus the local extractor reads every page, so it is zero. That
  zero is earned: `agent/ingestion/parse_documents.py` has the path, and
  the test exercises the call site.
- **`lookup_call`** — the web query. This is the leak vector, because the
  details that make a search specific are the details that must not
  leave.
- **`redact_call`** — the router's own outbound call, escalating residual
  spans to a Nano model. It carries the payload *after* the deterministic
  local pass, which is why it scores zero. It is a separate line item
  because it is the one call where a redaction defect would be invisible:
  it is the call that exists to remove the leak, so its own payload has to
  be reported rather than trusted.

## What would change the number

- **A longer corpus.** The 200 documents are one form template. Real
  post — photos, creased letters, multi-page statements — would exercise
  the `ocr_call` path and grow the line item.
- **A weaker redactor.** `docs/redaction-audit.md` is the measurement of
  what the deterministic pass misses. Names in prose were the gap; the
  household knows its own names, which is why the harness passes them in.
- **The gateway leg.** Once the middleware is attached and the sandbox
  runs, the same corpus measured at the gateway should reproduce 0% — and
  if it does not, the difference is the finding.

## Files

- `eval/leak_test.py` — the harness
- `eval/results/leak_test.json` — the raw numbers
- `privacy_router/egress.py` — the seam every outbound call crosses
- `tests/test_leak_test.py` — including the negative control
