# CCAT — Copper Country Angel Tree pipeline

Web application and pipeline for the CCAT gift-giving program. Full behavior
and writer ownership are documented in `pipeline_architecture.md`; the
refactor that introduced the current stack is described in
`temp/web_server_refactor.md` with its decisions in
`temp/refactor_decisions.md`.

## Architecture

```text
Vue 3 + TypeScript SPA (frontend/)
          |  same-origin HTTPS
          v
FastAPI application (src/ccat/api/)  ── auth/RBAC, REST DTOs, OpenAPI
          |
application services (src/ccat/services/)
          |
SQLAlchemy repositories + Alembic (src/ccat/db/, alembic/)
          |
SQLite (active store after cutover)   |   workbook (legacy import/export only)
          |
durable job worker (src/ccat/jobs/) ── Zite, ZeroBounce, Nominatim, OpenCode, LibreOffice
```

- **Domain logic** (`src/ccat/domain/`, `preparation.py`, `match.py`,
  `screening.py`, `child_info_generate.py`, `sponsor_packets.py`, `stats.py`)
  is unchanged and reused by both the workbook path and the database path.
- **Storage** is season-scoped in SQLite: `raw_*` acquisition snapshots,
  complete `final_*` stores (ignored, orphaned, duplicate, raw-missing and
  pending entities all remain stored), diagnostics/logs, match runs, jobs,
  artifacts, clarification state, users and sessions.
- **Raw is source; final is truth; logs are history.**
- The workbook stays as an import/export format only:
  `ccat db-import` migrates it into SQLite (validated, hash-guarded,
  idempotent per source hash) and `ccat workbook-export` writes the read-only
  reverse export used as rollback evidence.

## Setup

```sh
direnv allow        # devbox + uv environment (.envrc)
uv sync             # create .venv and install dependencies
```

Frontend (optional, for UI development):

```sh
cd frontend && npm install && npm run dev   # Vite on :5173, proxies /api
```

## Commands

```sh
ccat acquire       [--config PATH]  # fetch Zite tables verbatim -> raw_*
ccat screen        [--config PATH] [--redo ID] [--test] [--overwrite]
                   [--skip-email-check] [--skip-address-check]
                   # prepare raw in memory, append/merge full finals,
                   # refresh duplicate/DOB diagnostics and validation logs,
                   # then screen only children with screened=false
ccat audit         [--config PATH] [--test] [--restart]
                   # legacy NiceGUI editor (workbook mode only)
ccat match         [--config PATH] [--test] [--overwrite] [--skip-match]
                   # patch assignment fields; archive/report as configured
ccat generate      [--config PATH] [--test] [--overwrite]
                   [--sponsor ID] [--max-workers N]
                   # child information PDFs, generation markers, packets
ccat stats         [--config PATH] [--raw]
ccat clarify       [--config PATH] [--generate] [--overwrite]
ccat clarify-export [--config PATH] [--output PATH]

ccat db-import     --from WORKBOOK [--db URL] [--force] [--backup PATH]
                   [--report PATH]     # workbook -> SQLite migration batch
ccat workbook-export --output PATH [--db URL] [--report PATH] [--force]
ccat db-upgrade    [--config PATH]     # controlled Alembic upgrade to head
ccat serve         [--config PATH]     # FastAPI server (replaces both UIs)
ccat worker        [--config PATH]     # durable job worker loop
```

`--test` means the first unit of work: first family for screening, first
sponsor for match, first PDF for generate. It is **not** a dry run and can
mutate persistent state. `--overwrite` is an explicit rework control, not
blanket data replacement: human edits, controls, and module-owned fields
retain their protections. Failed or interrupted AI/PDF work remains
retryable; completed work remains complete.

Duplicates are flagged, never merged. ZeroBounce and Nominatim reuse their
logs, so reruns do not repeat successful external checks; complete verbatim
responses remain stored.

## Web application

- `ccat serve` starts the FastAPI app (`/api/v1`, OpenAPI at
  `/api/v1/docs`); the Vue SPA is served from the same origin when
  `frontend/dist` exists.
- `ccat worker` runs the durable job worker. Jobs are queued in the
  database, leased with heartbeats, cancelled cooperatively, and report
  typed, retryable errors.
- Authentication is cookie-session based. In `auth_mode: dev` the two
  bootstrap accounts (`cmendres`, `lmboswell`) sign in without a password —
  development only. Roles: `read_only`, `auditor`, `coordinator`, `admin`.
- Every entity edit is versioned: the client sends `If-Match`, and a stale
  write returns `412` with the current server row instead of overwriting
  another user's work.

## Migration

- `ccat audit-migrate --from OLD [--force]` bootstraps an old v1 workbook
  directly into persistent finals.
- `ccat consolidate-migrate --from OLD --output NEW [--force --backup
  BACKUP] [--report REPORT]` converts a full v2 `adj_*` workbook into a
  separate persistent workbook. It never mutates the input.
- `ccat db-import --from WORKBOOK` imports a persistent-final (v2) workbook
  into SQLite as one validated migration batch. It hashes the source before
  and after, refuses a nonempty target without `--force` plus a verified
  `--backup`, writes a PII-free JSON report, and reuses a completed batch
  when the same source hash is imported again.

Cutover procedure and rollback are in `docs/cutover.md`; deployment and
operations are in `docs/deployment.md`; known limitations and the
PostgreSQL trigger criteria are in `docs/limitations.md`.
