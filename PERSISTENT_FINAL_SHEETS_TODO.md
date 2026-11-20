# Persistent Final Sheets Migration — Implementation Checklist

> **Superseded.** The live store is SQLite; the workbook survives only as the
> import/export format. See `pipeline_architecture.md` Appendix A for the
> current architecture. This checklist is the historical record of the
> workbook-era migration.

Implementation tracker for `PERSISTENT_FINAL_SHEETS_MIGRATION.md`. Complete in
order; keep production workbooks and configs unchanged during development.

## 0. Architecture trace, safety, and baseline

- [ ] Trace configuration, workbook I/O, formula preservation, CLI dispatch,
  and current writer ownership.
- [ ] Trace acquisition, local preparation, duplicate/DOB diagnostics,
  email/address checks, AI candidate selection, retry behavior, and persistence.
- [ ] Trace audit schemas, seeding, status transitions, detail comparisons,
  saves, ignore/unignore, startup, migration, and projections.
- [ ] Trace match/pre-assignment reconciliation, generation, packets, stats,
  clarification, and every `adj_*`, `*_submissions`, and `final_*` reader/writer.
- [ ] Record pre-existing workspace changes and avoid overwriting user files.
- [ ] Run and record the complete pre-change test, lint, format, and type-check
  baseline; distinguish pre-existing failures from migration failures.
- [ ] Identify safe development fixtures; never mutate the configured production
  workbook or switch its config implicitly.

## 1. Contract tests and shared output semantics

- [ ] Add a comprehensive new-format golden fixture covering multi-submission
  families, child duplicates, DOB conflicts, orphan rows, ignored entities,
  assignments, generated children, and raw-missing final entities.
- [ ] Add fail-closing tests for full final schemas, required controls, stable
  non-null unique keys, unexpected row deletion, writer ownership, and mandatory
  `xmas` context for final-child writes.
- [ ] Add common reader filters exactly equivalent to legacy projections:
  non-ignored families/sponsors and non-ignored, non-orphan children.
- [ ] Verify audit readers intentionally retain ignored and orphan workflows.
- [ ] Verify no consumer depends on the old projected-only final schema.

## 2. Extract one in-memory preparation pipeline

- [ ] Move raw import, cleaning, validation-as-flags, duplicate detection,
  stable linkage/ID preparation, angel allocation, age derivation, and sibling
  DOB diagnostics into one side-effect-free shared preparation layer.
- [ ] Keep API calls, AI calls, workbook reads, and workbook writes outside that
  pure preparation step.
- [ ] Return prepared sponsor/family/child rows plus duplicate/DOB diagnostics.
- [ ] Route audit raw comparisons and child/family grouping through the shared
  preparation gateway without circular imports or recursive preparation.
- [ ] Preserve cleaning notes, duplicate survivor semantics, family grouping,
  child duplicate identity, orphan handling, sibling-conflict behavior, and
  existing response/log schemas.
- [ ] Characterize preparation with tests before changing persistence.

## 3. Persistent full-final store and append/merge reconciliation

- [ ] Point entity schemas/readers at `final_sponsor`, `final_family`, and
  `final_child` using the complete former `ADJ_*` columns, dtypes, and keys.
- [ ] Rename adj-specific store APIs and update every caller; remove temporary
  active-storage compatibility wrappers after migration support is isolated.
- [ ] Implement one guarded reconciliation contract shared by screening/audit
  backstop: validate stores, append genuinely new entities, patch only allowed
  machine-owned fields, and preserve all existing rows.
- [ ] Preserve raw-missing rows, ignored rows, orphan rows, human edits,
  clarification text, audit controls/timestamps/status, assignments, generation
  markers, screened state, and module-owned values.
- [ ] Preserve stable numeric entity IDs and angel IDs across sorting, source
  timestamp changes, duplicate arrival, and reruns; allocate only unused IDs in
  deterministic order.
- [ ] Keep family duplicate submissions mapped to one family entity and child
  submissions individually keyed by `zite_record_id`.
