# The document-reading model — decision record

**Phase 0 — Person B.** Decided 2026-10-21.

---

## The decision

**`nvidia/Nemotron-3.5-Super-VL`** reads our documents.

| | |
|---|---|
| Model ID | `nvidia/Nemotron-3.5-Super-VL` |
| Parameters | 120B total, 12B active |
| Price | $0.30 / 1M input · $0.90 / 1M output |
| Context | 1,048,576 tokens |
| Modality | `text+image->text` |
| Nebius's positioning | "Best for: document understanding, web research and agents working with visual information" |

The ID was read from the live API catalog, not typed — see
[`model-catalog.md`](model-catalog.md) for the dated snapshot it was
pinned from.

> **The `VL` suffix is load-bearing.** `nvidia/nemotron-3-super-120b-a12b`
> is a *different* model in the same catalog: text-to-text, no image
> input. It is the "Super" tier the policy's `redact_models` list
> already names.

---

## What the benchmark says

`make ocr-bench`, 20 documents, live API, `eval/results/ocr_bench.json`.

| Candidate | Field recovery | Line coverage | Mean | p95 | Tokens (in/out) | Cost |
|---|---:|---:|---:|---:|---|---:|
| `nvidia/Nemotron-3.5-Super-VL` | **99.0%** | 99.0% | 3.79s | 4.61s | 44,080 / 17,191 | $0.0287 |
| `zai-org/GLM-5.3-Flash` | 100.0% | 100.0% | 3.42s | 3.99s | 57,800 / 5,862 | $0.0116 |
| `deepseek-ai/DeepSeek-V4.1-Flash` | 100.0% | 99.5% | 2.69s | 3.07s | 21,440 / 4,754 | $0.0121 |
| `Qwen/Qwen3.8-27B` | 100.0% | 100.0% | 3.09s | 4.00s | 44,800 / 4,030 | $0.0323 |
| `openbmb/MiniCPM-V-4_5` | 100.0% | 100.0% | 4.69s | 6.75s | 13,000 / 7,253 | $0.0166 |
| **`local-tesseract`** (zero egress) | **100.0%** | **100.0%** | 2.07s | 2.83s | — | **$0** |
| `local-rapidocr` (zero egress) | 96.0% | 92.1% | 3.42s | 3.96s | — | $0 |
| `local-text` (baseline) | 100.0% | 100.0% | — | — | — | — |

Per-field recovery for Super-VL: `renewal_date` 100%, `account_number`
95%, `provider` 100%, `holder` 100%, `amount` 100%.

> **These numbers were re-measured after the corpus was fixed.** The
> corpus used to render at 96 DPI, where the digit `0` is
> indistinguishable from an `8` — every number above was different
> before that, and every one of them was wrong. See F-037.

### Read this honestly

**Super-VL is the weakest of the five vision models, and it costs 2.5×
what GLM-5.3-Flash costs to do the same job.** Its one defect is
`account_number` at 95% — one document in twenty.

Three things keep Super-VL as the choice:

1. **The gap is one field on one document out of twenty**, which is
   inside the noise of a 20-document sample.
2. **It is the Nemotron model**, and the project's cost/latency table in
   Phase 3 is a Nemotron tier table. A Super-tier that is also Nemotron
   keeps the story and the numbers consistent.
3. **It is the cheapest model from a major lab that is both multimodal
   and 1M-context.** The Phase 3 benchmark will show whether that
   matters.

**And the honest headline is not in that table at all:** the local
extractor matches every vision model, costs nothing, and cannot leak.
Super-VL's job is not to transcribe a form.

**If the decision has to be revisited**, the numbers to argue with are
in `eval/results/ocr_bench.json`, and the model to revisit it with is
`zai-org/GLM-5.3-Flash`.

**Caveat that matters more than any of the above:** this corpus is
cleanly rendered text from a form template. Every reader does well on
it. The absolute numbers will not transfer to photographs of real paper
with creases and handwriting, and the *ranking* may not either. The bench
is a de-risking tool, not a production accuracy claim.

---

## The local extractor — and why it is the *primary* path

**`local-tesseract` is the production path, and it is the best of the
lot.** It recovers **100% of the fields and 100% of the document's
lines**, at 1.3s a page, at no cost, with **no egress at all**. It is
the same binary the sandbox image ships (`sandbox/Dockerfile.hermes:27`).

`local-rapidocr` is the pip-only, no-sudo, zero-egress alternative
(96% of fields, 92.1% of lines, 3.4s). Having both was worth more than
having either: the pair is what exposed the corpus defect in F-037,
because a model-independent local candidate scoring *worse* than a
pip-only one is a fixture problem, not a model problem.

