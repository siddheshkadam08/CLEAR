# =============================================================================
# Contract Intelligence Platform
# =============================================================================
.DEFAULT_GOAL := help
SHELL := /bin/bash

# Container engine, auto-detected. Podman is preferred when present: it is
# rootless by default, which means a compromised container is confined to an
# unprivileged user rather than to root on the host - the right default for a
# stack that handles contract text.
#
# Override explicitly if both are installed and you want the other:
#   make up ENGINE=docker
ENGINE ?= $(shell command -v podman >/dev/null 2>&1 && echo podman || echo docker)

ifeq ($(ENGINE),podman)
  # `podman compose` shells out to podman-compose or the Docker Compose binary
  # depending on what is installed; either honours this file.
  COMPOSE ?= podman compose
else
  COMPOSE ?= docker compose
endif

BACKEND_DIR  := backend
FRONTEND_DIR := frontend
QUEUE_DIR    := queue

# ------------------------------------------------------------------ meta
.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

.PHONY: env
env: ## Create .env from .env.example if absent
	@test -f .env || (cp .env.example .env && echo "created .env - review before running")

# ------------------------------------------------------------------ stack
.PHONY: up
up: env ## Build and start the full stack
	$(COMPOSE) up -d --build

.PHONY: down
down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down

.PHONY: nuke
nuke: ## Stop the stack and DELETE all volumes (destroys local data)
	$(COMPOSE) down -v --remove-orphans

.PHONY: logs
logs: ## Tail logs for all services
	$(COMPOSE) logs -f --tail=200

.PHONY: logs-backend
logs-backend: ## Tail backend logs
	$(COMPOSE) logs -f --tail=200 backend

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: restart-backend
restart-backend: ## Rebuild + restart backend only
	$(COMPOSE) up -d --build backend worker-parser worker-ai

# ------------------------------------------------------------------ database
.PHONY: migrate
migrate: ## Apply all migrations
	$(COMPOSE) run --rm migrate python -m app.cli migrate

.PHONY: seed
seed: ## Seed roles, admin user, clause master and document profiles
	$(COMPOSE) run --rm migrate python -m app.cli seed

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add table x"
	$(COMPOSE) run --rm migrate alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Roll back one migration
	$(COMPOSE) run --rm migrate alembic downgrade -1

.PHONY: db-target
db-target: ## Assert this environment points at Hackathon-DB-SRV
	@# Piped into the running backend rather than mounted: the backend image is
	@# built from ./backend, so a repo-root script is not in it, and this checks
	@# what a *running* container resolved rather than what .env claims.
	$(COMPOSE) exec -T backend python - < scripts/check-db-target.py

.PHONY: psql
psql: ## psql shell on the database the app uses (Hackathon-DB-SRV)
	@url=$$(grep -hE '^DATABASE_URL=' .env | cut -d= -f2- | tr -d '\r' | sed 's/+asyncpg//;s/+psycopg//'); \
	 test -n "$$url" || { echo "DATABASE_URL is not set in .env - see the block above it in .env.example."; exit 1; }; \
	 $(COMPOSE) exec postgres psql "$$url"

.PHONY: psql-local
psql-local: ## psql shell on the bundled compose Postgres (holds no application data)
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-cip} -d $${POSTGRES_DB:-cip}

.PHONY: redis-cli
redis-cli: ## Open a redis shell
	$(COMPOSE) exec redis redis-cli

# ------------------------------------------------------------------ backend
.PHONY: backend-install
backend-install: ## Install backend deps locally (editable)
	cd $(BACKEND_DIR) && python -m pip install -e ".[dev]"

.PHONY: lint
lint: ## Lint + type-check everything
	cd $(BACKEND_DIR) && ruff check app tests && ruff format --check app tests && mypy app
	cd $(FRONTEND_DIR) && npm run lint && npm run typecheck
	cd $(QUEUE_DIR) && npm run lint && npm run typecheck

.PHONY: format
format: ## Auto-format everything
	cd $(BACKEND_DIR) && ruff format app tests && ruff check --fix app tests
	cd $(FRONTEND_DIR) && npm run format

.PHONY: test
test: test-backend test-frontend ## Run all tests

.PHONY: test-backend
test-backend: ## Run backend tests
	cd $(BACKEND_DIR) && pytest -q

.PHONY: test-backend-cov
test-backend-cov: ## Run backend tests with coverage
	cd $(BACKEND_DIR) && pytest --cov=app --cov-report=term-missing --cov-report=xml

.PHONY: test-frontend
test-frontend: ## Run frontend tests
	cd $(FRONTEND_DIR) && npm run test -- --run

.PHONY: test-integration
test-integration: ## Run integration tests against the running stack
	$(COMPOSE) exec backend pytest -q -m integration

# ------------------------------------------------------------------ frontend
.PHONY: frontend-install
frontend-install: ## Install frontend deps
	cd $(FRONTEND_DIR) && npm ci

