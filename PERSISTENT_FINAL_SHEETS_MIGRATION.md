# Migration Plan: Persistent `final_*` Entity Sheets

> **Superseded.** The live store is SQLite; the workbook survives only as the
> import/export format (`ccat db-import` reads it, `ccat workbook-export`
> writes it). See `pipeline_architecture.md` Appendix A for the current
> architecture. This document is the historical design record of the
> workbook-era migration and is kept for provenance.

**Status:** Implemented in the workbook era; superseded by the SQLite store.

**Decision:** Implement **Option 1**: retain replaceable `raw_*` sheets, make
the `final_*` sheets the single editable entity store, stop persisting
`*_submissions`, and retain the operational log sheets.

This is a **storage and writer-ownership migration**, not a redesign of Zite
acquisition, local screening, AI adjudication, human audit semantics, matching
logic, or output generation.

## 1. Purpose

Replace the current workbook chain

```text
Zite -> raw_* -> *_submissions -> adj_* -> final_*
```

with

```text
Zite -> raw_* --(in-memory preparation)--> persistent final_*
                                             |
                                             +-> match, generate, stats, packets, clarify
```

The operational logs remain separate:

```text
duplicate_log
dob_conflict_log
email_validation_log
address_validation_log
```

The central rule is:

> A final sheet row is never reconstructed or discarded merely because a
> pipeline stage has rerun. New source records append rows; existing rows
> receive deliberate, field-scoped updates.

## 2. Required Outcomes

1. Exactly one editable representation of each entity in the workbook.
2. No `adj_*` or `*_submissions` sheets are written after migration.
3. Human edits, audit controls, and module-owned values survive acquire,
   screen, audit, match, and generate reruns.
4. Normal screening performs only uncompleted AI work, except for cheap global
   checks and deliberate field merges.
5. Downstream consumers receive the same logical entity populations and data
   they received from the old projected final sheets.
6. Every failure leaves completed work persistent and recoverable.
7. The original migrated workbook can be restored without a reverse migration.

## 3. Non-Goals

The migration will **not**:

- change how `ccat.acquire.run` fetches or imports Zite tables;
- convert the final sheets into an append-only event history;
- delete or merge duplicate child submissions automatically;
- assign a new duplicate-resolution policy;
- change duplicate identities (`primary_email` for families/sponsors and
  `primary_email + first_name` for children);
- change sibling DOB-conflict rules or AI screening content;
- redo every historical AI/address/email check during conversion;
- automatically match or regenerate artifacts during migration;
- persist raw `updatedAt`, add per-cell version history, or introduce a
  separate audit/event-sourcing system;
- replace the four operational logs or their existing response payloads;
- change concurrent CLI usage into a supported mode. One controlling process
  must still own workbook writes; edits from other spreadsheet applications
  must be closed before a command runs.

Incorrect matching/generation rules or stale AI work are addressed by their
existing explicit controls (`--redo`, `--overwrite`, and existing test-mode
flags), not by destructive automatic recalculation.

## 4. Existing Architecture That Must Be Preserved

The implementation plan refers to these existing mechanisms rather than
creating parallel behavior:

- **Acquisition:** `src/ccat/acquire.py` writes only `raw_*` through
  `io.write_sheets`; it verifies final sheets were not corrupted.
- **Preparation:** `src/ccat/screening.py` calls `import_table`,
  `clean_rows`, duplicate detection, ID assignment, and DOB checking.
  Current results are persisted as `*_submissions` and seeded into `adj_*`.
- **AI control and protection:** `screened` already gates ordinary AI work;
  `dense_adj_row` preserves existing human-edited fields, controls, and
  module-owned data. Completed AI rows are saved individually.
- **Entity schemas:** `src/ccat/audit/store.py` defines `ADJ_CHILD`,
  `ADJ_FAMILY`, and `ADJ_SPONSOR` columns, dtypes, keys, editable fields,
  baselines, control fields, and saves through `ENTITY_SPECS`.
- **Human audit:** saves are read/modify/write cycles protected by the store
  write lock and full-row field ownership.
