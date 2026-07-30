# Root Makefile — local validation entry point.
#
# `make check` runs the fast, OFFLINE checks this project expects before a change
# lands: Python byte-compile + unit tests, Terraform formatting, YAML parsing,
# and shell syntax. It needs no AWS credentials and makes no network calls, so it
# is safe to run anywhere — and it is the body a CI job would call in the final
# repo. Steps whose tool is missing self-skip with a notice rather than fail, so
# a partial toolchain still gets partial coverage.
#
# (Infra orchestration lives in terraform/Makefile, driven by ./platformctl.)

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := check

# Python packages/modules that ship in the repo (not an installed package).
PY_DIRS := ops/lib \
           platform/services/cluster-dashboard/scripts \
           platform/services/litellm-sync/scripts

# Roots of plain-YAML manifests (Helm charts under argocd/ are template YAML and
# are validated separately with `helm template`, so they are not included here).
YAML_ROOTS := platform workloads

# Shell scripts to syntax-check.
SH_SCRIPTS := platformctl $(shell find ops platform -name '*.sh' 2>/dev/null)

.PHONY: check check-py test check-tf fmt check-yaml check-sh help

check: check-py check-sh check-yaml check-tf ## Run all local checks (default)
	@echo "✓ all local checks passed"

check-py: ## Byte-compile the Python sources, then run unit tests
	@echo "==> python compile"
	@python3 -m compileall -q $(PY_DIRS)
	@$(MAKE) --no-print-directory test

test: ## Unit tests (pytest); self-skips if pytest is not installed
	@if python3 -c 'import pytest' 2>/dev/null; then \
		echo "==> pytest"; python3 -m pytest -q tests; \
	else \
		echo "!! pytest not installed — skipping unit tests (pip install pytest)"; \
	fi

check-tf: ## terraform fmt -check on every .tf-bearing dir (offline)
	@if command -v terraform >/dev/null 2>&1; then \
		echo "==> terraform fmt -check"; \
		dirs=$$(find terraform -name '*.tf' -not -path '*/.terraform/*' -exec dirname {} \; | sort -u); \
		for d in $$dirs; do terraform fmt -check "$$d" || exit 1; done; \
	else \
		echo "!! terraform not installed — skipping fmt check"; \
	fi

fmt: ## Fix Terraform formatting in place
	@terraform fmt -recursive terraform

check-yaml: ## Parse every plain-YAML manifest (multi-doc aware)
	@echo "==> yaml parse"
	@python3 tools/check_yaml.py $(YAML_ROOTS)

check-sh: ## Syntax-check (bash -n) + lint (shellcheck, if installed) every script
	@echo "==> bash -n"
	@for f in $(SH_SCRIPTS); do bash -n "$$f" || exit 1; done
	@if command -v shellcheck >/dev/null 2>&1; then \
		echo "==> shellcheck"; shellcheck $(SH_SCRIPTS); \
	else \
		echo "!! shellcheck not installed — skipping lint (matches CI's shellcheck step)"; \
	fi

help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'
