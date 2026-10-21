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

| Candidate | Field recovery | Mean | p95 | Tokens (in/out) | Cost |
|---|---:|---:|---:|---|---:|
| `nvidia/Nemotron-3.5-Super-VL` | **99.0%** | 3.98s | 5.50s | 29,340 / 16,590 | $0.0237 |
| `zai-org/GLM-5.3-Flash` | 100.0% | 3.54s | 4.22s | 37,100 / 5,881 | $0.0085 |
| `deepseek-ai/DeepSeek-V4.1-Flash` | 100.0% | 2.86s | 3.67s | 18,460 / 4,885 | $0.0114 |
| `Qwen/Qwen3.8-27B` | 100.0% | 2.50s | 3.16s | 29,180 / 4,110 | $0.0255 |
| `openbmb/MiniCPM-V-4_5` | 100.0% | 4.25s | 5.34s | 10,340 / 7,568 | $0.0152 |
| `local-rapidocr` (zero egress) | 93.0% | 1.33s | 1.57s | — | $0 |
| `local-text` (baseline) | 100.0% | — | — | — | — |

Per-field recovery for Super-VL: `renewal_date` 100%, `account_number`
100%, `provider` 100%, `holder` 100%, `amount` 95%.

### Read this honestly

**Super-VL is the weakest of the five vision models on this corpus, and
it costs the most per document of the mid-priced ones.** GLM-5.3-Flash
matched it on quality at 2.8× lower cost.

Three things keep Super-VL as the choice:

1. **The gap is one field on one document out of twenty** — `amount` at
   95% is 19/20, which is inside the noise of a 20-document sample.
2. **It is the Nemotron model**, and the project's cost/latency table in
   Phase 3 is a Nemotron tier table. A Super-tier that is also Nemotron
   keeps the story and the numbers consistent.
3. **It is the cheapest model from a major lab that is both multimodal
   and 1M-context.** The Phase 3 benchmark will show whether that
   matters.

**Caveat that matters more than any of the above:** this corpus is
cleanly rendered text. Every model reads it well. The absolute numbers
here will not transfer to photographs of real paper, and the *ranking*
may not either. The bench is a Phase 0 de-risking tool, not a
production accuracy claim.

**If the decision has to be revisited**, the numbers to argue with are
in `eval/results/ocr_bench.json`, and the model to revisit it with is
`zai-org/GLM-5.3-Flash`.

---

## The local fallback

**`local-tesseract` was SKIPPED** — the binary is not installed on the
development host (`which tesseract` → nothing). It is baked into the
sandbox image (`sandbox/Dockerfile.hermes:27`), and it must be measured
there before Phase 1 relies on it.

`local-rapidocr` was installed as a pip-only, no-sudo, zero-egress
alternative. It recovers 93.0% of fields at 1.33s and no cost. That is
the number the privacy architecture rests on, and it is why the bench
reports a SKIPPED row rather than hiding a missing candidate.

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
| `provider` | 19/19 (100%) | 0 | 19 | 0 | 0 |
| `renewal_date` | 19/19 (100%) | 0 | 19 | 0 | 0 |
| `amount` | 19/19 (100%) | 0 | 19 | 0 | 0 |
| `account_number` | 19/19 (100%) | 13 | 6 | 0 | 0 |
| `holder` | 19/19 (100%) | 0 | 19 | 0 | 0 |

The model **reproduces every placeholder exactly** — 13 out of 13, with
**zero hallucinations**. That was the risk: a model asked for an account
number and shown `[ACCOUNT_1]` might return a plausible-looking number
instead, and the agent would then draft a cancellation email with the
wrong account number. It does not.

So under Option C the Super-tier call carries **no personal detail at
all**, and the claim holds literally with no exception to disclose.

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