- **Final projection:** `project_final` ignores rows, and
  `project_final_child` additionally excludes orphaned children and drops
  some control columns. `final_child` has live `age_at_xmas` formulas.
- **Match:** uses non-ignored entities and participation filters; its write
  path updates assignments and rewrites the current projected finals.
- **Generate:** writes `generated_info_sheet` in `adj_child` and refreshes
  the child projection.
- **Workbook I/O:** `src/ccat/io.py` enforces sheet ownership, performs
  atomic temp-file replacement, and preserves final-child age formulas.
- **External checks:** replay avoidance already comes from the email and
  address validation logs, not from auditable entity fields.
- **Clarification:** draft/contact state is in external CSV/JSON artifacts;
  this state must remain independent of the workbook consolidation.

## 5. Target Workbook

### 5.1 Required sheets

| Sheet | Role |
| --- | --- |
| `raw_sponsor` | Replaceable verbatim Zite fetch |
| `raw_family` | Replaceable verbatim Zite fetch |
| `raw_child` | Replaceable verbatim Zite fetch |
| `final_sponsor` | Persistent sponsor entity store and human-edit target |
| `final_family` | Persistent family entity store and human-edit target |
| `final_child` | Persistent child-submission entity store and human-edit target |
| `duplicate_log` | Current duplicate diagnostics |
| `dob_conflict_log` | Current sibling-DOB diagnostics |
| `email_validation_log` | Accumulating full ZeroBounce responses |
| `address_validation_log` | Accumulating full Nominatim responses |
| `match_report` | Written only by match runs that produce one |

Timestamped match archives may still be created as historical backups. The
legacy `dedup_log` is held only in the original backup workbook; the migrated
workbook need not recreate that unwritten legacy sheet.

### 5.2 Removed persistence layers

- `sponsor_submissions`, `family_submissions`, `child_submissions`
- `adj_sponsor`, `adj_family`, `adj_child`

These remain in the input workbook for auditing and rollback, but no migrated
workbook writer should read them as active stores.

### 5.3 Final sheet schemas

Use the **full current `ADJ_*` columns and dtypes as the starting schema**.
The old projected final schemas are insufficient: they omit controls such as
`audited` and, for children, `ignore`/`screened`.

- `final_child`: current `ADJ_CHILD_COLUMNS`, `ADJ_CHILD_SCHEMA`, key
  `zite_record_id`. One row per child submission, including duplicate,
  ignored, and orphaned records that still hold relevant work/state.
- `final_family`: current `ADJ_FAMILY_COLUMNS`, `ADJ_FAMILY_SCHEMA`, key
  `family_id`. One row per assigned family entity.
- `final_sponsor`: current `ADJ_SPONSOR_COLUMNS`, `ADJ_SPONSOR_SCHEMA`, key
  `sponsor_id`. One row per assigned sponsor entity.

The full store must retain current orphan/ignored rows. Excluding those rows
from **outputs** is a reader concern described in §10; it must not be
performed by deleting entity rows from the final sheets.

Suggested column ordering is current adj order, preserving all columns. Later
presentation changes are allowed only after the storage semantics are secure.

Optional future markers (`screened_at`, `screening_version`, input hashes,
migrated-at metadata) are **not prerequisites**. Avoid adding broad redundant
flags such as `cleaned`, `deduplicated`, `email_checked`, or `matched` unless
a distinct durable workflow requires them.

## 6. State Ownership Contract

### 6.1 Sheet writer matrix

The new `io.SHEETS`, `READABLE_SHEETS`, and `WRITABLE_SHEETS` contract must
implement this matrix:

| Module | May write |
| --- | --- |
| `acquire` | `raw_*` only |
| `screen` | Three `final_*`, current duplicate/DOB/validation logs |
| `audit` | Three `final_*` through audit save helpers |
| `match` | `final_family`, `final_sponsor`, `match_report`, timestamped archives |
| `generate` | `final_child` only, and only its generation-owned marker updates |
| `stats`, `clarify`, `sponsor packets`, read modules | No persistent entity/log writes |
| Consolidation migration | Target-sheet storage conversion only |
| Legacy `audit-migrate` | Either emits the new format directly or is explicitly unavailable for persistent-final workbooks |

