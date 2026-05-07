.DEFAULT_GOAL := help

help:
	@echo ""
	@echo "  leartech-automated-agent"
	@echo "  ========================"
	@echo ""
	@echo "  Development:"
	@echo "    setup       Install UV and dependencies"
	@echo "    fmt         Format code (ruff)"
	@echo "    lint        Lint code (ruff + mypy)"
	@echo "    test        Run tests with coverage"
	@echo "    all         fmt + lint + test"
	@echo "    check       all + build (pre-push validation)"
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
	uv run ruff format app gate tests
	uv run ruff check app gate tests --select I --fix

lint:
	uv run ruff format --check app gate tests
	uv run ruff check app gate tests
	uv run mypy app gate

test:
	uv run coverage run -m pytest -v
	uv run coverage report

all: fmt lint test

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

.PHONY: help setup fmt lint test all check serve api-test build docker-run gate agent initiative lessons-list
