# Agent Development Guide

A file for [guiding coding agents](https://agents.md/).

## Commands

- **Formatting (Python)**: `ruff format`
- **Linting (Python)**: `ruff check --unsafe-fixes`
- **Type Checking (Python)**: `ty check`
- **Local virtualenv (Python)**: `just ready-py`
- **Environment Loading (system)**: `direnv allow`
- **Full test suite (Python)**: `pytest` (pre-commit runs it on every commit)
- **Frontend install/build/test**: `cd frontend && npm install && npm run build && npx vitest run`
- **Frontend API client**: `cd frontend && npm run gen:api` (regenerates
  `src/api/schema.d.ts` from `frontend/openapi.json`; refresh
  `frontend/openapi.json` with `.venv/bin/python scripts/export_openapi.py`
  after changing API DTOs)

## Directory Structure

- Python core code: `src/`
  - `src/ccat/domain/` — shared pure primitives (age, IDs, DOB, duplicates)
  - `src/ccat/services/` — use-case orchestration shared by CLI and API
    (`db_pipeline.py`, `db_audit.py`, `db_generate.py`, `db_stats.py`,
    `db_clarify.py`, `results.py`, `ports.py`)
  - `src/ccat/db/` — SQLAlchemy models, repositories, unit of work, engine,
    importer/exporter, CLI helpers
  - `src/ccat/api/` — FastAPI app, routes, DTOs, auth/RBAC, error mapping
  - `src/ccat/jobs/` — durable job registry, worker, handlers
  - `src/ccat/engines/` — ZeroBounce/Nominatim adapters (no `scripts/` paths)
- Schema migrations: `alembic/` (run them with `ccat db-upgrade`)
- Vue SPA: `frontend/` (generated API types in `frontend/src/api/schema.d.ts`)
- Deployment: `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `docs/`
- Shared data directory: `../IO/`

## Ground rules

- One active store. After cutover the API and worker use SQLite only; the
  workbook is an import/export format (`ccat db-import`,
  `ccat workbook-export`).
- Do not edit a live database by hand — add an Alembic revision.
- Entity writes are optimistic: every persistent row carries `row_version`,
  and updates go through repository ownership rules.
- Keep the domain modules pure of HTTP/ORM concerns; services orchestrate,
  repositories persist, algorithms compute.

## Environments

- New shells should utilize the automatic initialization via `.envrc` and direnv. Use the `Environment Loading` command above.
- Use `uv` for local virtual environment management for python, use the local virtual environment for python development.

## Code Style

- Python

   - Use the `ruff` formatter and linter, respecting all rules present in the `ruff.toml` config file.
   - Use the `ty` type checker to ensure type safety.
   - Use CamelCase for classes and snake_case for functions and variables.
   - Use explicit error handling with try-except blocks.
   - Fully annotate functions' and methods' input and output signatures.
   - Use google docstring formatting for all functions and methods.

- Tests

   - Use the `pytest` framework and place tests within the `test` directory.
   - Classes suffixed with `Test`
   - Methods and functiouns prefixed with `test_`