Sheet-level ownership is necessary, not sufficient. Every writer must also
honor the column/lifecycle rules below.

### 6.2 Lifecycle ownership

- **Raw acquisition:** source-record accuracy, system linkage, source text.
- **Local preparation:** raw-parsed values and pure derived values for new
  or incomplete rows; global diagnostic flags.
- **AI screening:** AI corrections to eligible fields and `screened` after a
  successful response, never blanket data replacement on human-edited fields.
- **Human audit:** editable entity fields, `audited`, `ignore`,
  `require_clarification`, `audited_at`, and resulting human `status`.
- **Match:** `assigned_sponsor_id`, `assigned_angel_ids`, and matching-derived
  assignment counts. Match cannot rewrite audit controls or AI fields.
- **Generate:** `generated_info_sheet` and generation-specific output state.
  No other child or source field is modified to create an info sheet/PDF.
- **Local computed fields:** `age_at_xmas` and sponsor age bounds are recomputed
  from the current effective entity data. Preserve existing `ccat.io` formula
  behavior for `final_child`.

No writer should set `screened`, `audited`, `audited_at`, `ignore`, or
`generated_info_sheet` incidentally as a side effect of unrelated changes.

### 6.3 Process completion semantics

| Existing state | Meaning and guard |
| --- | --- |
| `screened` | Successful AI screening completed for this child. Ordinary screening skips the row; `--redo` and `--overwrite` remain deliberate exceptions. Local cleaning and global checks still run. |
| `audited`, `status`, `audited_at` | Human adjudication state. No generic rerun resets it. |
| `ignore` | Excludes an entity from outputs/participation; restores must retain recorded state rather than reconstructing from raw incorrectly. |
| `assigned_sponsor_id`, `assigned_angel_ids` | Owned only by the match/reconciliation policies. Raw fetch or screening reruns never assign or clear them. |
| `generated_info_sheet` | Set only when the corresponding artifact has really completed. Should not prevent an explicit overwrite run. |
| Email/address logs | Normal per-contact skip behavior remains exactly as the log-based check functions define. |
| `duplicate_log`, `dob_conflict_log` | Refreshed diagnostically; they do not permanently block the processing of existing rows. |

A successful AI result must persist `screened=True` atomically with the row;
a failed/interrupted one must remain eligible the next run. Continue writing
completed AI rows on the main thread rather than concurrent workbook writes.

Later automatic invalidation (e.g. an input hash/prompt version replacing a
manual `--overwrite`) should be a separate enhancement with its own migration.
Do not add it merely to justify the persistent store.

## 7. Append-and-Merge Algorithm

Create one tested storage-step contract, implemented around the existing
screening/store helpers. Avoid independently reproducing merge rules in each
module.

### 7.1 Normal preparation

For every run that currently prepares the submissions layer:

1. Read the `raw_*` sheets.
2. Execute the existing config import, cleaning, duplicate detection,
   validation-as-flags, and DOB logic in memory for all three tables.
3. Retain the prepared result for this run only; do not write submissions
   sheets.
4. Load the three current final stores with normalized full adj-compatible
   schemas.

Cheap global processing continues across raw records because a new row can
change duplicate or sibling conflict flags on existing entities.

### 7.2 Identity reconciliation

Before appending, validate final row keys and absence from current source.

- A missing final key or duplicate final entity key is a structural failure;
  stop before writing.
- For new records, reuse current grouping/linkage behavior. Children use
  `zite_record_id`; family/sponsor references follow their assigned entity
  IDs.
- Preserve final rows when there is no current raw match. Never delete them
  solely because Zite changed.
- Whether `zite_record_id` maps to one entity or a duplicate group must
  remain identical to today's prepared/adj mapping. This is especially
  important for families: multiple submissions can map to one `family_id`.

Do not use only workbook row order as an identifier.

### 7.3 New rows

Append records absent from the final store:

- initialize cleaned raw-backed fields and computed values;
- assign or inherit `sponsor_id`, `family_id`, and child linkage per existing
  grouping rules;
