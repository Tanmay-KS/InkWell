# Phase 4 packaging review

**Date:** 2026-10-28
**Scope:** the Phase 4 exit condition — *a stranger could open the repo and
demo URL and understand it in five minutes.*
**Method:** one pass over the interface and the demo as a first-time user,
one pass over the README and docs as a reviewer who has not seen the code,
then a joint read of the result.

This file is the record of the cross-review, including what was wrong when
the review started.

## Person A reviews Person B: interface and demo

### What was found

| # | Finding | Severity | Resolution |
|---|---|---|---|
| A1 | `/cancel item-…` was swallowed by the generic `"cancel"` keyword check and answered with the item's current status instead of recording the change. The slash-command branch below it was unreachable. | **high** — the visible command did not work | Slash commands are parsed before the keyword checks; regression test added. |
| A2 | A fresh item displayed as `new` in chat and in the web list, because the item lifecycle (`TrackedState`) was used as the subscription status. | **medium** — looked like an error to a new user | The display status is the current fact, else `active` (or `dismissed` for a dismissed lifecycle). |
| A3 | There were no interface tests at all. | **high** | `tests/test_bot_handlers.py` and `tests/test_web_interface.py`: 35 cases covering the loopback guard, the demo fixture, chat, status, upload, traversal, size limits and the slash-command regression. |
| A4 | The server could return a 500 on a malformed `Content-Length`, a non-object JSON body, or an unreadable upload. | **medium** | Each is now a 400/413/422 with a JSON error; the upload is wrapped so a bad file cannot kill the server. |
| A5 | There was no `make` target for the interface, so the README could not point a stranger at one command. | **medium** | `make web` and `make demo-web`. |
| A6 | Static responses had no security headers. | **low** — loopback only, but cheap to fix | `nosniff`, `no-referrer`, a same-origin CSP, `frame-ancestors 'none'`. |
| A7 | The bot docstring had a duplicated line and a trailing "Phase 4 implementation." marker. | **low** | Removed. |
| A8 | The demo had no written tour, and the README did not explain why there is no hosted URL. | **medium** — the exit condition is about understanding | `docs/demo.md` and a "Try it in five minutes" section in the README. |

### Checks that passed

- `make demo-web` starts with no credentials in the environment and prints
  `http://127.0.0.1:8765`.
- The header says **Demo · fictional data**; the seeded providers are
  invented and the database is a temporary directory.
- The demo never reads `~/.hermes/ledger_memory.db`; `serve(..., demo=True)`
  creates its own database and deletes it on exit.
- `Host: evil.example` returns 403. A cross-origin `Origin` returns 403.
  `GET /../pyproject.toml` and `/%2e%2e/server.py` return 404.
- There is **no login**, so "logged-out" is the only mode. The demo was
  started with no API keys, no `.env` values loaded by the server, and no
  existing database; every route worked.
- Upload of a plain-text notice extracts locally, tracks the item, and
  returns a draft. An upload with no date returns 422 with a message a
  person can act on.
- Cancelling an item removes it from *Coming up* but keeps it in the
  household list with a `cancelled` chip; reactivating restores it.

### Deliberate non-goal

A hosted demo URL. The server is loopback-only by construction and the
household database has no authentication layer. Publishing it would be a
privacy regression in a project whose main claim is privacy. The README and
`docs/demo.md` say this plainly instead of shipping a fragile tunnel.

## Person B reviews Person A: README and docs

### What was found

| # | Finding | Severity | Resolution |
|---|---|---|---|
| B1 | The status block said "Phases 0–3 built" and did not mention packaging. | **medium** — a reviewer's first paragraph was stale | Updated to Phases 0–4, with the demo, feedback and review links. |
| B2 | The README had no runnable interface path and no explanation of the demo boundary. | **high** for the exit condition | Added "Try it in five minutes" and the demo document. |
| B3 | The friction log is the required feedback artifact, but there was no distilled submission section. | **high** — submission requirement | Added `docs/feedback.md`, organised by theme with recommendations, linking every claim back to an F-number. |
| B4 | The repository layout still described `interface/` as "Telegram/Slack bot + web view" without naming the runnable server. | **low** | Layout now names `interface/web/server.py`, `index.html`, `app.js`. |
| B5 | The licence section did not link the feedback artifact. | **low** | Added a short Feedback section. |
| B6 | Setup documents the sandbox path but not the zero-credential demo path. | **medium** — a stranger may not have Docker or an API key | The demo section is now the first runnable instruction; it needs neither. |

### Checks that passed

- `LICENSE` is the full Apache-2.0 text and `pyproject.toml` declares
  `Apache-2.0`. *(Changed to MIT on 2026-10-29; the review above is the
  Phase 4 record.)*
- Every model ID in the README comes from the pinned catalog; the models
  table matches `docs/model-catalog.md`.
- The three claims each link to their evidence, and each evidence artifact
  is committed (`eval/results/*.json`, `docs/results.md`, charts).
- The README states the blocked live legs (gateway, serverless job, Phase 0
  verifier) instead of implying they are green.
- All `make` targets referenced by the docs exist in the `Makefile`
  (`web`, `demo-web`, `demo`, `leak-test`, `injection-demo`, `results`,
  `memory-eval`, `bench`, `job-status`).
- `make help` lists both new targets with one-line descriptions.

## Joint read: the five-minute test

A cold reader, no context, no credentials:

| Minute | What they do | What they should understand |
|---|---|---|
| 0:00 | Open the README. | What Ledger is, that it is privacy-routed, and that the live sandbox proofs are pending. |
| 0:45 | `make demo-web`, open the URL. | The product: upcoming dates, a household list, a chat box, an upload box. |
| 1:30 | Cancel Orchard Fitness. | Memory with updates: the item leaves *Coming up* but stays in history. |
| 2:15 | Upload a notice. | Local extraction, local redaction, a draft the user reviews. |
| 3:00 | Read *The three claims*. | The leak test, the injection block, the memory evaluation, each with an evidence link. |
| 4:00 | Skim `docs/feedback.md`. | What was hard and what we would tell the platform teams. |
| 5:00 | Skim the runbooks and the friction log. | The boundary: what is observed, what is implemented but unobserved, and why. |

Measured on this pass: about 4 minutes to the end of the claims table, and
the demo started in under two seconds. The one thing that still requires a
second document is the sandbox procedure; that is inherent to the missing
gateway host and is signposted from the status block.

## Residual risks

1. **No hosted URL.** Accepted. See above.
2. **The live gateway and scheduler legs are unobserved.** The README, the
   runbooks and the results page all say so. This is a submission boundary,
   not a hidden failure.
3. **The interface is single-user and single-threaded.** It is a demo, not
   a server. Binding it to anything but loopback raises `ValueError`.
4. **The demo fixture is dated relative to `date.today()`.** It stays
   coherent when run on any day; the dates are not fixed constants.
5. **The upload path trusts the local file extension only for the
   temporary filename.** `process_document` sniffs the media type; an
   unrecognised file returns 422 rather than crashing.

## Sign-off

- [x] Person A: interface and demo reviewed; A1–A8 fixed and tested.
- [x] Person B: README and docs reviewed; B1–B6 fixed.
- [x] Joint: the five-minute path works with no credentials.
- [x] `ruff` clean, full test suite green.
- [x] Apache-2.0 licence present and referenced. *(Now MIT; see `LICENSE`.)*
- [x] Feedback section written and linked.
