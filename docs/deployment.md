# Deployment runbook

Operator guide for the Docker/Compose stack (plan §13). Companion:
[`cutover.md`](cutover.md) for the production cutover rehearsal.

## 1. Prerequisites

- Docker Engine 24+ with the Compose v2 plugin (`docker compose version`).
- Host disk on local storage for the SQLite volume. **No NFS/SMB/Gluster for
  `ccat-db`** — SQLite locking over network filesystems corrupts data.
- A hostname (prod) and, for dev, a browser that can trust Caddy's local CA
  (or `-k` for the rehearsal).
- The image needs network access at build time (PyPI, npm, Debian packages).

## 2. Image layout

Built in three stages (`Dockerfile`):

| Stage | Contents |
|---|---|
| `frontend` | Vue SPA built with Node 24 → `/build/dist` |
| `python-builder` | `uv sync --frozen --no-dev` venv + `ccat` wheel |
| `runtime` | Python 3.12-slim, LibreOffice, non-root (uid 10001) |

The runtime installs the **wheel** (not the checkout). The application
derives its deployment root from the module path (`parents[3]` of
`ccat/db/engine.py`), which for an installed wheel is the venv's
`lib/python3.12` directory; the image links `alembic/` and
`frontend/dist` there so migrations and the SPA resolve outside the repo
checkout. The image self-checks `alembic/`, the `.ott` template and
`soffice` at build time.

Runtime paths (all config paths are absolute — no CWD dependence):

| Path | Source | Writable |
|---|---|---|
| `/data` | volume `ccat-db` (`ccat.sqlite3`, WAL files) | yes |
| `/artifacts` | volume `ccat-artifacts` (PDFs, packets, reports, exports) | yes |
| `/logs` | volume `ccat-logs` (`ccat.log`) | yes |
| `/app/config/ccat_docker.json` | bind mount, read-only | no |
| `/app/data` | bind mount, read-only | no |

> **Frontend stage:** until the Vue app lands, the stage logs
> `frontend/package.json absent` and produces an empty dist; the image and API
> still build. Production requires a real build — `cd frontend && npm ci &&
> npm run build` before `--profile prod up` (or `docker compose build` once
> `frontend/package.json` exists). Without it Caddy serves 404s.

## 3. Development profile (live reload)

```sh
cp .env.example .env      # optional in dev; fill placeholders later
docker compose --profile dev up --build
# https://localhost:8443  (API + Vite HMR behind Caddy's internal CA)
```

- `api-dev`: uvicorn `--reload` with `./src` mounted at `/app/src`
  (`PYTHONPATH=/app/src` overrides the wheel).
- `web`: Vite dev server with HMR; Caddy proxies non-`/api` traffic to it.
- `worker`: runs the image command unchanged; **do not hot-reload a worker**
  while it holds a job lease — rebuild/restart when idle.
- `db-upgrade` runs once before the API starts.
- Only in this profile do the two no-password dev accounts (`cmendres`,
  `lmboswell`) exist; they come from `api.auth_mode: dev`.

Logs: `docker compose --profile dev logs -f api-dev worker`.

## 4. Production profile

```sh
# 1. Fill secrets (never commit .env)
cp .env.example .env && $EDITOR .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # session secret

# 2. Build immutable assets + image
(cd frontend && npm ci && npm run build)
docker compose --profile prod build
docker compose --profile prod up -d

# 3. Verify
curl -fsS https://$CCAT_DOMAIN/api/v1/healthz
curl -fsS https://$CCAT_DOMAIN/api/v1/readyz
docker compose --profile prod ps
```

Profiles at a glance:

| Service | `dev` | `prod` | Notes |
|---|---|---|---|
| `api` | – | ✔ | uvicorn, no `--reload`, healthcheck |
| `api-dev` | ✔ | – | uvicorn `--reload`, source mount |
| `worker` | ✔ | ✔ | no reload; 150 s graceful stop |
| `db-upgrade` | ✔ | ✔ | `restart: "no"`, runs once |
| `caddy` | – | ✔ | :80/:443, Let's Encrypt or internal CA |
| `caddy-dev` | ✔ | – | :8443, `tls internal`, proxies Vite |
| `web` | ✔ | – | Vite HMR |

TLS:

- `CCAT_ACME_EMAIL` set → Caddy requests Let's Encrypt certificates for
  `CCAT_DOMAIN` (ports 80/443 must be reachable; DNS must point at the host).
