.PHONY: run dev stop restart setup install migrate test test-backend test-integration test-all test-coverage test-unit test-unit-fast test-unit-file test-with-timing clean clean-all pre-commit help check-poetry check-python check-deps install-poetry install-deps up down build logs shell db-shell redis-shell test-docker migrate-docker restart-api ps clean-docker check-docker ensure-test-deps prepare-test-data ensure-test-env

export SKIP_TEST_DB_FIXTURES ?= true

TEST_SERVER_HOST ?= 0.0.0.0
TEST_SERVER_PORT ?= 8000
TEST_APP_URL ?= http://localhost:$(TEST_SERVER_PORT)
TEST_API_BASE ?= $(TEST_APP_URL)/api

# tests/conftest.py sets TESTING_FORCE_MEMORY=true, which makes
# api/database.py bind to /tmp/signupflow_test.db no matter what
# DATABASE_URL says. Point the Makefile at that same file so `rm` and
# `setup_test_data` actually reset the database the suite reads. Using
# ./test_roster.db here meant every reset was a no-op and the real DB
# accumulated rows across runs until fixed-ID tests collided with 409.
TEST_DB_PATH := /tmp/signupflow_test.db
TEST_DB_PATH_STRIPPED := $(patsubst /%,%,$(TEST_DB_PATH))
TEST_DB_URL := sqlite:////$(TEST_DB_PATH_STRIPPED)
export DATABASE_URL ?= $(TEST_DB_URL)

# Detect available Docker Compose command (v1 `docker-compose` or v2 `docker compose`)
DOCKER_COMPOSE := $(shell \
	if command -v docker-compose >/dev/null 2>&1; then \
		echo docker-compose; \
	elif docker compose version >/dev/null 2>&1; then \
		echo docker compose; \
	else \
		echo missing; \
	fi)

# Ensure Docker CLI and daemon are available before running docker-compose targets
check-docker:
	@command -v docker >/dev/null 2>&1 || { \
		echo "❌ Docker CLI not found. Install Docker Desktop or Docker Engine first."; \
		exit 1; \
	}
	@docker info >/dev/null 2>&1 || { \
		echo "❌ Cannot connect to the Docker daemon. Start Docker (Docker Desktop, colima, or 'systemctl start docker') and ensure your user can access /var/run/docker.sock."; \
		exit 1; \
	}

DOCKER_TARGETS := up down build rebuild logs logs-api logs-db logs-redis shell db-shell redis-shell \
	test-docker test-docker-quick test-docker-summary test-docker-file \
	test-docker-unit test-docker-unit-fast test-docker-integration \
	test-docker-comprehensive test-docker-all test-docker-coverage \
	migrate-docker restart-api ps clean-docker clean-docker-all

$(DOCKER_TARGETS): check-docker

ensure-test-deps: check-poetry
	@POETRY_ENV=$$(poetry env info --path 2>/dev/null || true); \
	if [ -z "$$POETRY_ENV" ] || [ "$${FORCE_POETRY_INSTALL:-0}" = "1" ]; then \
		echo "📦 Installing Python dependencies via Poetry..."; \
		poetry install; \
	else \
		echo "✅ Poetry dependencies already installed (env: $$POETRY_ENV)"; \
	fi

prepare-test-data: ensure-test-deps
	@echo "🧪 Preparing baseline test data..."
	@poetry run python -m tests.setup_test_data || echo "⚠️  setup_test_data fallback: continuing without direct DB seed"

ensure-test-env: prepare-test-data

install-poetry:
	@if ! command -v poetry >/dev/null 2>&1; then \
		echo "📦 Installing Poetry..."; \
		echo "   Trying official installer..."; \
		if curl -sSL https://install.python-poetry.org | python3 - 2>/dev/null; then \
			echo "✅ Poetry installed successfully!"; \
			echo "⚠️  Add Poetry to your PATH by running:"; \
			echo "   export PATH=\"\$$HOME/.local/bin:\$$PATH\""; \
			echo "   Or restart your terminal."; \
		else \
			echo "⚠️  Official installer failed. Trying pip installation..."; \
			python3 -m pip install --user poetry && \
			echo "✅ Poetry installed via pip!" && \
			echo "⚠️  You may need to add Poetry to your PATH:"; \
			echo "   export PATH=\"\$$HOME/Library/Python/3.9/bin:\$$PATH\""; \
		fi \
	else \
		echo "✅ Poetry is already installed: $$(poetry --version)"; \
	fi

