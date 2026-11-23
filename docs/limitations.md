# Known limitations and follow-up triggers

Status of the 2026 refactor (`temp/web_server_refactor.md`) as implemented on
the `server` branch, plus the explicit follow-ups it left open. Everything
here is either a deliberate scope decision or a gate that requires a human
decision before public deployment.

## Deliberately out of scope

- No sponsor or family self-service accounts. All web accounts are staff
  accounts (decision D1). A sponsor portal needs row-level permissions and
  a different privacy design.
- No clarification-email sending. Clarification drafts are generated and
  edited, never sent; that pre-existing decision is unchanged. Sponsor packet
  email is the deliberate exception: the dispatch page composes it and an
  admin sends it through Gmail.
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
   generated packets and logs, plus backup hosting and encryption. Dispatch
   adds stored draft bodies, coordinator notes and send-attempt snapshots;
   these personal-data copies need a defined retention period.
4. **Report timezone.** `report.timezone` defaults to `local` (the previous
   behavior). Production should pin it explicitly.
5. **Clarification contact timestamps.** Legacy naive-local timestamps were
   converted assuming `America/Detroit`; confirm against the source
   system's timezone.
6. **Gmail OAuth consent and app status.** An operator must authorize the
   sending account with the `gmail.send` scope. An OAuth app left in Google's
   "Testing" publishing status receives seven-day refresh tokens; production
   needs a published/verified app rather than repeated weekly consent.
7. **Sending identity.** `dispatch.sender_email` must be the authorized Gmail
   account or a verified send-as alias that the account may use. The display
   name and reply-to address also need owner approval before sending.

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
- **The legacy workbook CLI still exists.** `ccat acquire`, `ccat screen`,
  `ccat match`, `ccat generate`, `ccat stats`, `ccat clarify-export` and
  `ccat audit-migrate` read and write the deprecated `excel_file` workbook.
  They refuse to run unless that path is configured, and the configuration
  files no longer carry it, so the accidental-write path is closed. The
  database equivalents are the jobs behind `ccat serve` and `ccat worker`.
  `ccat db-import` and `ccat workbook-export` stay the supported way to move
  data between the workbook and the database.
- **Generated artifacts are local files.** Artifacts live under
  `storage.artifact_root` on local disk; there is no object store.
- **The admin database console bypasses the domain rules.** A statement it
  runs does not update `row_version`, does not pass repository validation,
  and can write schema changes that belong in an Alembic revision. It is
  admin-only, one statement per request, and audited, but it is a
  deliberate escape hatch, not an editing path. SQLite has no statement
  timeout, so a pathological query holds the API worker until it finishes.

## Manual matching (module 4b)

- **The child row is the assignment.** `final_child.assigned_sponsor_id` is
  the authoritative "this child is sponsored by S" link; the family and
  sponsor assignment columns are derived summaries of it. A family may
  therefore be split across two sponsors, and the workbook export carries
  the assignment on the **child sheet** — the child sheet is the
  authoritative assignment record in an exported workbook.
- **Child PII is readable in bulk.** The manual-matching pool returns each
  child's name, needs, wants, interests, notes and sizes. Any authenticated
  user may read it, the same as the audit child list, but it makes the whole
  season's child detail reachable in one request instead of many. The
  provider/data-processing approval gate above covers this data.
- **Stale artifacts after a placement.** A manual assignment does not
  rewrite the information sheets or the packets already on disk. The pool
  card warns when a child already has a generated sheet, and the manual
  page offers a "Regenerate this sponsor's sheets" action. Recovery is to
  re-run the `generate` job with `overwrite` enabled.
- **Each placement rewrites the whole child, family and sponsor store.**
  The ownership registry requires the complete row set per entity, so one
  assignment is one transaction over the season's entire final store, plus
  one write lock. That is the same cost the audit save already pays; it is
  the trigger to move to PostgreSQL when a season's entity count grows.
- **A coordinator can change the default run flags of the matching and
  generation commands.** The write surface is one command's run flags
  (`matchmaking.run`, `generate.run`); the weights, the Zite endpoints,
  the paths and the database URL stay admin-only. The change is recorded in
  the security audit log with the before and after values, and it persists
  in the configuration file, so it affects every later operator's run.
- **The configuration file race.** The admin's whole-tree
  `PUT /api/v1/config` and the coordinator's scoped
  `PATCH /runs/{command}/settings` both read-merge-write the file without a
  lock. Both writes are atomic and the whole tree is re-validated, but a
  concurrent admin edit and coordinator change to the same subtree can lose
  one of them.
- **Holds do not round-trip through the workbook export.** A family hold is
  matching state, not a final-entity attribute, so an exported workbook
  carries the assignments but not the holds. The export is rollback
  evidence, not the active store.
- **The legacy "Assigned Angel ID Code" field is hidden in the sponsor
  editor.** `prior_assigned_angel_ids` still works exactly as before — the
  automatic matcher still reconciles it and still treats it as a manual
  assignment — but the free-text field cannot express a partial placement,
  so the editor hides it and points at the manual matching page.

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
