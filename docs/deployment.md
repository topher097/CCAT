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
| `/app/config/ccat_docker.json` | volume `ccat-config` (live config) | yes |
| `/app/config-seed/ccat_docker.json` | bind mount of the repo file, read-only | no |
| `/app/data` | bind mount, read-only | no |

> **Settings tab:** the admin Settings tab rewrites the live configuration
> file (`/app/config/ccat_docker.json`). That file is a regular file on the
> `ccat-config` volume, so the atomic write (temp file + rename) succeeds
> and every service — api, worker, db-upgrade — reads the same file. It
> used to be a bind mount of the repo file: a rename onto a bind-mounted
> file fails with `EBUSY` ("Device or resource busy"), and the read-only
> prod mount rejected the write outright, so every save failed with
> `500 config_write_failed`.
>
> The repo file (`config/ccat_docker.json`) is mounted read-only as the
> *seed*. At container start the entrypoint applies it when it is newer
> than the live copy, so an operator edit wins after a restart and a
> Settings-tab save survives one — the most recent write wins. Copy the
> repo file into a running container to deploy it without a restart:
>
> ```sh
> docker compose --profile prod cp ./config/ccat_docker.json \
>   api:/app/config/ccat_docker.json
> ```

> **Frontend stage:** when `frontend/package.json` is absent the stage logs
> `frontend/package.json absent` and produces an empty dist; the image and API
> still build. Production requires a real build — `cd frontend && npm ci &&
> npm run build` before `--profile prod up` (or `docker compose build`).
> Without it Caddy serves 404s.

## 3. Development profile (live reload)

```sh
direnv allow            # .envrc carries the public settings
docker compose --profile dev up --build
# https://localhost:8443  (API + Vite HMR behind Caddy's internal CA)
```

- `api-dev`: uvicorn `--reload` with `./src` mounted at `/app/src`
  (`PYTHONPATH=/app/src` overrides the wheel).
- `web`: Vite dev server with HMR; Caddy proxies non-`/api` traffic to it.
  Its `node_modules` is a container-only volume, so the image's musl
  binaries never overwrite the host's install (the local `vitest` run).
- `worker`: runs the image command unchanged; **do not hot-reload a worker**
  while it holds a job lease — rebuild/restart when idle.
- `db-upgrade` runs once before the API starts.
- Only in this profile do the no-password dev accounts (`cmendres`,
  `lmboswell`) exist; they come from `api.auth_mode: dev`.
- `auth_mode: dev` also means **no login at all**: a request without a
  session cookie is authenticated as `cmendres` (admin), so the SPA
  never stops at a login screen. A dev login still works and outranks
  the auto identity.

Logs: `docker compose --profile dev logs -f api-dev worker`.

## 4. Production profile

```sh
# 1. Secrets go in .envrc.private (gitignored, sourced by .envrc):
#    CCAT_SESSION_SECRET, ZITE_API_KEY, ZEROBOUNCE_API_KEY,
#    OPENCODE_SERVER_PASSWORD, OIDC_CLIENT_SECRET.
#    Public settings live in .envrc.
$EDITOR .envrc.private
python -c "import secrets; print(secrets.token_urlsafe(48))"   # session secret
direnv allow

# 2. Build immutable assets + image
(cd frontend && npm ci && npm run build)
docker compose --profile prod build
docker compose --profile prod up -d

# 3. Verify
curl -fsS https://$CCAT_DOMAIN/api/v1/healthz
curl -fsS https://$CCAT_DOMAIN/api/v1/readyz
docker compose --profile prod ps
```

Compose reads the shell environment (direnv puts it there), so `.envrc` /
`.envrc.private` are the only configuration files — there is no `.env`.

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
| `opencode` | ✔ | ✔ | LLM server for screening; internal only |

TLS:

- `CCAT_ACME_EMAIL` set → Caddy requests Let's Encrypt certificates for
  `CCAT_DOMAIN` (ports 80/443 must be reachable; DNS must point at the host).
