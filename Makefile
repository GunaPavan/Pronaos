.PHONY: help install dev up down logs lint fmt typecheck test test-cov test-integration \
        build docker-build docker-run clean reset-db precommit ci

PY := python
VENV ?= .venv

help:
	@echo "Pronaos — common developer tasks"
	@echo ""
	@echo "  make install           Install package + dev deps into .venv"
	@echo "  make dev               Run the gateway locally with reload"
	@echo "  make up                Start local infra stack (postgres/redis/qdrant/otel/grafana)"
	@echo "  make down              Stop local infra"
	@echo "  make logs              Tail docker-compose logs"
	@echo "  make lint              Ruff lint"
	@echo "  make fmt               Ruff format"
	@echo "  make typecheck         mypy strict"
	@echo "  make test              Run unit tests"
	@echo "  make test-cov          Run tests with coverage report"
	@echo "  make test-integration  Run integration tests (requires 'make up')"
	@echo "  make docker-build      Build production container image"
	@echo "  make precommit         Install and run pre-commit hooks"
	@echo "  make ci                Full local CI: lint + typecheck + test"
	@echo "  make clean             Remove caches and build artifacts"

install:
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e ".[dev]"

dev:
	$(VENV)/bin/uvicorn pronaos.main:app --reload --host 0.0.0.0 --port 8080

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

lint:
	$(VENV)/bin/ruff check src tests

fmt:
	$(VENV)/bin/ruff format src tests
	$(VENV)/bin/ruff check --fix src tests

typecheck:
	$(VENV)/bin/mypy src

test:
	$(VENV)/bin/pytest

test-cov:
	$(VENV)/bin/pytest --cov --cov-report=term-missing --cov-report=xml

test-integration:
	$(VENV)/bin/pytest -m integration

docker-build:
	docker build -t pronaos:dev .

docker-run:
	docker run --rm -p 8080:8080 --env-file .env pronaos:dev

precommit:
	$(VENV)/bin/pre-commit install
	$(VENV)/bin/pre-commit run --all-files

ci: lint typecheck test

clean:
	rm -rf .mypy_cache .pytest_cache .ruff_cache .coverage htmlcov coverage.xml dist build
	find . -type d -name __pycache__ -exec rm -rf {} +
