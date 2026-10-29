# Feedback

This is the written feedback submission for Ledger. The full, dated,
entry-by-entry record is in [`friction_log.md`](friction_log.md) (F-001 to
F-050); this page is the short version a reviewer can read in a few
minutes.

## What we built

Ledger is a household agent for renewals, deadlines and entitlements. It
reads a notice, extracts the dates and the reference, keeps a history of
facts that change, and drafts the letter or cancellation the user can send
themselves. It never sends anything on its own.

The privacy constraint shaped the architecture more than the feature list:

- documents are extracted locally (tesseract/poppler) first;
- the deterministic redactor runs locally, before any model call;
- the model only ever reads redacted text (Option C);
- every outbound call crosses one seam (`privacy_router/egress.py`), which
  is what makes the leak test measurable;
- the OpenShell middleware sees all allowed egress, not just the calls our
  code wrote, and writes an audit row for every decision.

Three claims are measured, not asserted: leak rate 100% → 0% on 200
documents, memory 100% on 30 update scenarios against a 43.3% update-blind
baseline, and a cost/latency table across the three Nemotron tiers.

## What worked

**Local-first extraction was the right default.** The pipeline runs without
a model call. A renewal notice that says "your policy renews on 2027-03-07"
does not need a language model, and a pipeline that does not need one
cannot leak anything to one. It also made the whole suite runnable offline
in CI, with no secrets.

**A single egress seam made privacy testable.** Because every outbound
request crosses `privacy_router/egress.py`, the leak test can capture the
payload, compare ON and OFF, and run in about two seconds for 200
documents. A privacy property that cannot be measured cheaply will not be
measured often.

**Facts with validity windows beat "remember the latest value".** A status
change closes the old fact's window and opens a new one. The evaluation can
then ask *when* something changed, and the display can answer with the
current value without deleting the history. The 30-scenario evaluation
includes a `never_stated` category that must query the store; an earlier
version short-circuited it and scored 100% by construction (F-046).

**Pinning model IDs from the live catalog removed a whole class of bugs.**
No model ID in the repo is hand-typed. `make pin-models` resolves each role
against `GET /v1/models` and writes a dated snapshot. This was not
theoretical: two models named in the original plan do not exist on Token
Factory at all (F-018, F-024).

**Determinism made the artifacts reviewable.** The corpus, the leak-test
baseline and `docs/results.md` are reproducible. The results packer refuses
to publish a smoke run, and `make results` is byte-identical across runs,
so a reviewer can diff the page instead of trusting a screenshot.

## What was hard

### The model catalog is the source of truth, and every other source drifts

The API catalog, the documentation and the cookbook disagreed about which
models exist, and the same model is published under several casings
(F-017). The cookbook's quickstart used a model the catalog does not serve
and a regional endpoint rather than the global one. A wrong ID returns a
404 that reads like a credential problem, so the first hour of debugging
goes to the wrong layer.

**Recommendation:** make the catalog the only documented source, generate
the docs' model tables from it, and return a distinct error for "model not
found" versus "not authorised". A 404 that names the requested ID and
suggests the nearest catalog match would have saved several cycles.

### Reasoning models spend `max_tokens` on thinking, not on the answer

Super-VL is a reasoning model. On one measured call, 847 of 859 completion
tokens went to `reasoning_content` and 12 to the answer. With a 1024-token
budget it returned an empty string, which the benchmark first scored as
"zero fields recovered" — a score, not an error (F-025). A literal
`/no_think` made it reason *more*; the working control was
`chat_template_kwargs: {"enable_thinking": false}` for the model that
supports it, and a larger budget for the model that does not.

**Recommendation:** document the thinking-budget behaviour per model, and
expose a request-level switch where it exists. An empty response should be
documented as a budget problem, not as a capability problem.

### Long benchmark sessions hit the endpoint in ways a rate limit does not explain

After roughly 250 calls in one session, the whole Token Factory domain
stopped accepting TCP connections while DNS still resolved and other sites
were reachable. It recovered on its own within a day (F-033). This is not
the 429-with-headers failure mode the docs describe, and there was nothing
in the response to read.

