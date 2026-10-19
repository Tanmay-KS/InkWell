# Synthetic document corpus

**Generated, not committed.** `eval/synthetic_docs/` is reproducible:

```bash
make synth
```

The generator is `eval/gen_synthetic_docs.py`. With the default seed
(`20261019`) it produces byte-identical documents, so the leak test can be
re-run and compared against a stored baseline.

## What is in a document

Each document is one of six kinds — insurance, membership, subscription,
licence, medical, utility — and each carries the things that actually
leak:

| Field | Example shape |
|---|---|
| Holder name | `Sam Rivera` |
| Account / policy number | `1234-5678`, `POL-482913-Z` |
| Address | `4128 Fenwick Street, Northgate, OR 97418` |
| Email | `sam.rivera@example.com` |
| Phone | `(503) 555-0148`, `+1 503 555 0148` |
| Renewal date | ISO, with a spread from 40 days past to 400 days ahead |
| Amount | `$12.00` – `$480.00` |

Every value is fake and deliberately generic, but the *shapes* vary —
grouped, dashed, bare, alphanumeric, with and without a country code —
because a router that only recognises one format is not a router.

## Files

```
doc-0001.txt      the document text
doc-0001.json     ground truth — every field, for scoring
doc-0001.png      a rendered "scan" (only with --render)
manifest.jsonl    one JSON object per document, in order
```

The ground truth is what makes `eval/ocr_bench.py` possible: instead of
measuring string similarity, it asks whether the extracted text actually
contains the renewal date, the account number, the provider, the holder,
and the amount.

## Rendering images

```bash
python eval/gen_synthetic_docs.py --out eval/synthetic_docs --count 200 --render
```

Needs Pillow (`pip install pillow`). The rendering is deliberately crude —
monospaced text on a light background, a slight rotation, some grain — so
that a vision model is doing more than reading a text file.

The sandbox image already includes `tesseract-ocr` and `poppler-utils`
(`sandbox/Dockerfile.hermes`), which is the local candidate in the
benchmark.

## Regenerating with a different seed

```bash
python eval/gen_synthetic_docs.py --out eval/synthetic_docs --count 200 --seed 12345
```

Keep the seed stable once the leak-test baseline is recorded, or the
baseline is not comparable.
