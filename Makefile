.PHONY: install check test lint typecheck build contracts migrate

install:
	uv sync --locked
	npm ci

lint:
	uv run ruff check apps tests scripts migrations
	uv run ruff format --check apps tests scripts migrations
	npm run lint

typecheck:
	uv run mypy
	npm run typecheck

test:
	uv run pytest --cov=spm_api --cov=spm_worker --cov-report=term-missing
	npm test

contracts:
	npm run check:contracts

build:
	npm run build

migrate:
	uv run spm-manage migrate

check: lint typecheck contracts test build
