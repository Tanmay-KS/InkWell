# Ledger — developer entry points.
#
# `make help` lists everything. Every target is safe to re-run.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PY := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
VENV := .venv
SANDBOX := sandbox
EVAL := eval

# The corpus is reproducible only if the reference date is fixed: the
# generator derives every renewal date from it, so a corpus generated on a
# different day differs even with the same seed. This is part of the
# reproducibility contract, not a convenience — the Phase 2 leak-test
# baseline is only comparable across runs if the date does not move.
CORPUS_TODAY ?= 2026-10-20
CORPUS_COUNT ?= 200

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- setup -----------------------------------------------------------------

.PHONY: venv
venv: ## Create the local virtualenv and install dev dependencies
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -e ".[dev,ocr]"
	@echo "venv ready: $(VENV)/bin/python"

.PHONY: install
install: venv ## Alias for venv

# --- quality ---------------------------------------------------------------

.PHONY: lint
lint: ## Run ruff over the repo
	@if [ -x "$(VENV)/bin/ruff" ]; then $(VENV)/bin/ruff check .; \
	else command -v ruff >/dev/null && ruff check . || echo "ruff not installed — run 'make venv'"; fi

.PHONY: fmt
fmt: ## Auto-fix lint issues
	@if [ -x "$(VENV)/bin/ruff" ]; then $(VENV)/bin/ruff check --fix .; \
	else command -v ruff >/dev/null && ruff check --fix . || echo "ruff not installed — run 'make venv'"; fi

.PHONY: test
test: ## Run the test suite
	@if [ -x "$(VENV)/bin/pytest" ]; then $(VENV)/bin/pytest; \
	else command -v pytest >/dev/null && pytest || echo "pytest not installed — run 'make venv'"; fi

# --- Phase 0: OpenShell + Token Factory ------------------------------------

.PHONY: sandbox-setup
sandbox-setup: ## Idempotent OpenShell + provider + image setup (Person A)
	bash $(SANDBOX)/setup.sh

.PHONY: sandbox-up
sandbox-up: ## Create the Hermes sandbox and attach the Token Factory provider
	bash $(SANDBOX)/setup.sh --up

.PHONY: sandbox-down
sandbox-down: ## Delete the sandbox
	openshell sandbox delete ledger-hermes || true

.PHONY: sandbox-logs
sandbox-logs: ## Tail sandbox logs (shows denied connections)
	openshell logs ledger-hermes --tail

.PHONY: verify
verify: ## Run the Phase 0 exit verification (pass/fail table)
	bash $(SANDBOX)/verify.sh

.PHONY: gateway-preflight
gateway-preflight: ## Validate sandbox/gateway.toml with the real OpenShell binary
	@command -v openshell-gateway >/dev/null 2>&1 \
		&& openshell-gateway config preflight --path $(SANDBOX)/gateway.toml \
		&& echo "gateway.toml OK" \
		|| echo "openshell-gateway not installed — skipping"

.PHONY: sandbox-policy
sandbox-policy: ## Show the effective policy (base + provider rules)
	openshell policy get ledger-hermes --full

.PHONY: pin-models
pin-models: ## Pin the live model catalog into .env and write docs/model-catalog.md
	$(PY) $(EVAL)/pin_models.py

.PHONY: model-catalog
model-catalog: ## Write a dated snapshot of the live Token Factory catalog only
	$(PY) $(EVAL)/pin_models.py --snapshot-only

# --- Phase 0: corpus + OCR -------------------------------------------------

.PHONY: synth
synth: ## Generate the synthetic document corpus (deterministic, seeded, rendered)
	$(PY) $(EVAL)/gen_synthetic_docs.py --out $(EVAL)/synthetic_docs \
		--count $(CORPUS_COUNT) --today $(CORPUS_TODAY) --render --clean

.PHONY: corpus-qa
corpus-qa: ## Validate the corpus: format coverage, ground truth, reproducibility
	$(PY) $(EVAL)/qa_corpus.py --docs $(EVAL)/synthetic_docs --count $(CORPUS_COUNT)

.PHONY: redaction-audit
redaction-audit: ## Per-field detection rates of the deterministic redaction pass
	$(PY) $(EVAL)/corpus_redaction_audit.py --docs $(EVAL)/synthetic_docs \
		--markdown docs/redaction-audit.md --json $(EVAL)/results/redaction_audit.json

.PHONY: redacted-extract
redacted-extract: ## Can the model read redacted text? (Option C validation)
	$(PY) $(EVAL)/redacted_extract_bench.py --docs $(EVAL)/synthetic_docs --count 20 \
		--json $(EVAL)/results/redacted_extract.json

.PHONY: ocr-bench
ocr-bench: ## Compare OCR models on a sample of the corpus
	$(PY) $(EVAL)/ocr_bench.py --docs $(EVAL)/synthetic_docs --count 10 \
		--comparators \
		--json $(EVAL)/results/ocr_bench.json

# --- later phases ----------------------------------------------------------

.PHONY: leak-test
leak-test: ## Phase 2 — run the corpus through the pipeline, router ON vs OFF
	$(PY) $(EVAL)/leak_test.py

.PHONY: memory-eval
memory-eval: ## Phase 3 — memory-with-updates accuracy
	$(PY) $(EVAL)/memory_updates_eval.py

.PHONY: bench
bench: ## Phase 3 — per-tier cost and latency
	$(PY) $(EVAL)/cost_latency_bench.py

.PHONY: clean
clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
