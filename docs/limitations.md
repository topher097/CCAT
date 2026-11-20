# Known limitations and follow-up triggers

Status of the 2026 refactor (`temp/web_server_refactor.md`) as implemented on
the `server` branch, plus the explicit follow-ups it left open. Everything
here is either a deliberate scope decision or a gate that requires a human
decision before public deployment.

## Deliberately out of scope

- No sponsor or family self-service accounts. All web accounts are staff
  accounts (decision D1). A sponsor portal needs row-level permissions and
  a different privacy design.
- No automatic email sending. Clarification drafts are generated and edited,
  never sent (the pre-existing behavior is preserved deliberately).
- No dual writing of Excel and SQLite in production. The workbook is an
  import/export format only.
- No PostgreSQL deployment. The schema and repositories are written to be
  portable, but only SQLite has been exercised.
- No automatic merging or deletion of duplicate, ignored, orphaned or
  raw-missing rows.

## Implemented but not yet exercised against production data

- The full Phase 5 rehearsal (import → acquire → screen → audit → match →
  generate → stats → clarify on a copy of the real workbook) has been
  scripted in `docs/cutover.md` but must be run by an operator against a
  copy of the production workbook before cutover. The test suite rehearses
  the same flow on synthetic golden fixtures.
- Packets, child sheets and statistics reports were exercised against
  fakes of the LibreOffice/py3o conversion seam. A run with real
  LibreOffice on the deployment image is part of the cutover checklist.
- Mail and address validation were exercised with faked provider engines.
  Real provider runs cost credits and are rate limited; they belong to the
  cutover rehearsal, not to CI.

## Open human decisions (gates before public deployment)

1. **Identity provider.** The `OidcAdapter`/`OtpAdapter` are interface
   stubs. `auth_mode: dev` (the passwordless bootstrap accounts and the
   no-login admin auto identity) is development-only. Production needs a
   reviewed OIDC provider or OTP/MFA for privileged roles, plus session
   lifetime and recovery decisions.
2. **Provider/data-processing approval** for clarification prompts, which
   deliberately contain family names, contacts, and child names, dates of
   birth, notes, and needs. Screening prompts exclude explicit name and
   contact columns, but free-text notes can still carry identifiers.
3. **Retention and safeguarding** for raw acquisition history, security
   activity diffs, full ZeroBounce/Nominatim payloads, match archives,
   generated packets and logs, plus backup hosting and encryption.
4. **Report timezone.** `report.timezone` defaults to `local` (the previous
   behavior). Production should pin it explicitly.
5. **Clarification contact timestamps.** Legacy naive-local timestamps were
   converted assuming `America/Detroit`; confirm against the source
   system's timezone.

## Technical limitations

- **SQLite single-writer model.** One API process and one worker per host.
  Writes are short, WAL is enabled, and a busy timeout surfaces
  `database is locked` rather than hiding lost updates. Multiple uvicorn
  workers are not supported.
- **PostgreSQL readiness is not proven.** No SQLite-specific SQL is used in
  shared repositories, but optimistic updates, uniqueness handling,
  transaction retry and migrations have only been exercised on SQLite. A
  separate dialect contract suite is required before claiming readiness.
- **Job queue is single-host.** The job table with leasing, heartbeats and
  recovery is not a distributed queue. A broker is the scaling path.
- **In-process login rate limiting.** The limiter is per process; a
  multi-instance deployment needs a shared limiter.
- **Reactivity is polling-based.** After-commit invalidation events are not
  implemented; the UI polls counts with an ETag-style fallback and pauses
  while the document is hidden.
- **Migration touches one source at a time.** `ccat db-import` refuses
  ambiguous or legacy sources and requires an explicit conversion step
  first for v1/v2 legacy workbooks.
- **Legacy NiceGUI apps removed.** The NiceGUI audit and clarify
  applications were deleted; interactive audit and clarification editing now
  live in the Vue SPA served by `ccat serve`, backed by the FastAPI
  resources. The workbook-mode `ccat audit` and `ccat clarify` commands no
  longer exist; the store/data logic in `src/ccat/audit/store.py` is
  unchanged.
- **Generated artifacts are local files.** Artifacts live under
  `storage.artifact_root` on local disk; there is no object store.
- **The admin database console bypasses the domain rules.** A statement it
  runs does not update `row_version`, does not pass repository validation,
  and can write schema changes that belong in an Alembic revision. It is
  admin-only, one statement per request, and audited, but it is a
  deliberate escape hatch, not an editing path. SQLite has no statement
  timeout, so a pathological query holds the API worker until it finishes.

## PostgreSQL trigger criteria

Move to PostgreSQL when any of these hold:

- More than one API instance or worker needs to write concurrently.
- Write-lock contention (`database is locked` retries) becomes routine.
- A season's entity count grows enough that full-store commits (the
  complete `final_*` store is rewritten per commit) stop fitting in a
  comfortable transaction window.
- Availability targets require multiple hosts or faster failover than a
  single-host SQLite deployment can provide.
- Storage growth or backup windows exceed what a single local disk can
  serve.

Before that move: add a PostgreSQL dialect contract suite, rehearse
migrations on a representative dataset, verify sequence/identity behavior
and lock semantics, and re-run the concurrency and job-recovery tests
against the new engine.