.PHONY: frontend-dev
frontend-dev: ## Run the Vite dev server on the host
	cd $(FRONTEND_DIR) && npm run dev

.PHONY: frontend-build
frontend-build: ## Production build
	cd $(FRONTEND_DIR) && npm run build

# ------------------------------------------------------------------ queue
.PHONY: queue-install
queue-install: ## Install queue deps
	cd $(QUEUE_DIR) && npm ci

.PHONY: queue-status
queue-status: ## Print queue depths / DLQ size
	curl -s localhost:9100/queues | python -m json.tool

# ------------------------------------------------------------------ utilities
.PHONY: openapi
openapi: ## Dump the OpenAPI schema to openapi.json
	curl -s localhost:8000/openapi.json > openapi.json && echo "wrote openapi.json"

.PHONY: shell
shell: ## Python shell with app context
	$(COMPOSE) exec backend python -m app.cli shell

.PHONY: reprocess
reprocess: ## Re-run a pipeline stage: make reprocess job=<uuid> stage=embedding
	$(COMPOSE) exec backend python -m app.cli reprocess --job $(job) --from-stage $(stage)

.PHONY: smoke
smoke: ## End-to-end smoke test against the running stack
	$(COMPOSE) exec backend python -m app.cli smoke

# ------------------------------------------------------- embeddings / vectors
# Every target here is read-only except `reindex`, which queues work but writes
# no vectors itself. Nothing applies a migration; `make migrate` stays explicit.

.PHONY: embedding-check
embedding-check: ## Probe the embedding provider: dimension, latency, auth
	$(COMPOSE) exec -T backend python -m app.tools.embedding_probe

.PHONY: embedding-check-local
embedding-check-local: ## Same probe, without Docker (uses backend/.venv)
	cd backend && python -m app.tools.embedding_probe

.PHONY: verify-pgvector
verify-pgvector: ## Compare the live vector schema against the live model
	$(COMPOSE) exec -T backend python -m app.tools.verify_pgvector

.PHONY: generate-vector-migration
generate-vector-migration: ## Write (never apply) the migration for a dimension mismatch
	$(COMPOSE) exec -T backend python -m app.tools.verify_pgvector --generate-migration

.PHONY: diagnostics
diagnostics: ## Full system diagnostics report
	$(COMPOSE) exec -T backend python -m app.tools.system_diagnostics

.PHONY: audit
audit: ## Write EMBEDDING_AUDIT.md from a live diagnostics run
	$(COMPOSE) exec -T backend python -m app.tools.system_diagnostics \
		--markdown /tmp/EMBEDDING_AUDIT.md --json > /dev/null
	$(COMPOSE) cp backend:/tmp/EMBEDDING_AUDIT.md ./EMBEDDING_AUDIT.md
	@echo "wrote EMBEDDING_AUDIT.md"

.PHONY: reindex
reindex: ## Regenerate vectors for contracts embedded with another model
	$(COMPOSE) exec backend python -m app.cli reindex-embeddings

.PHONY: nvidia-check
nvidia-check: ## Validate NVIDIA credentials + schema before switching over
	NVIDIA_API_KEY=$${NVIDIA_API_KEY:?set NVIDIA_API_KEY} \
	EMBEDDING_PROVIDER=nvidia $(COMPOSE) --profile nvidia run --rm embedding-check

.PHONY: nvidia-up
nvidia-up: ## Bring the stack up on NVIDIA embeddings (validates first)
	NVIDIA_API_KEY=$${NVIDIA_API_KEY:?set NVIDIA_API_KEY} \
	EMBEDDING_PROVIDER=nvidia $(COMPOSE) --profile nvidia up -d --build

.PHONY: test-live
test-live: ## Integration tests against a real Postgres (needs TEST_DATABASE_URL)
	cd backend && TEST_DATABASE_URL=$${TEST_DATABASE_URL:?set TEST_DATABASE_URL} \
		python -m pytest tests -q -m integration

.PHONY: check-contracts
check-contracts: ## Verify the frontend/backend interface contracts
	cd $(BACKEND_DIR) && python ../scripts/check_api_contract.py
	cd $(BACKEND_DIR) && python ../scripts/check_api_shapes.py
	cd $(BACKEND_DIR) && python ../scripts/check_path_params.py

# ---------------------------------------------------------------- podman
.PHONY: engine
engine: ## Show which container engine will be used
	@echo "engine : $(ENGINE)"
	@echo "compose: $(COMPOSE)"
	@$(ENGINE) --version 2>/dev/null || echo "  ($(ENGINE) is not on PATH)"

.PHONY: podman-init
podman-init: ## First-time Podman setup (machine on macOS/Windows, SELinux note on Linux)
	@command -v podman >/dev/null 2>&1 || { echo "podman is not installed"; exit 1; }
	@if podman machine list --format '{{.Name}}' 2>/dev/null | grep -q .; then \
		podman machine start 2>/dev/null || echo "  podman machine already running"; \
	else \
		echo "  no podman machine needed (native Linux)"; \
	fi
	@echo "ready. run: make up"
