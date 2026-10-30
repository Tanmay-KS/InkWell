# Ledger — developer entry points.
#
# `make help` lists everything. Every target is safe to re-run.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# VENV has to be defined before PY: `:=` expands immediately, so a PY
# defined first sees an empty VENV, falls through the wildcard to
# `/bin/python`, and every target runs outside the virtualenv. That
# failure is invisible until a target imports a dependency — `make demo`
# works on the system interpreter, `make leak-test` does not.
VENV := .venv
PY := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
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
	$(VENV)/bin/pip install -q -e ".[dev,ocr,middleware]"
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

.PHONY: run-pipeline
run-pipeline: ## Run a document through the pipeline inside the sandbox (DOC=path)
	@test -n "$(DOC)" || { echo "usage: make run-pipeline DOC=eval/synthetic_docs/doc-0000.png"; exit 1; }
	bash $(SANDBOX)/run_pipeline.sh "$(DOC)"

.PHONY: pipeline-check
pipeline-check: ## Check the sandbox plumbing only, without the pipeline (DOC=path)
	@test -n "$(DOC)" || { echo "usage: make pipeline-check DOC=eval/synthetic_docs/doc-0000.png"; exit 1; }
	bash $(SANDBOX)/run_pipeline.sh "$(DOC)" --check

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

# --- Phase 1: the pipeline --------------------------------------------------

.PHONY: demo
demo: ## Run the pipeline end to end over the corpus
	$(PY) agent/demo.py --count 3

.PHONY: pipeline
pipeline: ## Process one document and show the deadline and the draft
	$(PY) agent/demo.py $(DOC)

.PHONY: ocr-bench
ocr-bench: ## Compare OCR models on a sample of the corpus
	$(PY) $(EVAL)/ocr_bench.py --docs $(EVAL)/synthetic_docs --count 10 \
		--comparators \
		--json $(EVAL)/results/ocr_bench.json

# --- Phase 2: the privacy router -------------------------------------------

ROUTER_DIR := sandbox/middleware/ledger-router
AUDIT_LOG ?= /var/log/ledger/audit.jsonl

.PHONY: router-probe
router-probe: ## Describe + redaction self-test against the running middleware
	$(PY) $(ROUTER_DIR)/probe.py --self-test

.PHONY: router-serve
router-serve: ## Run the privacy router middleware (host path; binds 0.0.0.0:50051)
	LEDGER_ROUTER_PYTHON="$(PY)" bash $(ROUTER_DIR)/run.sh

.PHONY: router-check
router-check: ## Validate the router config without touching the gateway
	$(PY) $(SANDBOX)/router_policy.py --check

.PHONY: router-policy
router-policy: ## Print the router-enabled policy generated from the base policy
	$(PY) $(SANDBOX)/router_policy.py --on

.PHONY: router-on
router-on: ## Attach the router to the live sandbox (probe, policy set --wait)
	LEDGER_ROUTER_PYTHON="$(PY)" bash $(SANDBOX)/router_on.sh --audit-log "$(AUDIT_LOG)"

.PHONY: router-off
router-off: ## Detach the router; the base policy becomes the OFF condition
	LEDGER_ROUTER_PYTHON="$(PY)" bash $(SANDBOX)/router_off.sh

.PHONY: router-register
router-register: ## Register the middleware with the gateway (probe, preflight, restart)
	LEDGER_ROUTER_PYTHON="$(PY)" bash $(SANDBOX)/router_register.sh

.PHONY: router-unregister
router-unregister: ## Remove the middleware registration from the gateway
	LEDGER_ROUTER_PYTHON="$(PY)" bash $(SANDBOX)/router_unregister.sh

.PHONY: egress-probe
egress-probe: ## Prove allowed host/path and refused unknown host/path/port
	bash $(SANDBOX)/egress_probe.sh

.PHONY: verify-phase2
verify-phase2: ## Phase 2 exit verification, including the injection demo
	LEDGER_ROUTER_PYTHON="$(PY)" bash $(SANDBOX)/verify_phase2.sh --audit-log "$(AUDIT_LOG)"