- [ ] Merge duplicate/DOB/global diagnostics without deleting human-authored
  notes or suppressing checks because AI screening completed.
- [ ] Recompute local derived fields from effective final values without
  overwriting protected human/source fields.
- [ ] Reject legacy projected-only finals clearly instead of expanding or
  overwriting them.

## 4. Workbook schema, ownership, and atomic write guards

- [ ] Replace `SHEETS`, `READABLE_SHEETS`, and `WRITABLE_SHEETS` with the raw /
  persistent-final / operational-log/report contract.
- [ ] Enforce the writer matrix for acquire, screen, audit, match, generate,
  consolidation migration, and explicitly supported legacy migration.
- [ ] Remove active submissions/adj read/write permissions and normal-code
  references; keep conversion-only legacy reads isolated.
- [ ] Keep atomic temp-file replacement, final-child formula/cache patching,
  formula restoration on other-sheet writes, and schema/dtype normalization.
- [ ] Add guards against null/duplicate keys, missing required controls,
  unexpected final-row removal, wrong writer modules, and final-child writes
  without `xmas`.
- [ ] Update acquisition protection to verify persistent finals and match report,
  never submissions/adj sheets.

## 5. Screening and external-check writers

- [ ] Run in-memory preparation on every screen, reconcile all three persistent
  final stores, and persist current duplicate/DOB logs without submissions or
  adj sheets.
- [ ] Initialize controls only for genuinely new rows; new children start
  unscreened and retain current orphan clarification seeding.
- [ ] Select ordinary AI candidates from unscreened final children while still
  running cheap global preparation/diagnostics for all raw rows.
- [ ] Preserve `--redo`, `--overwrite`, `--test`, human-edit baseline protection,
  exact response validation, prompt privacy, and per-child main-thread writes.
- [ ] Persist successful AI corrections and `screened=True` atomically per child;
  failed/interrupted work remains eligible and completed rows stay completed.
- [ ] Preserve log-driven ZeroBounce/Nominatim replay avoidance, request pacing,
  full response payloads, and nonfatal retry behavior.

## 6. Audit UI, save lifecycle, and legacy bootstrap

- [ ] Make audit startup/read/detail/save paths operate directly on full final
  stores with no final projection refresh.
- [ ] Convert `ensure_adj_sheets` into a final-store reconciliation backstop and
  rename it truthfully.
- [ ] Use shared in-memory prepared data for side-by-side raw comparisons and
  preserve unavailable-state guidance to acquire/screen.
- [ ] Preserve editable-field rules, write locking, status computation,
  audited/audited_at/ignore/clarification behavior, orphan correction, and
  module-owned assignment/screening/generation values on every save.
- [ ] Preserve ignored/orphan rows in storage and apply eligibility only in
  output readers.
- [ ] Keep ignore/unignore semantics explicit and tested, including documented
  destructive behavior where applicable.
- [x] Removed the NiceGUI pages/server and the audit smoke script; UI
  coverage now belongs to the Vue SPA.
- [ ] Make `audit-migrate` use one explicit storage mode: bootstrap directly to
  persistent finals or fail with instructions for the documented two-step path;
  never mix adj and persistent-final semantics.

## 7. Match and generation writers

- [ ] Read match pools from persistent finals with identical ignore, audit,
  family-linkage, angel-linkage, and participation predicates.
- [ ] Patch only match-owned columns in final families/sponsors; preserve every
  other field and never rewrite final children merely to match.
- [ ] Preserve sponsor phase ordering, exact-count/indivisible-family behavior,
  preferences, pre-assignment reconciliation, duplicate conflicts, reports,
  CSV export, timestamped archives, `--skip-match`, and `--overwrite`.
- [ ] Generate from explicitly eligible persistent-final children with identical
  family/sponsor joins, required-column failures, template enrichment, output
  paths, concurrency, cancellation, and overwrite/test/sponsor scoping.
- [ ] Update `generated_info_sheet` only after actual PDF success and write only
  that child-owned marker; never re-project finals.

