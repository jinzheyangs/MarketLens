PYTHON ?= python

.PHONY: install install-dev run worker agent-worker events-worker monitor-worker retention rotate-notification-keys ops-metrics security-scan lint format typecheck test test-unit test-integration migrate revision up down logs

install:
	$(PYTHON) -m pip install .

install-dev:
	$(PYTHON) -m pip install -e ".[dev]"

run:
	$(PYTHON) -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log

worker:
	$(PYTHON) -m app.collector.worker

agent-worker:
	$(PYTHON) -m app.agent.worker

events-worker:
	$(PYTHON) -m app.events.worker

monitor-worker:
	$(PYTHON) -m app.monitoring.worker

retention:
	$(PYTHON) -m app.ops.retention

rotate-notification-keys:
	$(PYTHON) -m app.ops.notification_keys

ops-metrics:
	$(PYTHON) -m app.ops.metrics_exporter

security-scan:
	$(PYTHON) -m pip_audit --local

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .

typecheck:
	$(PYTHON) -m mypy app

test:
	$(PYTHON) -m pytest

test-unit:
	$(PYTHON) -m pytest -m "not integration"

test-integration:
	$(PYTHON) -m pytest -m integration

migrate:
	$(PYTHON) -m alembic upgrade head

revision:
	$(PYTHON) -m alembic revision --autogenerate -m "$(m)"

up:
	docker compose up -d postgres qdrant

down:
	docker compose down

logs:
	docker compose logs -f --tail=100
