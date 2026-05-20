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
	@echo "    helm-lint    Lint Helm chart (default + postgresql.enabled toggles)"
	@echo "    helm-template Render Helm chart, list objects produced"
	@echo "    helm         helm-lint + helm-template"
	@echo "    all          fmt + lint + test + helm"
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
	@echo "  Local mock-MCP testing (no Anthropic, no GitHub, no kubectl):"
	@echo "    mock-scenarios          List available mock scenarios"
	@echo "    mock-scenario SCENARIO=…  Run a scenario through the MCP tools (no agent)"
	@echo "    initiative-mock INITIATIVE=… SCENARIO=…  Full agent run with mock pipeline"
	@echo ""

setup:
	@command -v uv >/dev/null 2>&1 || { echo "Installing UV..."; curl -LsSf https://astral.sh/uv/install.sh | sh; }
	uv sync --dev
	@echo "Setup complete. Run 'make all' to validate."

fmt:
	uv run ruff format app gate tests
	uv run ruff check app gate tests --select I --fix

lint:
	uv run ruff format --check app gate tests
	uv run ruff check app gate tests
	uv run mypy app gate

test:
	uv run coverage run -m pytest -v
	uv run coverage report

# Helm chart validation — render with realistic values and lint. Mirrors
# what the chart goes through in JX3 release; catches schema breaks +
# missing toggles locally before they fail in cluster.
helm-lint:
	helm lint charts/leartech-automated-agent \
	  --set image.repository=local --set image.tag=local
	@echo "✓ helm lint OK (postgresql.enabled=false default)"
	helm lint charts/leartech-automated-agent \
	  --set image.repository=local --set image.tag=local \
	  --set postgresql.enabled=true
	@echo "✓ helm lint OK (postgresql.enabled=true)"

helm-template:
	@echo "Rendering with postgresql.enabled=false (default — no DB objects):"
	@helm template t charts/leartech-automated-agent \
	  --set image.repository=local --set image.tag=local \
	  | grep "^kind:" | sort | uniq -c | sed 's/^/  /'
	@echo ""
	@echo "Rendering with postgresql.enabled=true (Database + ConfigMap + Job appear):"
	@helm template t charts/leartech-automated-agent \
	  --set image.repository=local --set image.tag=local \
	  --set postgresql.enabled=true \
	  | grep "^kind:" | sort | uniq -c | sed 's/^/  /'

helm: helm-lint helm-template

all: fmt lint test helm

check: all build

serve:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

api-test:
	@echo "Smoke-testing /health..."
	@curl -fsS http://localhost:8080/health | python -m json.tool || { echo "Service not running. Start with 'make serve' in another terminal."; exit 1; }

build:
	docker build -t leartech-automated-agent .

docker-run: build
	docker run --rm -p 8080:8080 \
		-e ANTHROPIC_API_KEY="$$ANTHROPIC_API_KEY" \
		-e GH_TOKEN="$$(gh auth token 2>/dev/null || echo '')" \
		leartech-automated-agent

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

# ─── Local mock-MCP testing ─────────────────────────────────────────────────
# Drives the agent's MCP tools against scripted scenarios — no Anthropic,
# no GitHub, no kubectl. See gate/mcp_servers/pipeline_server_mock.py for
# scenario format. Demonstrates the agent's fail-fast / wait-for-terminal
# semantics on synthetic inputs.

mock-scenarios:
	@echo ""
	@echo "Available scenarios in gate/mcp_servers/mock_scenarios/:"
	@echo ""
	@for f in gate/mcp_servers/mock_scenarios/*.yaml; do \
		name=$$(basename $$f .yaml); \
		desc=$$(awk '/^description:/{flag=1; next} /^[a-z]+:/{flag=0} flag' $$f | sed 's/^  //' | head -1); \
		printf "  %-40s %s\n" "$$name" "$$desc"; \
	done
	@echo ""
	@echo "Run with: make mock-scenario SCENARIO=<name>"
	@echo ""

mock-scenario:
	@if [ -z "$(SCENARIO)" ]; then \
		echo "Usage: make mock-scenario SCENARIO=<name>"; \
		echo "       (run 'make mock-scenarios' to list available)"; \
		exit 2; \
	fi
	@if [ ! -f "gate/mcp_servers/mock_scenarios/$(SCENARIO).yaml" ]; then \
		echo "ERROR: scenario not found at gate/mcp_servers/mock_scenarios/$(SCENARIO).yaml"; \
		echo "Run 'make mock-scenarios' to list available."; \
		exit 2; \
	fi
	uv run python scripts/run_mock_scenario.py gate/mcp_servers/mock_scenarios/$(SCENARIO).yaml

initiative-mock:
	@if [ -z "$(INITIATIVE)" ] || [ -z "$(SCENARIO)" ]; then \
		echo "Usage: make initiative-mock INITIATIVE=<name> SCENARIO=<name>"; \
		echo "       (needs ANTHROPIC_API_KEY in env; uses mock pipeline MCP)"; \
		exit 2; \
	fi
	@if [ ! -f "gate/mcp_servers/mock_scenarios/$(SCENARIO).yaml" ]; then \
		echo "ERROR: scenario gate/mcp_servers/mock_scenarios/$(SCENARIO).yaml not found"; \
		exit 2; \
	fi
	@mkdir -p logs
	@LOGFILE="logs/initiative-mock-$(INITIATIVE)-$(SCENARIO)-$$(date +%Y%m%d-%H%M%S).log"; \
	echo "→ logging to $$LOGFILE"; \
	LEARTECH_MOCK_PIPELINE_SCENARIO=$$PWD/gate/mcp_servers/mock_scenarios/$(SCENARIO).yaml \
		uv run initiative initiatives/$(INITIATIVE).yaml 2>&1 | tee "$$LOGFILE"

.PHONY: help setup fmt lint test helm-lint helm-template helm all check serve api-test build docker-run gate agent initiative lessons-list mock-scenarios mock-scenario initiative-mock