- default controls consistently with current seed helpers;
- set `screened=False` for a new child until a successful AI result exists;
- seed orphan clarification state exactly as current audit/screening docs do.

No existing row may be re-seeded implicitly simply because this creates a
batch of new rows.

### 7.4 Existing rows

For each existing final row:

- preserve all stable IDs, audit controls, timestamps, generated/matched
  values, and manually changed data;
- reapply safe global flags/notes using current append/merge semantics rather
  than wiping user-authored clarification text;
- do not replace its complete row from current cleaned submissions;
- preserve data changes previously approved by AI according to the same
  baseline comparisons used by `dense_adj_row`;
- maintain computed fields from the effective final row.

If a raw source changes data already corrected by a human, retain the human
value. If source-proof/staleness warnings are needed, defer those to the
optional provenance/version enhancement rather than guessing new fields.

### 7.5 Stable ID invariants

Once allocated and used in a final store:

- IDs never renumber due to sorting or timestamp changes;
- newly encountered record groups get unused numeric IDs above the current
  maximum, while still assigning later-created records in a deterministic
  order;
- `angel_id` values are not transferred to another submission by an ordinary
  rerun;
- an `angel_id` already used by an assignment or generated filename is
  especially explicit-change-only;
- sibling letter allocation is deterministic for new, unassigned members;
- a changed duplicate survivor after audit/matching/generation requires an
  explicit conflict-resolution action, not automatic reassignment;
- sort order after merges is presentation only.

The initial migration retains existing IDs, including historically allocated
duplicate/angel assignments. Do not recreate all IDs in creation order during
conversion.

### 7.6 Raw-missing records

Preserve a raw-missing final row and its edits. If a `source_present` or
similar marker is later required, add it by contract rather than dropping
rows. This change is unnecessary for Option 1's first implementation.

### 7.7 Persistence guards

Every full-sheet write goes through the atomic `io.write_sheets` path, and
final-child writes continue to supply `xmas` so age formulas are embedded and
cached. Add guards that reject:

- a pre-consolidation/project-only final schema lacking required controls;
- duplicate entity keys or missing stable identities;
- writes that unexpectedly remove final rows;
- final child writes omitting its `xmas` formula context;
- any module writing sheets outside its new matrix.

Do not repair malformed state by silently discarding it.

## 8. One-Time Storage Conversion

### 8.1 Prepare for cutover

1. Stop audit server and every other process that may touch the workbook.
   Close Excel/LibreOffice editors.
2. Confirm the configured source workbook is readable and current.
3. Copy the workbook and its config to timestamped backups outside the live
   input path. Retain actual raw, logs, adj sheets, and generated artifacts.
4. Record baseline metrics for later acceptance:
   - counts/keys per entity and counts of ignored/orphan rows;
   - final/adj column names;
   - `screened`, `audited`, `status`, `ignore` distributions;
   - duplicate/family linkage and orphan-ID counts;
   - assigned sponsor/angel IDs and counts;
   - `generated_info_sheet` values and output file existence;
   - log schemas, row counts, and content hashes;
   - email/address/AI work pending count.
5. Ensure no old final remains the migration data source when a corresponding
   full `adj_*` exists.

### 8.2 Proposed conversion entrypoint

Add a separate one-shot command, e.g. `ccat consolidate-migrate`, accepting
explicit input/output paths. It must not be conflated with existing old-v1 to
v2 `ccat audit-migrate` bootstrap semantics.

The command reads the input **without writing it**, produces a separate
output workbook, and refuses to overwrite a non-empty output unless an
explicit force option implies verified access to a backup first.

Suggested workflow:

```text
ccat consolidate-migrate \
    --config config/ccat_2026_v2.json \
    --from output/ccat_2026_v2.xlsx \
    --output output/ccat_2026_v2_persistent.xlsx
```

Only switch `excel_file` in a validated config after conversion and dry-run
verification. `consolidate-migrate` is a planning name; the actual CLI should
keep its semantics if a different name is chosen.

### 8.3 Conversion algorithm

For each entity:

1. Load all `adj_*` rows using existing normalization and full schema.
2. Copy every field and control **as values**, without re-running matching,
   AI screening, generation, or default re-seeding.
