.PHONY: sync lint type test verify live es-up es-down es-test

sync:
	uv sync

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

type:
	uv run mypy src

test:
	uv run pytest -v

verify: lint type test

live:
	uv run pytest -m live -v tests/live

# Elasticsearch for integration tests and eval runs (the VM; see docs/SETUP.md)
es-up:
	docker compose up -d elasticsearch
	./scripts/wait_es.sh

es-down:
	docker compose down

es-test:
	uv run pytest -m es -v