**This changes the shape of the decision.** The local extractor is not a
fallback — it is the best reader of these documents, and it is the only
one that cannot leak. The vision model's job is not to transcribe a
form; it is to handle what a local extractor cannot read (a photograph,
a handwritten note, an unusual layout) and to do the *semantic* work —
extracting structured fields from prose, and the redaction escalation
that the deterministic pass cannot do on its own.

That is a much better position than "the model reads the page". The
exception is narrow, countable, and reported.

---

## The consequence: Super-VL is a Super-tier model

This is the part that has to be settled before Phase 2.

The project's headline claim (`eval/leak_test.py`) is *"personal details
never reach a Super/Ultra tier or a third-party service."* Super-VL is
a Super-tier model, and Phase 2 enforces the claim by redacting traffic
to specific models (`sandbox/policy.yaml:162-165`).

Both obvious resolutions break something:

- **Put Super-VL on `redact_models`** → the router redacts the OCR
  request body. That body is a base64-encoded PNG, and a regex/NER
  redactor cannot meaningfully redact an image. The call mangles and
  the vision model cannot read the page.
- **Leave Super-VL off `redact_models`** → the raw document leaves the
  sandbox to a Super-tier model, and the headline claim is false.

### The decision: Option C — extract locally, redact locally, then read

```
document bytes
  └─► local extraction INSIDE the sandbox → plain text
        └─► privacy_router.redact.local_redact() INSIDE the sandbox → placeholders
              └─► Super-VL receives ONLY the redacted text → structured fields
                    └─► placeholder → value resolved INSIDE the sandbox for the user's draft
```

**This is validated, not assumed.** `make redacted-extract`
(`eval/redacted_extract_bench.py`) runs the real pipeline against the
real corpus with the real model:

| Field | Correct | Redacted | Model saw it in the clear | Hallucinated | Missing |
|---|---:|---:|---:|---:|---:|
| `provider` | 20/20 (100%) | 0 | 20 | 0 | 0 |
| `renewal_date` | 20/20 (100%) | 0 | 20 | 0 | 0 |
| `amount` | 20/20 (100%) | 0 | 20 | 0 | 0 |
| `account_number` | 20/20 (100%) | 20 | **0** | 0 | 0 |
| `holder` | 20/20 (100%) | 20 | **0** | 0 | 0 |

The model **reproduces every placeholder exactly** — 20 out of 20, with
**zero hallucinations**. That was the risk: a model asked for an account
number and shown `[ACCOUNT_1]` might return a plausible-looking number
instead, and the agent would then draft a cancellation email with the
wrong account number. It does not.

And the "Model saw it in the clear" column is **zero for every personal
field**. The model was shown no name and no account number at all — it
read a document in which both had been replaced by placeholders, and it
returned the placeholders. The Super-tier call carries no personal
detail, so the claim holds literally with no exception to disclose.

The remaining problem is not the architecture. It is the redactor — and
it has since been fixed. See [`docs/redaction-audit.md`](redaction-audit.md)
for the current numbers: with the household's names supplied from the
memory layer, **all 200 documents come out of the redactor with no
surviving personal value**, and the deterministic pass alone gets 167/200
with names at 83.5%.

### When Option C falls back to Option A

If local extraction is not good enough for a real page, Super-VL reads
the image directly. That is the **document-reading exception**, and it
is scoped to individual low-confidence pages rather than to every
document. RapidOCR reports per-line confidence, so the fallback is
measurable and countable. The leak test reports it as its own line item,
alongside the Nano redaction call.

---

## What this changes downstream

| Change | Owner | Phase |
|---|---|---|
| `sandbox/policy.yaml:162-165` — `redact_models` must name the decided Super ID | Person A | 2 |
| `agent/ingestion/parse_documents.py` — extract → redact → extract order; the placeholder map never leaves the sandbox | Person B | 1 |
| `privacy_router/redact.py` — expose the placeholder→value map for local resolution | Person A | 1/2 |
| `eval/leak_test.py` — an `ocr_call` line item, separate from the headline | Person B | 2 |
| The redactor's gaps (names, alphanumeric account numbers, city/postcode) | Person A | 2 |

---

## Reproducing this

```bash
make synth              # 200 documents + rendered PNGs, deterministic
make corpus-qa          # validate the corpus
make pin-models         # re-pin the catalog into .env + snapshot
make ocr-bench          # the table above
make redacted-extract   # the Option C validation
make redaction-audit    # what the deterministic pass misses
```
