# Cutover runbook (Phase 5 rehearsal and production switch)

> **Status.** Executed in November 2026; kept as the record of how the
> workbook became an import/export format. The live store is SQLite.

Two phases, both rehearsed: **import + operate against SQLite** using a copy
of the workbook, then flip `storage.mode` from `workbook` to `db`. The Excel
workbook stops being truth at the flip; it becomes an immutable snapshot and a
read-only export format (`ccat workbook-export`).

Do the whole runbook once as a rehearsal. The rehearsal mutates only the
imported database; the workbook copy is never written.

## 1. Freeze writers

1. Announce a write freeze: no audit edits, no pipeline runs against the
   workbook (CLI or API).
2. Confirm nobody holds a workbook open (`lsof` / close Excel).
3. Stop any remaining pipeline/worker processes. Leave the workbook
   untouched from now on.

## 2. Back up the sources of truth

```sh
STAMP=$(date +%Y%m%d-%H%M)
mkdir -p backups/$STAMP
cp -a output/ccat_2026_v2.xlsx          backups/$STAMP/workbook.xlsx
cp -a config/ccat_2026_v2.json          backups/$STAMP/config.json
cp -a data                              backups/$STAMP/data-templates
cp -a output/child_info_sheets output/sponsor_packets output/reports \
      output/clarify_emails             backups/$STAMP/artifacts/ 2>/dev/null || true
sha256sum backups/$STAMP/workbook.xlsx  | tee backups/$STAMP/SHA256SUMS
```

Record the config hash too — jobs pin it. Copy the workbook to the deployment
host as the import input; keep the original where it is (never import the
live workbook in place).

The importer reads the workbook and the artifact tree from the `ccat-artifacts`
volume, so copy both in before §3:

```sh
docker compose --profile prod cp ./backups/$STAMP/workbook.xlsx \
  worker:/artifacts/workbook/ccat_2026_v2.xlsx
docker compose --profile prod cp ./backups/$STAMP/artifacts/. worker:/artifacts/
```

The workbook destination must not already exist as a directory — `docker cp`
then copies the file *into* it and the importer finds no workbook.

## 3. Import into a database (via a copy)

`ccat db-import` opens the source read-only, runs a validation gate, and writes
a JSON report. Always import a **copy**.

```sh
docker compose --profile prod build
docker compose --profile prod run --rm --entrypoint ccat worker \
  db-import \
  --config /app/config/ccat_docker.json \
  --from /artifacts/workbook/ccat_2026_v2.xlsx \
  --report /artifacts/rehearsal/import-report.json
```

The target DB is `storage.database_url` (`/data/ccat.sqlite3`). A non-empty
target is refused unless `--force` is paired with an existing `--backup`.

The import **merges**: the workbook wins for every key it carries, and rows the
database holds from a later acquisition survive (the final stores are
append-and-merge, never projections). A stored row that holds an angel id the
workbook assigns to another child releases it, so the audited assignments stay
intact. Both are reported: `merged_rows` counts the stored rows the workbook
does not carry, `angel_ids_released` lists the rows that gave up a letter.

## 4. Review the validation report

Open `/artifacts/rehearsal/import-report.json` (or
`docker compose --profile prod cp worker:/artifacts/rehearsal/import-report.json .`)
and check:

- counts: sponsor/family/child imported vs workbook rows;
- all validation checks pass (`validation` list is empty of failures);
- `merged_rows` and `angel_ids_released` show what the merge kept or gave up;
- logs (raw snapshot, four diagnostic/validation logs) and match report rows
  imported;
- import batch status `completed` and source SHA-256 matches
  `backups/$STAMP/SHA256SUMS`.

Do not proceed on a failed or short import.

## 5. Rehearse operations against the imported DB

Start the stack pointed at the imported database (no production writes — this
DB is disposable and will be re-imported for the real cutover):

```sh
docker compose --profile prod up -d db-upgrade api worker
curl -fsS http://localhost:8650/api/v1/readyz     # via caddy in prod shape
```

Log in as a dev account (`POST /api/v1/auth/login {"username":"cmendres"}`),
keep the session cookie, and take the CSRF token from `GET /api/v1/auth/me`.

Read-only smoke test first:

- [ ] `GET /api/v1/seasons/{season}/sponsors|families|children` (paged lists)
- [ ] One entity detail + diagnostics views
- [ ] Downloads: an existing info-sheet PDF, a sponsor packet
- [ ] `GET /api/v1/seasons/{season}/jobs` (history empty after import)
- [ ] `GET /api/v1/readyz` reports `database`, `migration_head`,
      `artifact_root` all true