3. Preserve existing cell meanings and row identities; do not use the old
   projected final as the primary data source.
4. Write these same rows under the appropriate `final_*` name in the new
   workbook.
5. If migration sees only a legacy preliminary workbook without dense adj
   sheets, require the established migration/bootstrap path or fail clearly;
   do not speculate how to merge sparse rows.

For the remaining sheets:

- copy `raw_*` without modification;
- retain the four operational logs unchanged, when present;
- retain a current `match_report` unchanged;
- do not create `adj_*` or `*_submissions` in the output;
- retain legacy/current archives only in the original backup unless
  explicitly required for history — they are not active data.

### 8.4 Structural migration validation

The command must block handoff if any of these fails:

- row and key sets in every new final equal the corresponding adj set;
- all migrated fields equal after normalization;
- the final schema includes all required control/screening fields;
- all current `raw_*` rows, raw columns, logs, and report values survived;
- assigned sponsor/angels are preserved exactly;
- `screened`, `audited`, `status`, `ignore`, and
  `generated_info_sheet` are preserved exactly;
- no final key is null/duplicated or has changed type;
- no rerun has already occurred during the conversion;
- `age_at_xmas` semantics and formulas/cached values survive opening and
  reading the output;
- the output workbook opens read-only without corruption.

The first post-migration screen should not automatically screen all children
or reset any audit controls. Save a JSON/text migration report listing the
counts above for the handoff record.

## 9. Instructions and Code Changes by Area

These are planned implementation changes, not changes made now.

### A. First: Extract in-memory preparation (small, test-protected step)

**File:** `src/ccat/screening.py`.

Move the current clean/validate/duplicate/ID/DOB sequence into a helper
returning prepared rows plus diagnostics without deciding which Excel sheet
stores them. At this stage temporarily write current sheets if needed to keep
the transition testable.

Update store consumers of persisted submissions (`_entity_rows` and
`_child_groups`) to obtain the same in-memory preparation from a shared,
nonrecursive gateway. Do not import from a circular screening/store cycle;
place common prepared-row logic at a lower-level module if dependency direction
requires it. Preserve legacy reading only until conversion completes.

### B. Turn `adj_*` storage helpers into the full final entity store

**File:** `src/ccat/audit/store.py`.

- Point `ENTITY_SPECS[*]["sheet"]` and readers to `final_{entity}`.
- Rename `read_adj` semantically (`read_final` or similar) and update all
  callers rather than leaving wrappers indefinitely.
- Retain full schemas, controls, keys, baselines, save status computation,
  writable-field rules, and `_WRITE_LOCK`.
- Use existing seed logic for genuinely added records only. Ensure audit
  bootstrap no longer removes/manipulates finals as projections.
- Make `ensure_adj_sheets` a reconciling backstop for missing final entities,
  not a full projection refresh.
- Eliminate active final projection calls from startup and save paths.

At this point old-project-only finals must fail with a clear migration
message; never silently expand/seed over them.

### C. Screening append/update behavior

**Files:** `src/ccat/screening.py`, `src/ccat/acquire.py` where preparation
helpers are shared.

- Route local prepared data and AI updates to final stores under §7.
- Replace whole adj-table seed writes with guarded append/patched-row writes.
- Keep the current AI candidate selection and atomic per-child persistence.
- Make ordinary candidate selection refer to `final_child`, with old adj
  compatibility removed from the output format.
- Add structural assertions before save.
- Update acquisition's change-check: acquire still protects final sheets and
  `match_report`, but no longer checks `adj_*` or submissions.

The sibling DOB and duplicate flags still refresh globally. Human notes and
edits follow existing merge protection; never use `{field}_screened` to
suppress global correction/flag checks.

### D. Audit UI and save functions

**Files (historical):** `src/ccat/audit/store.py`, relevant
`src/ccat/audit/pages.py` and `src/ccat/audit/server.py` callers. The pages
and server modules were later removed with the NiceGUI UI; `store.py`
survives.

- Update detail-page side-by-side raw comparisons to use in-memory prepared
  raw data rather than `*_submissions`.
