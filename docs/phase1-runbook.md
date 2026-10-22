# Ledger — Phase 1 runbook

**Phase 1 — Core Pipeline (Oct 21–23)**
**Goal:** a working end-to-end agent loop: document in → deadline flagged → draft action out.

**Exit condition:** *feed a document in, get a flagged deadline and a draft
action out the other side, running inside the sandbox.*

Phase 0 left the infrastructure verified. The pipeline then landed on the
`agents` branch: `agent/` is ~2,500 lines with a working `agent/pipeline.py`
and an `agent/demo.py` exit-condition runner, and `make demo` produces a
deadline and a draft for every document it is given.

This runbook is therefore two things at once: the **sandbox contract** the
pipeline is built against (§1 — still the authority, and still what a
reviewer checks the code against), and the **status of Phase 1** (§2, §3, §5).
Where the two disagree with the code, the code wins and the runbook is
wrong.

---

## 1. The sandbox contract

**This is the constraint on the pipeline, and it is not optional.** The plan
asks for the pipeline to run "on the real sandbox from day one, not bolted
on later". Code written against a normal host and moved into the sandbox
later will not work, because three things differ: the credential is a
placeholder, the filesystem is closed, and the network is a three-host
allowlist.

### 1.1 Where the code runs

| | |
|---|---|
| Location | inside the `ledger-hermes` OpenShell sandbox |
| Identity | UID 10000, group 10000 (`hermes`). **Not root** — OpenShell rejects a root workload outright |
| Working directory | `/sandbox` (the image's WORKDIR) |
| Python | `/opt/hermes/.venv/bin/python` — the venv interpreter is the real path the OpenShell proxy matches when it binds a credential to a binary |

### 1.2 Model access

- `NEBIUS_API_KEY` in the environment is an **opaque placeholder**. The real
  key never enters the sandbox; OpenShell substitutes it at the network
  boundary, and only for `api.tokenfactory.nebius.com:443`.
- The same placeholder sent anywhere else is refused with
  `credential_endpoint_mismatch`. That is the enforcement, not an accident.
- **Model IDs come from the environment** (`LEDGER_MODEL_NANO`,
  `LEDGER_MODEL_SUPER`, `LEDGER_MODEL_ULTRA`, `LEDGER_MODEL_OCR`,
  `LEDGER_MODEL_REDACT`). Never hard-code one; the ID spelling is not
  stable across Nebius's own sources (F-002, F-017, F-024).
- **Do not set `NEBIUS_BASE_URL` inside the sandbox.** It makes Hermes aim
  at another host, which the credential is not bound to. That is a useful
  test — see `sandbox/verify.sh` check 5 — and a broken pipeline.

### 1.3 Where state lives

| Path | Use | Lifetime |
|---|---|---|
| `/opt/data` | the only persistent, writable place. `HERMES_HOME=/opt/data`. Memory DB, sessions, skills, cron, logs | survives sandbox recreation |
| `/sandbox` | scratch — working files, the uploaded document | wiped on recreation |
| `/tmp` | ephemeral scratch | wiped |

Nothing else is writable. `landlock` additionally protects `/.openshell`, and
the sandbox supervisor's own CA material is mounted read-only.

> **Never bind-mount a host directory for state.** Hermes keeps sessions and
> memory in a WAL-mode SQLite database, and a bind mount that crosses a VM
> boundary can corrupt it *silently* — `PRAGMA integrity_check` still passes
> while concurrent writers destroy the file (F-008). The named Docker volume
> is the only sanctioned way to carry state.

### 1.4 Egress — exactly three destinations

| Host | Path | Used for |
|---|---|---|
| `api.tokenfactory.nebius.com` | `/v1/**` | every model call: OCR, extraction, redaction, drafting |
| `api.tavily.com` | `/search` | web search, via `search/tavily_client.py` |
| `api.telegram.org` | `/**` | the messaging gateway (Phase 4) |

Everything else is denied. The policy is **enforcing**, not staged: a
request to any other host gets `policy_denied` before it leaves. DNS is
answered by the supervisor with placeholder addresses for allowed hosts only.

### 1.5 There is no router yet

`sandbox/policy.yaml`'s `network_middlewares` map is empty until Phase 2, so
**redaction has to happen in-process**, inside the sandbox, in Phase 1. Do
not design the pipeline assuming a network-level filter will catch
something later.

### 1.6 The ordering constraint — extract locally, redact locally, then read

This is the decision of record (`docs/ocr-decision.md`), and it is
**validated**, not assumed: `make redacted-extract` runs the real pipeline
over the real corpus with the real model and the model reproduces every
placeholder exactly, 13 out of 13, with zero hallucinations.

```
document bytes
  └─► local extraction INSIDE the sandbox → plain text
        └─► privacy_router.redact.local_redact() INSIDE the sandbox → placeholders
              └─► Super-VL receives ONLY the redacted text → structured fields
                    └─► placeholder → value resolved INSIDE the sandbox for the user's draft
```

Three consequences for the pipeline:

1. **The document image does not go to the model.** Local extraction
   (RapidOCR, 93.0% field recovery, 1.33s, zero egress) produces text
   first. Super-VL is a *Super-tier* model, so the raw document reaching it
   would make the headline privacy claim false.
2. **The placeholder→value map never leaves the sandbox.** It is resolved
   at the last moment, when the draft is shown to the user, whose own chat
   app is inside their boundary. `privacy_router.redact.RedactionMap`
   (Phase 1, Person A) exists for exactly this.
3. **The draft contains real values; the model's output does not.** The
   model sees `[ACCOUNT_1]`. The user sees the account number. Nothing in
   between carries the real value.

**Documented exception.** If local extraction is not good enough for a
page, Super-VL reads the image directly — scoped to individual
low-confidence pages, countable, and reported as its own line item in the
Phase 2 leak test. RapidOCR reports per-line confidence, so the fallback is
measurable.

### 1.7 Failure modes you will hit, and what they mean

| What you see | What it is | What to do |
|---|---|---|
| `policy_denied` | the code reached a host that is not on the list | add the host to `sandbox/policy.yaml` **deliberately**, then re-apply the policy. Do not work around it in code, and do not widen the path rule |
| `credential_endpoint_mismatch` | the placeholder was sent somewhere the credential is not bound to | almost always a wrong base URL or a hard-coded host. Check for a literal hostname in the request code |
| `Permission denied` on a write | the path is not in the filesystem policy | state belongs in `/opt/data`; scratch in `/sandbox` |
| `403` from the model | egress was fine, the request shape was wrong | read the response body; the model is not the problem |
| `No space left on device` | the image is small and `/opt/data` is a volume | check the volume is attached; the image is not a place to write |

### 1.8 What must never happen

- **The real API key is never in the sandbox**, so there is nothing to
  leak — but equally, never read a key from a file, never log the
  placeholder, and never echo a request header.
- **Never log extracted document text.** It contains the personal details
  the whole project exists to protect. Log counts, ids, and field names.
- **Never put a personal detail in a URL, a query string, or an exception
  message.** They travel further than the body does.

---

## 2. What to build, file by file

Person B's files, in dependency order. Each row is a checklist item.

### 2.1 `agent/ingestion/parse_documents.py` — document → text

- [ ] Local extraction first: RapidOCR on the rendered page, per-line
      confidence retained
- [ ] `local_redact()` on the extracted text, **inside the sandbox**
- [ ] Keep the placeholder→value map; it is needed in step 2.4
- [ ] Only then, if local confidence is too low, fall back to the model
      reading the image — and count it
- [ ] Chunks (if any) are cut from the **redacted** text
- [ ] Model IDs read from the environment, never hard-coded

### 2.2 `agent/ingestion/parse_email.py` — email → text + attachments

- [ ] Strip the quoted reply chain **before** anything else — it is the
      single largest accidental-leak surface in email ingestion
- [ ] Attachments go through the same path as documents
- [ ] `Message-ID` is the stable identity, so the same email is not
      processed twice

### 2.3 `agent/memory/hermes_memory.py` — the household picture

- [ ] The structured sidecar lives under `/opt/data` (SQLite, WAL mode)
- [ ] The narrative household picture goes into Hermes's `MEMORY.md`
- [ ] The handled log is **append-only** — an audit trail that can be edited
      is not an audit trail
- [ ] `dismissed` is a real state: the user said "I know, leave it alone",
      and it must not come back

### 2.4 `agent/deadlines/flag_deadlines.py` — text → dated obligations

- [ ] Every deadline carries **evidence**: the exact span of source text
      that supports it. A deadline with no evidence is a hallucination
- [ ] Model output is validated against the source text before it is
      accepted — a date the model produced but the text does not contain is
      dropped, not kept
- [ ] Past deadlines are kept and flagged: a document that arrived late may
      still contain something the user must act on
- [ ] Dates are parsed from the document, never from the model's confidence

### 2.5 `agent/actions/draft_actions.py` — deadline → a draft the user can send

- [ ] A **draft, never an action**. Ledger does not send anything.
- [ ] The body is written to be sent as-is: no `[YOUR_NAME]`, no
      meta-commentary, a specific ask with a date attached
- [ ] The placeholders are resolved **here**, inside the sandbox, at the
      moment the draft is shown — via `RedactionMap.resolve()`
- [ ] The resolved values are never written to a log, a file outside
      `/opt/data`, or a network request

### 2.6 `agent/memory/update_tracker.py` — facts that change

- [ ] A fact has a value, a `valid_from`, and a `valid_to`
- [ ] A contradicting write closes the old fact's window and opens a new
      one; nothing is deleted
- [ ] A fact that *agrees* with the current value is a restatement, not a
      new version
- [ ] Low confidence does not close a high-confidence fact

### 2.7 `eval/leak_test.py` — the Phase 2 measurement, built now

Not a Phase 1 deliverable, but the pipeline it needs is Phase 1's. The
capture point is the outbound request, not the pipeline's return value.

---

## 3. Person A's Phase 1 work

- [x] The sandbox contract — this document, §1
- [x] `privacy_router/redact.py` — `RedactionMap` and `redacted_text`, so the
      draft can be resolved locally without the map travelling with the text
- [x] `sandbox/run_pipeline.sh` — one command that puts a document through
      the pipeline **inside the sandbox** and prints the deadline and the
      draft. This is what makes "from day one" checkable
- [x] `sandbox/policy.yaml` — `redact_models` names the decided Super ID
      (`nvidia/Nemotron-3.5-Super-VL`), not the text-only model of the
      same tier
- [x] The leak review of the pipeline as it lands — §4. Reviewed against
      the `agents` branch on 2026-10-22; two low-severity findings are in
      the friction log as F-038 and F-039
- [x] Keep the egress policy in step with what the pipeline actually calls
      — Done for Phase 1. The pipeline contacts exactly one host,
      `api.tokenfactory.nebius.com`. `api.tavily.com` and
      `api.telegram.org` are allowed but unused yet — Tavily is for the
      Phase 2 leak test, Telegram for the Phase 4 interface. The allowlist
      is ahead of the code, which is the right way round: a host the code
      cannot reach is not a leak
- [x] Run `bash sandbox/run_pipeline.sh <doc> --check` on the gateway host
      — Passed 2026-10-22: gateway reachable, sandbox `ledger-hermes`
      present, the nebius provider attached, a real model call from inside
      the sandbox (`MODEL-OK OK`), and `/opt/data` writable. The first
      attempt failed on a null `content` in the model's reply — a harness
      bug, not a network one (F-040, fixed)

---

## 4. The leak review

Run against the pipeline as it lands. Each item is a place personal detail
can escape without anyone deciding to let it.

Reviewed against the `agents` branch on 2026-10-22, with the evidence for
each answer.

- [x] **Extracted document text in a log.** Not the text, the counts.
      — Clean. `Document.text` is the *redacted* text by construction —
      `redacted_text()` is what fills it — and `agent/demo.py` prints that
      one. The raw extraction exists only as a local variable inside
      `_extract_local()`.
- [x] **A personal detail in an exception message or a traceback.** These
      go to stderr, into logs, and into the chat platform.
      — **Fixed, F-039.** All four sites that built a warning from an
      exception — two in `agent/pipeline.py`, two in
      `agent/ingestion/parse_documents.py` — now report
      `type(exc).__name__` and never `str(exc)`. The household picture *is*
      the personal data, and the rule that already applies to secrets now
      applies here: the exception *type*, never its message.
- [x] **Extracted text written outside `/opt/data` and `/sandbox`.**
      — Clean. Document bytes go to a `tempfile.TemporaryDirectory()`
      (`/tmp`, ephemeral). Email attachments are written with
      `Path(filename).name`, so an attachment cannot escape its directory.
- [x] **Raw content sent to a model that is not the redaction tier.**
      — Clean. The local extractor is used *first regardless* of the model
      argument, deliberately: "a page that can be read locally must be read
      locally, because the alternative is the raw document leaving the
      sandbox". The only model endpoint the pipeline names is
      `api.tokenfactory.nebius.com`.
- [x] **A personal detail in a Tavily query.** The query string is the
      part that travels; `include_raw_content` and `include_answer` are the
      other two, and both stay off (`search/tavily_client.py`).
      — Not applicable in Phase 1: the pipeline never calls Tavily. The
      client exists, but nothing in `agent/` uses it yet. See the egress
      note in §3.
- [x] **A personal detail in a URL path, a filename, or a hostname.**
      — **Fixed, F-038.** The household item's `source` column held the
      document's path, and a file the user named themselves put a household
      name into the memory database. `build_item()` now stores only the
      basename, put through the same redaction pass as the document, with
      the directory dropped. `demo.py` still echoes the path the user typed
      on the command line, back to the user's own terminal — that is not a
      new disclosure.
- [x] **The placeholder→value map** written anywhere but memory.
      — Clean. `agent/memory/` contains no reference to `redactions` at
      all. The map lives on `Document.redactions` and is read in
      `agent/pipeline.py` only; it is never persisted. `item_id` is a
      SHA-256 digest rather than the values it is derived from.
- [x] **The resolved draft** written to a log, a file, or a request.
      — Clean. The draft is printed for the user and held in
      `result.action`. It is not written to a file and not sent anywhere.
- [x] **The redaction itself** — is the redactor catching what it claims?
      `make redaction-audit` measures the bound. Re-run it when the
      redactor changes.
      — Measured. See the table below.

### What the redaction actually catches now

The bound has moved a long way since the first audit. `make redaction-audit`
over 200 documents:

| Value | Deterministic | With household names |
|---|---:|---:|
| `holder` | 167/200 (83.5%) | **200/200 (100%)** |
| `email` | 200/200 (100%) | 200/200 |
| `phone` | 200/200 (100%) | 200/200 |
| `account_number` | 200/200 (100%) | 200/200 |
| `policy_number` | 25/25 (100%) | 25/25 |
| `street`, `city`, `postcode` | 200/200 (100%) | 200/200 |

Only 33/200 holder names survive the deterministic pass, and the
household-names path — the memory layer telling the redactor who the
household is — catches the rest. The alphanumeric identifiers
(`POL-4825232-I`), the city and the postcode, which were the first
audit's gaps, are all covered now.

**The one place personal data is stored in the clear, by design:** the
household picture. `household_item` holds the real holder and the real
reference, because a draft without the account number is useless. It is
inside the boundary, in `/opt/data`, and it never leaves. That is the trade
the architecture makes, and it is the reason the *model* never sees it.

---

## 5. Definition of done

- [x] A document goes in and a flagged deadline and a draft action come
      out — `make demo` does this on the host, 3/3 documents
- [ ] …and the same works **inside the sandbox**, which is the exit
      condition as written. The plumbing is proven (see §3) — gateway,
      sandbox, provider, a real model call, writable `/opt/data` — but
      `bash sandbox/run_pipeline.sh <document>` has not yet run the
      pipeline itself against a live sandbox
- [x] The draft's personal details were resolved inside the boundary and
      appear nowhere else — `make demo` prints the real account number in
      the draft and the placeholder in the model's view
- [x] The pipeline's egress is exactly what `sandbox/policy.yaml` allows —
      no undocumented host, no widened path
- [x] `make leak-test` runs against the pipeline and reports a number — see §6
- [x] The leak review in §4 is done, and the findings are in the friction log
      — nine surfaces reviewed; F-038 and F-039 found, fixed, and recorded
- [ ] `bash sandbox/verify.sh` on the gateway host still exits 0
- [x] The friction log has the Phase 1 entries from both people
      — Person A: F-040 and F-038/F-039; Person B: F-034 through F-037

---

## 6. The measured result

`make leak-test`, 200 documents, 2026-10-22, `eval/results/leak_test.json`.

| Condition | Documents | Payloads with personal detail | Leak rate | Bytes/doc |
|---|---:|---:|---:|---:|
| router OFF — no redaction | 200 | 200 | **100.0%** | 458 |
| router ON — redaction applied | 200 | 0 | **0.0%** | 404 |

**The leak rate falls from 100.0% to 0.0%.** Every one of the 200 payloads
carried a personal detail before the redaction step, and none of them does
after it. The redaction also costs 12% of the payload size, because the
values it replaces are longer than the placeholders that stand in for them.

Per value, with the router ON:

| Value | In the corpus | Leaked with router OFF | Leaked with router ON |
|---|---:|---:|---:|
| `holder` | 200 | 200 | 0 |
| `email` | 200 | 200 | 0 |
| `phone` | 200 | 200 | 0 |
| `account_number` | 200 | 200 | 0 |
| `policy_number` | 25 | 23 | 0 |
| `street` | 200 | 200 | 0 |
| `city` | 200 | 200 | 0 |
| `postcode` | 200 | 194 | 0 |

The two values that do not read 200/200 with the router OFF —
`policy_number` (23 of 25) and `postcode` (194 of 200) — are documents
where the OCR did not recover the value at all. They were absent from both
payloads, so they are a coverage gap in the *extractor*, not a leak. That
distinction matters: a value that was never read cannot leak.

And the values the router must keep, are all kept:

| Value | In the corpus | Kept with router OFF | Kept with router ON |
|---|---:|---:|---:|
| `renewal_date` | 200 | 200 | 200 |
| `amount` | 200 | 200 | 200 |
| `provider` | 200 | 200 | 200 |

**The self-check.** On all 200 documents the ON payload with its
placeholders resolved is byte-identical to the OFF payload. The redaction
is a pure text transform, so the two conditions differ by the redaction
step and by nothing else — no OCR variation, no prompt difference, no
sampling.

### What this is and is not

It measures the **model-facing text**: the payload that would be sent. In
Phase 1 the pipeline is entirely local — there is no HTTP client anywhere in
`agent/` — so its current egress is zero and there is no request on the wire
to capture. The measurement is of the text the architecture puts there.

In Phase 2, when the router is a supervisor middleware attached to real
traffic, the same scorer runs against captured request bodies and the ON
condition becomes the middleware's output rather than the in-process pass.
The number to beat is 0.0%.
