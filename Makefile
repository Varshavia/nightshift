.PHONY: help install test lint types check scan run-local deploy diagram clean

PYTHON ?= python3

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package and dev dependencies
	$(PYTHON) -m pip install -e ".[dev]"

test:  ## Run the test suite
	$(PYTHON) -m pytest

lint:  ## Lint with ruff
	$(PYTHON) -m ruff check .

types:  ## Type-check with mypy (strict)
	$(PYTHON) -m mypy

check: lint types test  ## Everything CI runs

scan:  ## Run one scan against the configured fleet
	$(PYTHON) -m services.scanner.main

run-local:  ## End to end on one repository: make run-local REPO=owner/name
	@test -n "$(REPO)" || (echo "usage: make run-local REPO=owner/name" && exit 1)
	# Run as a module, not by path: `services` and `scripts` are only importable
	# when the repository root is on sys.path, which -m provides and a bare path
	# invocation does not.
	$(PYTHON) -m scripts.run_local --repo "$(REPO)"

deploy:  ## Deploy to GCP (idempotent)
	./infra/deploy.sh

diagram:  ## Render docs/architecture.mmd to SVG
	npx --yes @mermaid-js/mermaid-cli -i docs/architecture.mmd -o docs/architecture.png -w 1800 -b white

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache **/__pycache__ workspaces
