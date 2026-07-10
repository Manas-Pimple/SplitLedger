.PHONY: dev-deps db-up db-down migrate test lint run

dev-deps:
	cd backend && uv sync
	cd frontend && npm install

db-up:
	docker compose -f docker-compose.dev.yml up -d --wait

db-down:
	docker compose -f docker-compose.dev.yml down

migrate:
	cd backend && uv run alembic upgrade head

test:
	cd backend && uv run pytest
	cd frontend && npm test -- --run

lint:
	cd backend && uv run ruff check . && uv run mypy app tests
	cd frontend && npm run lint

run:
	cd backend && uv run uvicorn app.main:app --reload --port 8000