- `CCAT_ACME_EMAIL` empty → `tls internal` (Caddy's local CA). Fine for a
  single-host rehearsal; browsers must trust the CA or use `-k`.

The API is never published directly; only Caddy binds host ports.

## 5. Migrations (controlled release step)

Schema migrations are **not** run by the API/worker replicas. Compose runs a
one-shot `db-upgrade` service (`ccat db-upgrade`) and `api`/`worker` wait for
`condition: service_completed_successfully`.

```sh
docker compose --profile prod run --rm db-upgrade
```

`ccat db-upgrade` is idempotent and touches schema only. Data import is
separate (`ccat db-import`, see `cutover.md`). `CCAT_RUN_MIGRATIONS=1` on the
entrypoint exists for bare-container bootstraps; do not combine it with
multiple replicas.

## 6. Backup and restore

**Never copy a live SQLite file.** Use the online backup API so WAL content is
consistent, and copy artifacts as a separate tree:

```sh
# database (online backup, safe while api/worker run)
docker compose --profile prod exec -T worker \
  python -c "import sqlite3; src=sqlite3.connect('/data/ccat.sqlite3'); \
dst=sqlite3.connect('/tmp/backup.sqlite3'); src.backup(dst); dst.close()" \
  && docker compose --profile prod cp worker:/tmp/backup.sqlite3 \
       ./backups/ccat-$(date +%Y%m%d-%H%M).sqlite3

# artifacts (PDFs, packets, reports) and logs
docker compose --profile prod cp worker:/artifacts ./backups/artifacts-$(date +%Y%m%d-%H%M)
docker compose --profile prod cp worker:/logs      ./backups/logs-$(date +%Y%m%d-%H%M)
```

A database dump alone is insufficient: packets/PDFs are not in SQLite.
Encrypt backups at rest and restrict access — they contain minors' data.

Restore:

```sh
docker compose --profile prod stop api worker
docker compose --profile prod run --rm --entrypoint sh db-upgrade -c \
  "rm -f /data/ccat.sqlite3 /data/ccat.sqlite3-wal /data/ccat.sqlite3-shm"
docker compose --profile prod cp ./backups/ccat-YYYYmmdd-HHMM.sqlite3 db-upgrade:/data/ccat.sqlite3
docker compose --profile prod cp ./backups/artifacts-YYYYmmdd-HHMM/. db-upgrade:/artifacts
docker compose --profile prod run --rm db-upgrade       # schema at head
docker compose --profile prod up -d api worker
curl -fsS https://$CCAT_DOMAIN/api/v1/readyz
```

## 7. Upgrade and rollback

Upgrade: backup → `docker compose --profile prod pull/build` → `db-upgrade`
→ `up -d` → verify `/readyz` and one read-only page.

Rollback (image):

```sh
CCAT_IMAGE_TAG=previous docker compose --profile prod up -d
```

Rollback after new writes is a data problem, not an image problem: if the new
schema/rows are already in use, take an explicit reverse path
(`ccat workbook-export` + validation, plan §10) instead of restoring the old
workbook, which is only a snapshot. See `cutover.md` §7.

## 8. Health checks and observability

- `GET /api/v1/healthz` — liveness only (used by the compose healthcheck).
- `GET /api/v1/readyz` — database reachable, migration at head, artifact root
  writable; 503 with check names (no internals) when not ready.
- Logs rotate via the json-file driver (10 MB × 5 per service). Request logs
  carry `request_id`, method, path, status, duration, user/roles — no payloads.
- Watch: queue age/failures/heartbeat (`job` table), disk space on
  `ccat-db`/`ccat-artifacts`, `database is locked` errors, LibreOffice
  conversion failures, worker restarts.
- `ccat.log` on the `ccat-logs` volume mirrors stderr for pipeline detail.

## 9. Single-host SQLite constraints

- **One writer.** Keep exactly one `worker` and one API process; do not add
  uvicorn `--workers` or scale replicas. WAL allows concurrent readers, not
  multiple writers.
- Connections already set `journal_mode=WAL`, `busy_timeout=15000`,
  `synchronous=NORMAL`, `foreign_keys=ON` (`ccat/db/engine.py`).
- The database must live on a local filesystem volume; network volumes break
  locking and `fsync` semantics.
- Scale-out to PostgreSQL is a planned portability follow-up (decision D11),
  not a configuration switch.

## 10. Security checklist (plan §8)

- [ ] TLS terminates at Caddy; HSTS/security headers on (in `Caddyfile`).
- [ ] Session cookie is `HttpOnly`, `SameSite`, `Secure` outside dev auth
      mode; `CCAT_SESSION_SECRET` is random and **not** in the image.
- [ ] CSRF header enforced on cookie-authenticated mutations (already in the
      API); keep the API on the same origin as the SPA.
- [ ] Login rate limiting active (in-process in the API; single-process only).
- [ ] No dev no-password accounts on a public route. `api.auth_mode: dev` in
      `config/ccat_docker.json` is a **pre-production gate**: the OIDC adapter
      is still a stub (decision D2, `[human]`). Do not expose the stack beyond
      localhost/a trusted network until OIDC lands.
- [ ] Secrets (`ZITE_API_KEY`, `ZEROBOUNCE_API_KEY`, `CCAT_SESSION_SECRET`,
      OIDC) enter via environment only — never image layers, Vue build args,
      compose files, logs or reports.
- [ ] Rotate provider/session secrets on staff changes; restart api/worker.
- [ ] Backups encrypted and access-controlled; retention schedule agreed
      (decision D9, `[human]`).
- [ ] API/worker ports unpublished; only Caddy binds host ports.
- [ ] Logs contain no child/family payloads, emails, addresses or AI prompts.

## 11. LibreOffice dependency

`soffice` renders `.ott` info sheets through py3o; Python wheels alone cannot
convert documents. The runtime installs `libreoffice-writer` plus
`fonts-liberation`/`fonts-dejavu-core` (metric-compatible substitutes keep page
layout stable). Symptoms of a broken runtime: `InfoSheetError`, empty PDFs, or
`soffice` not found in worker logs. Test after any base-image change:

```sh
docker compose --profile prod run --rm --entrypoint soffice worker --version
```

## 12. Known deviations / follow-ups

- `Caddyfile.dev` is an extra dev-only file: Caddy cannot switch from
  `file_server` to a Vite proxy by env var alone.
- The prod profile runs the API in `dev` auth mode until OIDC is implemented
  (see checklist). Treat `docker compose --profile prod` on a public host as
  blocked.
- Frontend builds are conditional at image-build time; a production image
  built before the SPA landed must be rebuilt.