- `CCAT_ACME_EMAIL` empty → `tls internal` (Caddy's local CA). Fine for a
  single-host rehearsal; browsers must trust the CA or use `-k`.

The API is never published directly; only Caddy binds host ports.

### opencode server (LLM behind AI screening)

The `screen` job (module 2) and the clarify writer (module 7) need an LLM.
The CCAT image ships no opencode CLI, so compose runs a headless **opencode
v2** server as a service; the app reaches it at `http://opencode:4097`
(`OPENCODE_BASE_URL`, set for api/worker by compose).

- **Image.** Built by compose from
  [`roborew/opencode-server`](https://github.com/roborew/opencode-server)
  (git build context). The first build clones that repo and its config repo
  and installs node/opencode — expect several minutes. Rebuild with
  `docker compose --profile prod build opencode` after upstream changes.
- **Auth.** Set `OPENCODE_SERVER_PASSWORD` in `.envrc.private`: the server
  then requires basic auth and the same value is handed to api/worker. The
  variable is **not optional** — with an empty value opencode invents a
  random password and every client request is rejected with 401.
- **Credentials.** The host's `~/.local/share/opencode/auth.json` (provider
  API keys of the local opencode install) is mounted read-only at
  `/run/opencode-auth.json` and copied into the container's own credential
  store on every start — the host file is mode 600, so only root can read it
  and the copy happens before the upstream entrypoint drops privileges.
  Point `OPENCODE_AUTH_FILE` at another file to override.
- **Account.** A key alone is not an account: opencode also needs the
  console session that carries the workspace (`x-opencode-org-id`), and it
  keeps that in its own store, not in `auth.json`. Copy the host's
  credentials into the container once (the container keeps them in the
  `opencode-data` volume):

  ```sh
  opencode auth export > /tmp/creds.json          # on the host
  docker compose --profile prod cp /tmp/creds.json opencode:/tmp/creds.json
  docker compose --profile prod exec opencode opencode auth import /tmp/creds.json
  docker compose --profile prod restart opencode
  ```

  `opencode auth login` inside the container does the same interactively.
  Until that is done the server only serves the anonymous free tier (usage
  limited) and the `opencode-go` provider answers "Invalid credential".
- **Model.** The pinned opencode v2.3.0 serves `glm-5.3-flash` through the
  `opencode-go` provider, not through `opencode` — that is why
  `config/ccat_docker.json` asks for `opencode-go/glm-5.3-flash`. Check
  what the server offers with
  `docker compose --profile prod exec opencode opencode models`.
- **Ordering.** `worker` waits for `condition: service_healthy` on
  `opencode`, so screening jobs never start against a cold server.
- **Verify.**

  ```sh
  docker compose --profile prod exec opencode \
    curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4097/api/info
  docker compose --profile prod logs opencode | tail -20
  ```

  `401` means the server is up and asking for the password; `200` means it is
  up without auth.

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

# live configuration (the Settings tab writes it; the repo file is only
# the seed)
docker compose --profile prod cp worker:/app/config/ccat_docker.json \
  ./backups/config-$(date +%Y%m%d-%H%M).json
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
docker compose --profile prod cp ./backups/config-YYYYmmdd-HHMM.json worker:/app/config/ccat_docker.json
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
- The `opencode` healthcheck asks `GET /api/info` inside the container:
  `401` = up with basic auth, `200` = up without auth.
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
      is still a stub (decision D2, `[human]`). Dev mode grants every
      request the admin auto identity (`cmendres`) without a login, so
      the whole stack must stay behind localhost/a trusted network until a
      real auth mode replaces `dev`. Do not expose the stack beyond
      localhost/a trusted network until OIDC lands.
- [ ] Secrets (`ZITE_API_KEY`, `ZEROBOUNCE_API_KEY`, `CCAT_SESSION_SECRET`,
      OIDC, `OPENCODE_SERVER_PASSWORD`) live in `.envrc.private` only —
      never image layers, Vue build args, compose files, logs or reports.
- [ ] The host `~/.local/share/opencode/auth.json` is a secret: it is mounted
      read-only into the opencode service and copied there; never commit it.
      Set `OPENCODE_SERVER_PASSWORD` unless the compose network is private.
- [ ] Rotate provider/session secrets on staff changes; restart api/worker.
- [ ] Backups encrypted and access-controlled; retention schedule agreed
      (decision D9, `[human]`).
- [ ] API/worker/opencode ports unpublished; only Caddy binds host ports.
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
  (see checklist), which also means it hands every request the admin auto
  identity. Treat `docker compose --profile prod` on a public host as
  blocked.
- Frontend builds are conditional at image-build time; a production image
  built before the SPA landed must be rebuilt.