- Save each entity to its `final_*` directly; module-owned state stays
  unchanged.
- Preserve ignored/orphan filtering in lists and statistics through explicit
  reader filters, not by removing final store rows.
- Preserve `screening` → `audit` unavailable-state error messaging: users
  should be told to acquire/screen when raw or required processed values are
  unavailable.

### E. Match

**File:** `src/ccat/match.py`.

- Rename adj inputs to final inputs.
- Use the same participation predicates (`ignore`, null family, angel linkage
  rules) as today.
- Update only assignment-owned fields in `final_family`/`final_sponsor`.
- Preserve all other fields and current archive/`match_report` behavior.
- Preserve `--skip-match` reconciliation, but stop calling that reconciliation
  a raw/final projection operation.
- Do not needlessly write `final_child` during a match if the existing
  algorithm does not need to edit it.

### F. Generate and output consumers

**Files:** `src/ccat/child_info_generate.py`, `src/ccat/sponsor_packets.py`.

- Set `generated_info_sheet` directly on final child rows after actual
  generation; don't re-project after setting it.
- Readers that relied on the projected final to remove ignored/orphan rows
  must explicitly apply the same filter before treating any final row as an
  eligible output entity.
- Preserve family/sponsor join semantics and required-column errors.

### G. Stats, packets, clarify

**Files:** `src/ccat/stats.py`, `src/ccat/sponsor_packets.py` and affected
`src/ccat/clarify/*` readers.

- Final reads now return a full dense store. Apply in-memory output filters
  matching the old projection where required.
- For child outputs, the old projected population was non-ignored children
  with a non-null `family_id`. For families/sponsors it was non-ignored rows.
- Do not apply this filter to the human audit UI: it intentionally needs
  ignored and orphan correction workflows.
- Replace `stats --raw` dependencies on removed submissions with a clearly
  named preparation/raw strategy. Default stats keeps final reads and its
  existing eligible-output filters.

### H. Workbook schema and ownership

**File:** `src/ccat/io.py`.

- Remove active final projection/submissions/adj contracts from `SHEETS`,
  `WRITABLE_SHEETS`, `READABLE_SHEETS`, and docs.
- Grant each writer exactly the §6 sheet permissions.
- Preserve atomic write behavior and `final_child.age_at_xmas` handling.
- Make migration-only write permissions require an explicit migration module,
  not accidental permission for acquire/screen/match to erase raw/log data.

### I. CLI, docs, terminology

**Files:** `src/ccat/__init__.py`, module docstrings,
`pipeline_architecture.md`, `audit_architecture.md`, config documentation.

- Describe new final sheets as durable editable truth, never as projections.
- Explain screen resume flags and global diagnostic recomputation.
- Document the consolidation command, its report, and rollback.
- Update legacy `audit-migrate` and every example containing `adj_*`,
  `*_submissions`, or a final-is-generated claim.
- Avoid the unexplained word "raw" for `stats --raw` if its actual source
  becomes cleaned, ephemeral preparation output.

### J. Legacy `audit-migrate`

Choose and implement one explicit behavior at the matching implementation
stage:

1. **Preferred:** bootstrap from old v1 directly into full persistent finals,
   with no active submissions output.
2. **Compatibility:** continue producing the legacy storage target and require
   `consolidate-migrate` as a second explicit step.

It must never intermix new final semantics with old adj semantics in the same
write mode. Tests must accept only the documented behavior.

## 10. Migration Task Checklist

### Phase 0 — Contract and fixtures

- [ ] Freeze this plan and confirm final schema/key/global diagnostics rules.
- [ ] Capture source workbook backup and pre-migration acceptance metrics.
- [ ] Add fail-closing tests for final persistence invariants before removing
      old storage contracts.
- [ ] Confirm no future reader requires the exact projected-only final shape.

**Exit:** Full-current test baseline recorded; cutover fixture and expected
counts known.

### Phase 1 — Preparation extraction and schema-aware store

- [ ] Extract single in-memory preparation function.
- [ ] Add safe normalized full-final entity read/write guards.
- [ ] Add common legacy-projection-equivalent output filters for consumers.
- [ ] Add final-key and row-invariant tests.