install-deps: install-poetry
	@echo ""
	@echo "✅ All system dependencies installed!"
	@echo ""

check-poetry:
	@command -v poetry >/dev/null 2>&1 || { \
		echo "❌ Poetry is not installed or not in PATH"; \
		echo "   Run 'make install-poetry' to install it automatically"; \
		echo "   Or install manually: curl -sSL https://install.python-poetry.org | python3 -"; \
		exit 1; \
	}

check-python:
	@PY_VERSION=$$(python3 --version 2>&1 | sed 's/Python //'); \
	PY_MAJOR=$$(echo $$PY_VERSION | cut -d. -f1); \
	PY_MINOR=$$(echo $$PY_VERSION | cut -d. -f2); \
	if [ "$$PY_MAJOR" -lt 3 ] || ([ "$$PY_MAJOR" -eq 3 ] && [ "$$PY_MINOR" -lt 10 ]); then \
		echo "⚠️  Python 3.10+ required (you have: Python $$PY_VERSION)"; \
		echo "   Some features will not work with Python 3.9 or earlier"; \
		echo "   Install with: brew install python@3.11"; \
	else \
		echo "✅ Python version OK: Python $$PY_VERSION"; \
	fi

check-deps:
	@echo "🔍 Checking development dependencies..."
	@echo ""
	@echo "Python:"
	@if python3 --version >/dev/null 2>&1; then \
		PY_VERSION=$$(python3 --version 2>&1 | sed 's/Python //'); \
		PY_MAJOR=$$(echo $$PY_VERSION | cut -d. -f1); \
		PY_MINOR=$$(echo $$PY_VERSION | cut -d. -f2); \
		echo "   ✅ Python installed: $$PY_VERSION"; \
		if [ "$$PY_MAJOR" -lt 3 ] || ([ "$$PY_MAJOR" -eq 3 ] && [ "$$PY_MINOR" -lt 10 ]); then \
			echo "   ⚠️  Python 3.10+ required (upgrade recommended)"; \
		else \
			echo "   ✅ Python 3.10+ detected"; \
		fi; \
	else \
		echo "   ❌ Python not found"; \
	fi
	@echo ""
	@echo "Poetry:"
	@command -v poetry >/dev/null 2>&1 && echo "   ✅ Poetry installed: $$(poetry --version)" || echo "   ❌ Poetry not installed"
	@echo ""
	@if ! command -v poetry >/dev/null 2>&1; then \
		echo "⚠️  Missing dependencies detected. Run 'make help' for installation instructions."; \
	else \
		echo "✅ All dependencies installed! You can run 'make setup' to install project packages."; \
	fi

# Run the development server
run: check-poetry
	@echo "🚀 Starting SignUpFlow development server..."
	@poetry run uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

# Run Celery worker
celery: check-poetry
	@echo "🚀 Starting Celery worker..."
	@poetry run celery -A api.celery_app worker --loglevel=info

dev: run

stop:
	@echo "🛑 Stopping SignUpFlow server..."
	@-pkill -f "uvicorn api.main:app" 2>/dev/null || true
	@-lsof -ti:8000 | xargs kill -9 2>/dev/null || true
	@echo "✅ Server stopped"

restart: stop
	@sleep 1
	@echo "🔄 Restarting server..."
	@$(MAKE) run

setup:
	@echo "🚀 Starting SignUpFlow setup..."
	@echo ""
	@$(MAKE) check-python
	@$(MAKE) install-deps
	@$(MAKE) install
	@$(MAKE) migrate
	@echo ""
	@echo "✅ Setup complete! Run 'make run' to start the server."
	@echo "   Visit http://localhost:8000/docs"
	@echo ""

install: check-poetry
	@echo "📦 Installing project packages..."
	@poetry install
	@echo "✅ Project packages installed"

migrate: check-poetry
	@echo "🔄 Running database migrations..."
	@poetry run alembic upgrade head
	@echo "✅ Migrations complete"

# Run all backend tests
test: test-backend

test-backend: check-poetry
	@echo "🧪 Running backend tests..."
	@poetry run pytest tests/comprehensive_test_suite.py -v --tb=short

test-integration: check-poetry
	@echo "🧪 Running integration tests..."
	@poetry run pytest tests/integration/ -v --tb=short