## 8. Output and operational readers

- [ ] Update stats default/raw-preparation modes, sponsor packets, clarification
  extraction/export/status readers, and any scripts that consume active sheets.
- [ ] Apply legacy-projection-equivalent output filters centrally where required;
  do not filter audit correction workflows.
- [ ] Preserve joins, required-column errors, packet overwrite behavior,
  clarification CSV/JSON independence, and no persistent writes from read-only
  modules.
- [ ] Rename misleading raw-mode terminology in docs/CLI help while retaining
  command compatibility where required.

## 9. One-shot consolidation command and validation

- [ ] Add `ccat consolidate-migrate --config ... --from ... --output ...` with an
  explicit force policy and a migration-only writer identity.
- [ ] Read legacy input without mutation; require full adj sources or fail with
  bootstrap guidance; produce a separate persistent-final workbook.
- [ ] Copy all adj fields/controls/IDs as normalized values without rerunning AI,
  matching, generation, acquisition, or default seeding.
- [ ] Copy raw sheets, four operational logs, and current match report unchanged;
  emit no submissions/adj sheets and no unrequested archives.
- [ ] Refuse nonempty/existing output without explicit force and verified backup
  conditions; prevent input/output aliasing.
- [ ] Validate row/key sets, every migrated field, schemas/dtypes, controls,
  assignments, generation markers, raw/log/report survival, formulas/caches,
  and read-only workbook opening.
- [ ] Write a JSON migration report with entity/orphan/ignore/status/work-pending
  metrics, log counts/hashes, validation results, and output path.
- [ ] Add repeated-run/refusal, malformed-source, rollback-oriented immutability,
  and formula integration tests.

## 10. End-to-end tests and acceptance rehearsal

- [ ] Update all affected existing unit tests alongside production code.
- [ ] Test new initial screening, immediate rerun, later additions, stable IDs,
  late duplicates, assigned-angel duplicate survival, and global DOB refresh.
- [ ] Test raw-missing preservation, human-vs-raw conflict protection,
  redo/overwrite protection, failed/interrupted AI resume, and atomic writes.
- [ ] Test human saves, ignore/unignore, orphan correction, match reruns,
  generation reruns, and repeated email/address replay avoidance.
- [ ] Test stats, packets, clarify operations, audit UI, read-only commands, and
  all writer ownership restrictions against a copied new-format workbook.
- [ ] Test required `xmas` formulas and cached values after every permitted
  final-child write and after writes that omit final child.
- [ ] Assert every ordinary no-change run preserves edits, controls, assignments,
  generation markers, raw-missing rows, and external-call counts while creating
  no submissions/adj sheets.
- [ ] Run focused tests after each phase, then the complete pytest suite.
- [ ] Run `ruff format`, `ruff check`, and `ty check`; fix all introduced issues.

## 11. Documentation, terminology, and handoff

- [ ] Rewrite `pipeline_architecture.md` so its storage graph, schemas, module
  behavior, ownership matrix, code layout, commands, resume semantics, outputs,
  and examples exactly match the implemented system; remove obsolete claims.
- [ ] Update `audit_architecture.md`, README/config documentation, module
  docstrings, CLI help, and migration examples for persistent-final truth.
- [ ] Document raw-as-source/final-as-truth/logs-as-history, output filtering,
  global diagnostics versus AI resume, redo/overwrite, migration validation,
  report, immutable input backup, config cutover, and rollback.
- [ ] Remove normal-code references to `*_submissions`, `adj_*`, sparse adj seeding,
  and final-as-projection behavior; retain only clearly isolated conversion or
  historical documentation references.
- [ ] Update this checklist with completion evidence and any explicitly blocked
  operational cutover steps.
- [ ] Review the complete diff for accidental user-file changes and unintended
  production data mutations.
- [ ] Perform conversion rehearsal on a disposable copied fixture/workbook and
  report validation artifacts. Leave real config/workbook cutover untouched
  unless explicit source/output paths and sign-off are available.