**Exit:** No processing consequence changes; current tests pass, new store
contract passes isomorphic fixture tests.

### Phase 2 — Persistent writers

- [ ] Make audit startup/save/read functions use final stores directly.
- [ ] Refactor screening to append/merge existing finals and skip completed AI.
- [ ] Refactor match to patch assignment-owned final columns only.
- [ ] Refactor generate to update final child generation state only.
- [ ] Update all readers, acquisition protection, and the matrix.
- [ ] Drop all active `adj_*`/submissions write sites.

**Exit:** Full unit integration tests against a copied new-format workbook.

### Phase 3 — One-shot workbook conversion

- [ ] Build the separate `consolidate-migrate` command.
- [ ] Convert the current copied workbook from full `adj_*`.
- [ ] Run structural/data validation and save the report.
- [ ] Keep the original workbook/config immutable.

**Exit:** New workbook accepted at file-level review; historical fields all
untouched, all controls and external logs intact.

### Phase 4 — End-to-end rehearsal and cutover

- [ ] Validate ordinary screen resumes with zero duplicate AI work.
- [ ] Validate acquisition+screen appends new rows but changes no approved
      existing content except owned diagnostics/computed fields.
- [ ] Validate one ignored row, orphan corrected to a family, duplicate child
      needing resolution, existing assignment, and generated child survive.
- [ ] Validate redo/overwrite end-to-end with human protection.
- [ ] Test match, generate, stats, packet building, clarify extraction/export,
      and the audit UI against the converted copy.
- [ ] Rehearse interruption during AI and atomic file replacement.
- [ ] Switch config to the new workbook after sign-off.
- [ ] Keep the legacy backup read-only.

**Exit:** Production cutover.

### Phase 5 — Cleanup

- [ ] After the safe retention window, remove deprecated migration-only reader
      compatibility not exercised by the consolidation entrypoint.
- [ ] Remove projection helpers no longer used by output filtering or retain
      them only under truthful output-helper names.
- [ ] Ensure tests fail if submissions/adj resume normal use.
- [ ] Confirm docs/UI do not ask a user to edit raw, adj, or a projection.

## 11. Tests and Acceptance Matrix

The following must be explicitly tested with units and an integration fixture;
do not substitute only argument-count/schema assertions.

| Scenario | Expected result |
| --- | --- |
| Normal initial screening of a new child | Appends final row, AI result saved, `screened=True`, controls initialized |
| Immediate screen rerun | No AI call for the completed child; stable final row count/IDs; global diagnostics still correct |
| Child/family/sponsor added later | Only eligible new entities added; existing rows not re-seeded; no ID renumbering |
| New duplicate appears after initial screening | Both affected existing/new flags updated; no merge, automatic deletion, or assignment transfer |
| New child duplicate of an already assigned angel | Survives for clarification; old `angel_id` is not transferred |
| Same-family DOB changed by a new record | DOB log/flags updated independently of AI skip state |
| Missing raw record on next acquire | Final row and human/module edits remain |
| Acquired raw field differs from a human-corrected field | Corrected field is not silently reset |
| `--redo`/`--overwrite` | Targeted AI work runs, human-edited/controlled/module-owned values remain protected per current behavior |
| Interrupted AI work | Completed rows persist as completed; pending ones remain eligible |
| Failed AI result | No success marker; retry next run |
| Human audit save | Updates final entity controls/fields only; match/generation fields stay intact |
| Ignore/unignore | Status reduction/restoration works; final row remains stored; output population follows documented filters |
| Orphan child | Remains in `final_child`; audit can fix linkage; output modules exclude until eligible |
| Match rerun | Assignment fields reconciled only by match; no AI/audit/human data loss |
| Generate rerun | Only actual completion changes generation markers; children without eligible family/ignored remain excluded |
| Repeated email/address run | Existing log results do not trigger further validation calls; no new responsibility added to final flags |
| Repeated consolidation run | Output existing is refused without explicit force; no duplicate final rows |
| Final-child writes | Required `xmas` passed; live age formula and cached value still work |