test-all: ensure-test-env
	@echo "🚀 Running complete test suite..."
	@rm -f $(TEST_DB_PATH) $(TEST_DB_PATH)-shm $(TEST_DB_PATH)-wal
	@echo "🔄 Rebuilding fresh SQLite test database..."
	@poetry run python -m tests.setup_test_data >/dev/null
	@echo ""
	@echo "================================"
	@echo "   UNIT TESTS"
	@echo "================================"
	@poetry run pytest tests/unit/ -v --tb=short
	@echo ""
	@echo "================================"
	@echo "   API TESTS"
	@echo "================================"
	@poetry run pytest tests/api/ -v --tb=short
	@echo ""
	@echo "================================"
	@echo "   CLI TESTS"
	@echo "================================"
	@poetry run pytest tests/cli/ -v --tb=short
	@echo ""
	@echo "================================"
	@echo "   INTEGRATION TESTS"
	@echo "================================"
	@poetry run pytest tests/integration/ -v --tb=short

test-coverage: check-poetry
	@echo "📊 Generating test coverage reports..."
	@poetry run pytest tests/ --cov=api --cov-report=html --cov-report=term

test-unit: check-poetry
	@echo "🧪 Running unit tests..."
	@poetry run pytest tests/unit/ -v --tb=short

test-unit-file: check-poetry
	@echo "🧪 Running specific unit test file..."
	@if [ -z "$(FILE)" ]; then \
		echo "❌ Usage: make test-unit-file FILE=tests/unit/test_name.py"; \
		exit 1; \
	fi
	@timeout 60 poetry run pytest $(FILE) -v --tb=short -s