**Recommendation:** batch benchmarks, persist raw results, and make a
decision depend on committed evidence rather than on the ability to re-run
that afternoon.

### OpenShell's install and auth paths assume a specific host

The upstream installer supports `.deb`, `.rpm` and snap, and exits on
Arch-family distributions; the RPMs are plain archives, so `setup.sh`
installs from them with `bsdtar` (F-022). The gateway also creates its
Docker client at startup and exits if it cannot, so a stopped Docker daemon
takes down the gateway, not just sandbox creation (F-021). A remote CLI
cannot authenticate without the gateway's client bundle and fails with
`invalid peer certificate: BadSignature` (F-030/F-043).

**Recommendation:** support `bsdtar`/libarchive as an install path, separate
"gateway is running" from "compute driver is running" in `status`, and make
`gateway add` fetch or explain the client bundle instead of silently
reusing local material.

### The middleware registration contract is easy to get wrong

The commented registration block advertised 256 KiB and 500 ms while the
service advertised 4 MiB and 1500 ms; the largest payload (a document image)
would have been rejected before the middleware saw it, and the failure would
have looked like an OCR problem (F-044). A registered middleware that is
down also stops the gateway from starting, so registration has to be last
and reversible (F-005, F-041).

**Recommendation:** have the gateway validate the registration block
against the service's advertised capabilities at registration time, with an
error that names both numbers.

### Benchmark design was harder than the model choice

Several early numbers were wrong because the fixture was wrong: a benchmark
that could not call a vision model, an amount field counted as recovered
no matter what, a truncated rendered page, a corpus that was not
reproducible across dates, and a 96 DPI render where `0` and `8` are
indistinguishable (F-009 to F-012, F-034, F-037). The 96 DPI defect made
the local extractor look worse than the vision models for the entire Phase
0 decision. A model-independent failure is a harness failure.

**Recommendation:** for any OCR or extraction benchmark, publish the
fixture's DPI, page size, font, and a line-coverage metric alongside field
recovery. A field score can hide a dropped line.

### Privacy bugs hide in error paths

The document's own path was stored and printed (F-038), and an exception
message could carry the household picture (F-039). Neither is in the happy
path. The deterministic redactor also missed every holder name, policy
number, city and postcode before it was measured (F-015), and the fixed
version now clears 200/200 documents.

**Recommendation:** treat logs, exception strings, and file paths as
egress surfaces, and test them with the same corpus used for the network
test.

## What we would do differently

1. **Build the measurement before the feature.** The leak test, the
   redaction audit and the line-coverage metric each changed what we
   believed. The earlier they exist, the less rework they cause.
2. **Keep one canonical implementation per phase.** A parallel Phase 2
   build diverged and had to be archived; the merge cost more than the
   parallelism saved. One branch, one owner, a review before merge.
3. **Treat "blocked" as a first-class result.** The live OpenShell gateway
   and the serverless job could not be observed from this host. The repo
   says so in the README, the runbooks and the results page instead of
   claiming a green run. A submission that is honest about its boundary is
   more useful than one that is not.
4. **Do the demo with fictional data from the start.** The local web view
   seeds an isolated database, so the demo can never read the real
   household sidecar. That should have been the first version of the UI,
   not the last.

## Limitations we are not hiding

- The live gateway leg of the Phase 2 claim is blocked on this host: no
  Docker daemon and no matching client certificate. The offline suite, the
  middleware RPC self-test and the fake-gateway egress probe are green; the
  sandbox enforcement is not observed.
- The Nebius Serverless Job is implemented and has a `--status` liveness
  check, but has not been observed firing on a scheduler.
- The local web interface binds to loopback only and refuses non-loopback
  `Host` headers. It is a demo you run, not a hosted URL; hosting it would
  require adding authentication and a real deployment boundary first.
- Phase 0's automated verifier has not produced a green run on this host,
  though the exit condition was met by hand on 2026-10-21.

## Acknowledgements

The friction log is the work of both people on the project. Every entry
names an owner, a phase, what it cost, how it was detected and what was
done. It is deliberately unflattering: the point of the record is that the
next person does not have to rediscover the same failure.
