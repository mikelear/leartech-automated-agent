.DEFAULT_GOAL := help

help:
	@echo ""
	@echo "  leartech-automated-agent"
	@echo "  ========================"
	@echo ""
	@echo "  Development:"
	@echo "    setup        Install UV and dependencies"
	@echo "    fmt          Format code (ruff)"
	@echo "    lint         Lint code (ruff + mypy)"
	@echo "    test         Run tests with coverage"
	@echo "    all          fmt + lint + test"
	@echo "    check        all + build (pre-push validation)"
	@echo ""
	@echo "  Service (HTTP):"
	@echo "    serve       Run FastAPI service locally on :8080"
	@echo "    api-test    Smoke-test local API with sample initiative"
	@echo "    build       Build Docker image"
	@echo "    docker-run  Build and run service in Docker"
	@echo ""
	@echo "  Direct CLI (no service, no kubectl required):"
	@echo "    gate        Run criteria gate against a live PR (REPO=… PR=…)"
	@echo "    agent       Read-only review agent (REPO=… PR=…)"
	@echo "    initiative  Write-mode initiative (INITIATIVE=…)"
	@echo "    lessons-list   Browse lessons catalog"
	@echo ""

setup:
	@command -v uv >/dev/null 2>&1 || { echo "Installing UV..."; curl -LsSf https://astral.sh/uv/install.sh | sh; }
	uv sync --dev
	@echo "Setup complete. Run 'make all' to validate."

fmt:
	uv run ruff format gate tests
	uv run ruff check gate tests --select I --fix

lint:
	uv run ruff format --check gate tests
	uv run ruff check gate tests
	uv run mypy gate

test:
	uv run coverage run -m pytest -v
	uv run coverage report

# Helm chart validation — render with realistic values and lint. Mirrors
# what the chart goes through in JX3 release; catches schema breaks +
# missing toggles locally before they fail in cluster.

all: fmt lint test

check: all





# Direct CLI shortcuts — no service, no HTTP, just `uv run`.
# These work locally without kubectl, without cluster credentials.
gate:
	@if [ -z "$(REPO)" ] || [ -z "$(PR)" ]; then \
		echo "Usage: make gate REPO=<repo> PR=<pr-number>"; exit 2; \
	fi
	uv run gate check --repo $(REPO) --pr $(PR)

agent:
	@if [ -z "$(REPO)" ] || [ -z "$(PR)" ]; then \
		echo "Usage: make agent REPO=<repo> PR=<pr-number>"; exit 2; \
	fi
	uv run agent --repo $(REPO) --pr $(PR)

initiative:
	@if [ -z "$(INITIATIVE)" ]; then \
		echo "Usage: make initiative INITIATIVE=<initiative-yaml-name>"; exit 2; \
	fi
	@mkdir -p logs
	@LOGFILE="logs/initiative-$(INITIATIVE)-$$(date +%Y%m%d-%H%M%S).log"; \
	echo "→ logging to $$LOGFILE (symlinked at logs/latest.log)"; \
	ln -sfn "$$(basename $$LOGFILE)" logs/latest.log; \
	uv run initiative initiatives/$(INITIATIVE).yaml 2>&1 | tee "$$LOGFILE"

lessons-list:
	uv run lessons list

# ─── Local mock-MCP testing (retired) ──────────────────────────────────────
# The in-process `pipeline_server` shim + its mock counterpart were retired
# when the PR-check surface (list_pr_checks / wait_for_terminal /
# wait_for_first_failure_or_all_pass) moved to the remote leartech-jx3-flow
# MCP. Local mock harnesses (`make mock-scenario`, `make initiative-mock`,
# `scripts/run_mock_scenario.py`, `gate/mcp_servers/mock_scenarios/*.yaml`)
# have been deleted; drive integration tests directly against the deployed
# Go `leartech-mcp-servers/jx3_flow` server via `scripts/mcp_test_client.py`.

.PHONY: help setup fmt lint test helm-lint helm-template helm all check serve api-test build docker-run gate agent initiative lessons-list
