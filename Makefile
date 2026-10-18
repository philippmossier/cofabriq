.PHONY: setup up down check check-ui ui fmt health

setup:  ## install deps into .venv, create .env if missing
	uv sync
	test -f .env || cp .env.example .env
	git config core.hooksPath .githooks
	cd ui && pnpm install --frozen-lockfile

up:     ## start Postgres+pgvector and Neo4j in the background
	podman compose up -d

down:   ## stop containers (volumes kept)
	podman compose down

check:  ## lint, format check, typecheck, tests
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright
	uv run pytest
	@if git diff --quiet HEAD -- ui src/cofabriq/serve.py; then \
		echo "ui: unchanged, skipped"; else $(MAKE) check-ui; fi

check-ui: ## the UI: client regenerated from the schema, types, lint
	uv run cofabriq openapi --out ui/openapi.json
	cd ui && pnpm exec orval && pnpm exec tsc -b && pnpm lint
	git diff --exit-code -- ui/openapi.json ui/src/api/generated.ts

ui:     ## build the UI into ui/dist (served by `cofabriq serve`)
	cd ui && pnpm build

fmt:    ## autofix lint + format
	uv run ruff check --fix .
	uv run ruff format .

health: ## check DB connectivity
	uv run cofabriq health