.PHONY: injection-demo
injection-demo: ## Run the live prompt-injection-block demo
	LEDGER_ROUTER_PYTHON="$(PY)" bash demo/injection/run_injection_demo.sh --audit-log "$(AUDIT_LOG)"

.PHONY: audit-tail
audit-tail: ## Show the last audit decisions (never the payload)
	$(PY) $(SANDBOX)/audit_tail.py --path "$(AUDIT_LOG)" --last 20

# --- later phases ----------------------------------------------------------

# A partial run must never overwrite the canonical result. Only the
# default, full-corpus run writes `leak_test.json`; an explicitly
# overridden `CORPUS_COUNT` is a smoke run and writes somewhere else.
#
# This is not hypothetical: `make leak-test CORPUS_COUNT=3` once replaced
# the committed 200-document number with a 3-document sample.
LEAK_JSON := $(if $(filter command line,$(origin CORPUS_COUNT)),$(EVAL)/results/leak_test.smoke.json,$(EVAL)/results/leak_test.json)

.PHONY: leak-test
leak-test: ## Measure the leak rate with the router ON vs OFF
	$(PY) $(EVAL)/leak_test.py --docs $(EVAL)/synthetic_docs --count $(CORPUS_COUNT) \
		--json $(LEAK_JSON)

.PHONY: leak-test-smoke
leak-test-smoke: ## Fast leak-test smoke run — never overwrites the canonical result
	$(PY) $(EVAL)/leak_test.py --docs $(EVAL)/synthetic_docs --count 5 \
		--json $(EVAL)/results/leak_test.smoke.json

.PHONY: memory-eval
memory-eval: ## Phase 3 — memory-with-updates accuracy
	$(PY) $(EVAL)/memory_updates_eval.py

.PHONY: auditor-eval
auditor-eval: ## Upgrade Phase D — Auditor catch rate over planted cases
	$(PY) $(EVAL)/auditor_eval.py

.PHONY: langgraph-gate
langgraph-gate: ## Upgrade Phase B — compile the graph and make one real Nemotron call
	$(PY) $(EVAL)/langgraph_gate.py

.PHONY: bench
bench: ## Phase 3 — per-tier cost and latency
	$(PY) $(EVAL)/cost_latency_bench.py

# The benchmark spends money, so the dry run is the default way to check
# that the catalog still resolves the three tiers.
.PHONY: bench-dry-run
bench-dry-run: ## Phase 3 — resolve tiers and prices without calling a model
	$(PY) $(EVAL)/cost_latency_bench.py --dry-run

.PHONY: job
job: ## Phase 3 — run the deadline monitor once
	$(PY) -m jobs.deadline_monitor

.PHONY: job-dry-run
job-dry-run: ## Phase 3 — deadline monitor without notifying or recording
	$(PY) -m jobs.deadline_monitor --dry-run

.PHONY: job-status
job-status: ## Phase 3 — is the deadline-monitor schedule actually firing?
	$(PY) -m jobs.deadline_monitor --status

.PHONY: deadline-monitor
deadline-monitor: job ## Phase 3 alias — run one deadline-monitor pass

.PHONY: deadline-monitor-dry-run
deadline-monitor-dry-run: job-dry-run ## Phase 3 alias — query due items without notifying

# --- Phase 4: the interface -------------------------------------------------

.PHONY: web
web: ## Run the local web view against the household database (loopback only)
	$(PY) -m interface.web.server

.PHONY: demo-web
demo-web: ## Run the web view with isolated fictional demo data
	$(PY) -m interface.web.server --demo

.PHONY: telegram-bot
telegram-bot: ## Run the Telegram bot over long polling (needs TELEGRAM_BOT_TOKEN)
	$(PY) -m interface.chat

.PHONY: results
results: ## Phase 3 + Upgrade — compile the four numbers into docs/results.md + charts
	$(PY) $(EVAL)/results_pack.py

.PHONY: clean
clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