Then guarded mutations / jobs, in this order, checking results against the
same operation on the golden fixture:

| Step | Call | Look for |
|---|---|---|
| acquire | `POST /api/v1/seasons/{season}/jobs {"job_type":"acquire"}` | completes, raw snapshot replaced, finals untouched |
| screen | `{"job_type":"screen"}` | per-child completion; baseline protection holds |
| match | `{"job_type":"match"}` | assignments + match archive/report columns |
| generate | `{"job_type":"generate"}` | real PDF markers only after real PDFs; packets built |
| stats | `{"job_type":"stats"}` | stats HTML/PDF matches legacy population |
| clarify | `{"job_type":"clarify_generate"}` | drafts stored, no email sent |

Also exercise one audit save (edit + save with `row_version`), one
ignore/unignore, and a stale-version save (must 409, not overwrite).

### Rollback drill (rehearsal)

Before any real writes: restore the old CLI/UI config, workbook, and artifact
tree from `backups/$STAMP` and confirm the old tooling still opens them. After
new DB writes, the old workbook is only a snapshot — rehearse the reverse path
instead: `ccat workbook-export --config ... --output /artifacts/rehearsal/rollback.xlsx
--report /artifacts/rehearsal/rollback-report.json` and validate it against
the plan §10 expectations (read-only export as evidence, never a second truth).

## 6. Switch the configuration

Make the config change explicit and reviewed:

```jsonc
// config/ccat_docker.json (or the operator's deployment config)
"storage": {
  "mode": "db",
  "database_url": "sqlite+aiosqlite:////data/ccat.sqlite3",
  "artifact_root": "/artifacts"
}
```

`config/ccat_docker.json` already ships `mode: db`; the same storage block
applies to every host. `mode` is informational only — the API and worker
always use SQLite.

Re-import for real (clean DB, same commands as §3), or keep the rehearsed DB
if no rehearsal writes were made. Then:

```sh
docker compose --profile prod up -d db-upgrade   # schema at head
docker compose --profile prod up -d api worker
docker compose --profile prod ps
curl -fsS https://$CCAT_DOMAIN/api/v1/readyz
```

The old NiceGUI apps are already removed, so no local editor can write the
workbook. Set `storage.mode: workbook` nowhere — the API/worker are DB-only.

## 7. Post-cutover verification checklist (plan §11.3)

Fill in during the rehearsal; repeat on cutover day.

| # | Criterion | Evidence (command/screen) | Rehearsal result | Cutover result |
|---|---|---|---|---|
| 1 | Backend and frontend suites, ruff, ty, Vue lint/build pass | `pytest`, `ruff check`, `ty check`, `npm run build` | | |
| 2 | No normal API operation reads/writes the workbook; no editor can mutate it | API routes on `SeasonStore` port; workbook is import/export only | | |
| 3 | No-change rerun preserves edits, controls, assignments, generation markers, raw-missing rows, external-call counts | re-run screen/match on golden fixture | | |
| 4 | Interrupted jobs resume; every generation marker has an authorized downloadable PDF | kill worker mid-job, restart; spot-check PDFs | | |
| 5 | Two users cannot silently overwrite; mutations authorized + attributed | stale-version save → 409; `last_updated_by/at` | | |
| 6 | Match/archive/report, clarification, stats, PDFs, packets match legacy on golden fixture | side-by-side against `backups/$STAMP/artifacts` | | |
| 7 | SQLite backup/restore and rollback/reverse-migration rehearsed | `docs/deployment.md` §6/§7, §5 rollback drill above | | |
| 8 | API/UI documentation current | `docs/`, exported `openapi.json`, `frontend/README` | | |

## 8. Rollback

**Before new writes after the flip:** restore the previous deployment config
and the workbook copy from `backups/$STAMP`; restart api/worker. No data moved.
The legacy NiceGUI editor no longer exists, so any workbook-mode corrections
must go through a reviewed CLI/import path, not a local UI.

**After new writes:** restoring the workbook would silently discard DB writes.
Instead:

1. Stop api/worker (no new writes).
2. `ccat workbook-export --output /artifacts/rollback.xlsx --report ...` and
   validate the export.
3. Decide explicitly: either keep SQLite (fix forward) or reverse-migrate using
   the export plus a reviewed import path. The workbook is a snapshot, not
   the active truth.
4. Record what was written after the flip (job history, audit events) with the
   decision.

**Image-only rollback:** `CCAT_IMAGE_TAG=<previous> docker compose --profile prod up -d`
(see `docs/deployment.md` §7).
