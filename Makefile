# Ledger — developer entry points.
#
# `make help` lists everything. Every target is safe to re-run.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PY := python3
VENV := .venv
SANDBOX := sandbox
EVAL := eval

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- setup -----------------------------------------------------------------

.PHONY: venv
venv: ## Create the local virtualenv and install dev dependencies
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -e ".[dev]"
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

.PHONY: sandbox-policy
sandbox-policy: ## Show the effective policy (base + provider rules)
	openshell policy get ledger-hermes --full

.PHONY: pin-models
pin-models: ## Read the live Token Factory catalog and print exact model IDs
	@test -n "$$NEBIUS_API_KEY" || { echo "NEBIUS_API_KEY is not set"; exit 1; }
	curl -sS "https://api.tokenfactory.nebius.com/v1/models?verbose=true" \
		-H "Authorization: Bearer $$NEBIUS_API_KEY" \
		| $(PY) -c 'import json,sys; [print(m["id"]) for m in json.load(sys.stdin).get("data",[])]'

# --- Phase 0: corpus + OCR -------------------------------------------------

.PHONY: synth
synth: ## Generate the synthetic document corpus (deterministic, seeded)
	$(PY) $(EVAL)/gen_synthetic_docs.py --out $(EVAL)/synthetic_docs --count 200

.PHONY: ocr-bench
ocr-bench: ## Compare OCR models on a sample of the corpus
	$(PY) $(EVAL)/ocr_bench.py --docs $(EVAL)/synthetic_docs --count 10

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