clean:
	@echo "🧹 Cleaning test artifacts and temporary files..."
	@rm -rf coverage
	@rm -rf htmlcov
	@rm -rf .pytest_cache
	@rm -f $(TEST_DB_PATH) $(TEST_DB_PATH)-shm $(TEST_DB_PATH)-wal
	@rm -rf __pycache__
	@find . -type d -name "__pycache__" ! -path "*/.venv/*" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" ! -path "*/.venv/*" -delete 2>/dev/null || true
	@find . -type f -name ".DS_Store" -delete 2>/dev/null || true
	@rm -f *.db-shm *.db-wal 2>/dev/null || true
	@echo "✅ Clean complete"

clean-weekly:
	@echo "🧹 Running weekly maintenance cleanup..."
	@./scripts/cleanup_maintenance.sh
	@echo "✅ Weekly maintenance complete"

clean-all: stop clean
	@echo "🧹 Deep cleaning (removing dependencies)..."
	@rm -rf .venv
	@echo "✅ Deep clean complete. Run 'make setup' to reinstall."

pre-commit: check-poetry
	@echo "⚡ Running fast pre-commit tests..."
	@poetry run pytest tests/unit/ -x --tb=short
	@echo "✅ Pre-commit tests passed!"

test-unit-fast: check-poetry
	@echo "⚡ Running fast unit tests (skipping slow tests)..."
	@poetry run pytest tests/unit/ -v --tb=short -m "not slow"

test-with-timing: check-poetry
	@echo "⏱️  Running tests with timing information..."
	@poetry run pytest tests/unit/ --durations=20 -v --tb=short

test-contract: check-poetry
	@echo "📜 Running OpenAPI contract snapshot tests..."
	@poetry run pytest tests/contract/ -v --tb=short

update-openapi-snapshot: check-poetry
	@echo "🔄 Refreshing OpenAPI contract snapshot..."
	@poetry run python -m tests.contract.test_openapi_snapshot --update
	@echo "✅ Snapshot updated. Review the diff, run 'make mobile-codegen' to refresh the Flutter client, then commit."

# ============================================================================
# Mobile (Flutter) — see specs/022-flutter-mobile-app/spec.md
# ============================================================================

# Regenerate the Dart API client from the OpenAPI snapshot.
# Requires: openjdk@17 (`brew install openjdk@17`) and node (already required).
# Run after any backend API change that you want surfaced to the iOS app.
# Auto-detect Java: prefer brew openjdk@17 if installed (most common dev
# setup), fall back to anything on PATH that responds to `java -version`.
JAVA_HOME_BREW := $(shell brew --prefix openjdk@17 2>/dev/null)
JAVA_BIN := $(if $(JAVA_HOME_BREW),$(JAVA_HOME_BREW)/bin/java,$(shell command -v java 2>/dev/null))

mobile-codegen:
	@echo "🔄 Generating Flutter API client from OpenAPI snapshot..."
	@if [ -z "$(JAVA_BIN)" ] || ! $(JAVA_BIN) -version >/dev/null 2>&1; then \
		echo "❌ Java not found. Install with: brew install openjdk@17"; \
		echo "   The Make target auto-detects brew openjdk@17 — no need to set JAVA_HOME."; \
		exit 1; \
	fi
	@echo "ℹ️  Using $(JAVA_BIN)"
	@PATH="$(dir $(JAVA_BIN)):$$PATH" \
	  npx -y @openapitools/openapi-generator-cli@2.20.2 generate \
	  -i tests/contract/openapi.snapshot.json \
	  -g dart-dio \
	  -o mobile/api_client \
	  --additional-properties=pubName=signupflow_api,pubVersion=0.0.1,nullSafe=true,nullableFields=true \
	  --skip-validate-spec
	@cd mobile/api_client && PATH=/Users/tomwu/Projects/flutter/bin:$$PATH flutter pub get
	@cd mobile/api_client && PATH=/Users/tomwu/Projects/flutter/bin:$$PATH dart run build_runner build --delete-conflicting-outputs
	@cd mobile && PATH=/Users/tomwu/Projects/flutter/bin:$$PATH flutter pub get
	@echo "✅ Mobile API client regenerated as a path-dep package at mobile/api_client/."

# ============================================================================
# Docker Compose Commands (Development Environment)
# ============================================================================

up:
	@echo "🐳 Starting SignUpFlow development environment..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml up -d
	@echo ""
	@echo "✅ Services started!"
	@echo "   API:        http://localhost:8000"
	@echo "   PostgreSQL: localhost:5433 (user: signupflow, db: signupflow_dev)"
	@echo "   Redis:      localhost:6380"
	@echo ""
	@echo "View logs:     make logs"
	@echo "Stop services: make down"
	@echo ""

down:
	@echo "🛑 Stopping SignUpFlow development environment..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml down
	@echo "✅ Services stopped"

build:
	@echo "🔨 Building Docker images..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml build --no-cache
	@echo "✅ Build complete"

rebuild: down build up

logs:
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml logs -f

logs-api:
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml logs -f api

logs-db:
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml logs -f db

logs-redis:
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml logs -f redis

logs-worker:
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml logs -f worker

shell:
	@echo "🐚 Opening shell in API container..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec api bash

db-shell:
	@echo "🐘 Opening PostgreSQL shell..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec db psql -U signupflow -d signupflow_dev

redis-shell:
	@echo "🔴 Opening Redis CLI..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec redis redis-cli -a dev_redis_password

# ============================================================================
# Docker-Based Testing
# ============================================================================

test-docker:
	@echo "🧪 Running unit tests in Docker container..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec -T api pytest tests/unit/ -v --tb=short

test-docker-quick:
	@echo "⚡ Running unit tests in Docker (quick mode)..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec -T api pytest tests/unit/ -v --tb=no

test-docker-summary:
	@echo "📊 Running unit tests in Docker (summary only)..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec -T api pytest tests/unit/ -v --tb=no 2>&1 | grep -E "(PASSED|FAILED|SKIPPED|ERROR|=====|passed|failed|warning)"

test-docker-file:
	@echo "🎯 Running specific test file in Docker..."
	@if [ -z "$(FILE)" ]; then \
		echo "❌ Usage: make test-docker-file FILE=tests/unit/test_name.py"; \
		exit 1; \
	fi
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec -T api pytest $(FILE) -v --tb=short

test-docker-unit:
	@echo "🧪 Running unit tests in Docker container..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec -T api pytest tests/unit/ -v --tb=short

test-docker-unit-fast:
	@echo "⚡ Running fast unit tests in Docker..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec -T api pytest tests/unit/ -v --tb=short -m "not slow"

test-docker-integration:
	@echo "🔗 Running integration tests in Docker..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec -T api pytest tests/integration/ -v --tb=short

test-docker-comprehensive:
	@echo "🚀 Running comprehensive test suite in Docker..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec -T api pytest tests/comprehensive_test_suite.py -v --tb=short

test-docker-all:
	@echo "🎯 Running ALL tests in Docker container..."
	@echo ""
	@echo "================================"
	@echo "   UNIT TESTS (Docker)"
	@echo "================================"
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec -T api pytest tests/unit/ -v --tb=short
	@echo ""
	@echo "================================"
	@echo "   INTEGRATION TESTS (Docker)"
	@echo "================================"
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec -T api pytest tests/integration/ -v --tb=short
	@echo ""
	@echo "✅ All Docker tests complete!"

test-docker-coverage:
	@echo "📊 Running tests with coverage in Docker..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec -T api pytest tests/ --cov=api --cov-report=html --cov-report=term -v --tb=short

migrate-docker:
	@echo "🔄 Running migrations in Docker container..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml exec api alembic upgrade head
	@echo "✅ Migrations complete"

restart-api:
	@echo "🔄 Restarting API service..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml restart api
	@echo "✅ API restarted"

ps:
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml ps

clean-docker:
	@echo "🧹 Cleaning Docker volumes and containers..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml down -v
	@echo "✅ Docker cleanup complete"

clean-docker-all: clean-docker
	@echo "🧹 Removing Docker images..."
	@$(DOCKER_COMPOSE) -f docker-compose.dev.yml down --rmi all
	@echo "✅ Complete Docker cleanup done"

.DEFAULT_GOAL := help

help:
	@echo "SignUpFlow Commands:"
	@echo ""
	@echo "🚀 Quick Start:"
	@echo "  make setup            - Auto-install Poetry, packages, and setup DB"
	@echo "  make up               - Start Docker environment (PostgreSQL + Redis + API)"
	@echo ""
	@echo "🐳 Docker Development (Recommended):"
	@echo "  make up               - Start all services (PostgreSQL + Redis + API)"
	@echo "  make down             - Stop all services"
	@echo "  make logs             - View logs from all services"
	@echo "  make logs-api         - View API logs only"
	@echo "  make shell            - Open bash shell in API container"
	@echo "  make db-shell         - Open PostgreSQL shell"
	@echo "  make redis-shell      - Open Redis CLI"
	@echo "  make test-docker      - Run tests in Docker container"
	@echo "  make migrate-docker   - Run migrations in Docker container"
	@echo "  make restart-api      - Restart API service only"
	@echo "  make build            - Build Docker images"
	@echo "  make rebuild          - Rebuild and restart all services"
	@echo "  make ps               - Show running services"
	@echo "  make clean-docker     - Clean Docker volumes and containers"
	@echo "  make clean-docker-all - Remove everything including images"
	@echo ""
	@echo "💻 Local Development (Without Docker):"
	@echo "  make check-deps       - Check which dependencies are installed"
	@echo "  make install-deps     - Auto-install Poetry (if missing)"
	@echo "  make install-poetry   - Auto-install Poetry only"
	@echo "  make install          - Install project packages (requires Poetry)"
	@echo "  make run              - Start development server (localhost:8000)"
	@echo ""
	@echo "Development:"
	@echo "  make dev              - Alias for 'make run'"
	@echo "  make stop             - Stop the development server"
	@echo "  make restart          - Restart the development server"
	@echo "  make migrate          - Run database migrations"
	@echo ""
	@echo "Testing:"
	@echo "  make test             - Run backend tests"
	@echo "  make test-backend     - Run backend Python tests only"
	@echo "  make test-integration - Run integration tests only"
	@echo "  make test-all         - Run ALL tests (unit + api + cli + integration)"
	@echo "  make test-coverage    - Run tests with coverage reports"
	@echo "  make test-unit        - Run unit tests only"
	@echo "  make test-unit-fast   - Run fast unit tests (skip slow password tests)"
	@echo "  make test-unit-file   - Run specific unit file (FILE=path/to/test.py)"
	@echo "  make test-with-timing - Run tests with timing information"
	@echo "  make pre-commit       - Run fast tests for pre-commit hook"
	@echo ""
	@echo "Maintenance:"
	@echo "  make clean            - Clean test artifacts and temp files"
	@echo "  make clean-weekly     - Weekly maintenance cleanup"
	@echo "  make clean-all        - Deep clean (stop server + remove dependencies)"
	@echo "  make help             - Show this help message"
	@echo ""
	@echo "Manual Dependency Installation (if auto-install fails):"
	@echo "  Poetry:  curl -sSL https://install.python-poetry.org | python3 -"
	@echo "  Python:  brew install python@3.11"
	@echo ""