Additional assertions for every ordinary no-change run:

```text
No existing final edits/controls/assignments/generation markers changed.
No human-entered cells were restored from raw values.
No eliminated source row was physically deleted from a final store.
No submission/adj sheet was recreated.
No external validation or AI request repeated solely because the command ran again.
```

Update existing test modules alongside their production readers/writers:
`test_acquire.py`, `test_screening.py`, `test_audit_store.py`,
`test_match.py`, `test_child_info_generate.py`, `test_stats.py`,
`test_sponsor_packets.py` (or corresponding existing modules), and new tests
for the consolidation writer and global append/merge behavior.

A golden fixture should include at least one multiple-submission family,
child duplicates, a DOB conflict, an orphan, an ignored child/family/sponsor,
an assigned sponsor with generated children, and a raw-missing final entity.
Dump selected pre/post fields for these individuals in code-ready fixtures.

## 12. Reporting, Backups, and Rollback

- The migration uses a different output filename and never mutates its input.
- Any production archive run preserves earlier assignments through existing
  match archival, not through rollback of the storage migration.
- Use backups recorded before any new-format screen/match run to avoid merging
  newly obtained data back into the old schema by guesswork.
- To roll back, restore the saved config and input workbook and rerun only
  original older commands. Never feed the new full finals to old code as if
  they were projections.
- Do not delete the old workbook, backups, or conversion report at cleanup
  time until current assignments and human work have been verified.

## 13. Operational Instructions After Cutover

1. `ccat acquire` replaces raw sheets only.
2. `ccat screen` appends/prepares new final entities, refreshes cheap global
   flags/log results, and AI-screens children that still need it.
3. `ccat serve` + the Vue SPA is the sole place for interactive human changes
   (the legacy `ccat audit` NiceGUI command was removed). Direct Excel
   editing remains possible technically, but programmatic runs assume no
   concurrent external edits and obey the same surviving-value contract.
4. `ccat match` owns matching fields; `ccat generate` owns generation fields.
5. Audit flags do **not** block duplicate/DOB/global diagnostics.
6. Completed screening means AI completed, not frozen in perpetuity. Use the
   explicit redo/overwrite controls whenever an intentional re-evaluation is
   required.
7. User guidance is simply: **raw is source; final is truth; logs are process
   history.**

## 14. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Existing projected final lacks `audited`/`ignore`/`screened` | Migrate from full adj sheets; enforce full-schema validation |
| Accidental two-way migration behavior overwrites rows | Separate conversion entrypoint; format guards; writer contract |
| Global flags become stale behind `screened=True` | Explicitly separate cheap global checks from AI skip state |
| New source event causes ID shifts | Never renumber once assigned; explicit duplicate-survivor conflict workflow |
| Orphans/ignored rows appear in output now included in final store | Apply old projection-equivalent filters at every output reader |
| Human clarification notes vanish when flags refresh | Merge machine-derived values, do not reset user-authored notes |
| A later algorithm change leaves old work stale | Existing explicit redo/overwrite remains; defer version/hash invalidation |
| Multiple tabs/processes race whole-row write | Keep `_WRITE_LOCK`, completed AI writes on main thread, single controller, close external editors |
| Spreadsheet age formulas become stale | Keep `xmas` parameter requirement and live formula/cache checks on final-child writes |
| Legacy bootstrap produces incompatible sheets | Update `audit-migrate` in the same transition or require an explicit two-step stored format migration |

## 15. Definition of Done

- Only raw, full persistent final, and designated log/report sheet categories
  remain active in the main workbook.
- There are no normal-code writes to `adj_*` or `*_submissions`.
- Manual edits from before migration are verified field-for-field.
- Repeated commands are state-preserving and perform only intended checks and
  new/incomplete work.
- Duplicate, DOB, orphan, ignore, match, generation, email/address behavior
  is consistent with pre-migration behavior, adjusted only by the changed
  storage shape.
- Final-child formulas survive every permitted writer.
- Conversion validation, new-format integration tests, UI tests, interruption
  tests, and rollback drill pass before production cutover.
