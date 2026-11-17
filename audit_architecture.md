# CCAT Audit Architecture

Authoritative guide to the NiceGUI human audit tool and its persistent-final
data layer. This document reflects implementation; superseded dense-adj and
projection descriptions are removed.

## 1. Runtime and Commands

- CLI: `ccat audit [--config PATH] [--test] [--restart]`.
- `server.run` optionally discovers/stops stale audit processes (`--restart`),
  calls `store.ensure_final_sheets`, installs global `AuditState`, and starts
  NiceGUI at `audit.host:audit.port`.
- `--test` limits displayed lists to the first stable family/sponsor and its
  children after normal startup reconciliation. It does not reduce writes.
- `scripts/audit_ui_smoke.py` boots a throwaway redirected workbook and
  requests every page.
- One controlling process owns workbook writes. Close Excel/LibreOffice and
  other CLI processes before audit runs.

## 2. Persistent Store

`audit/store.py` is a pure polars/pydantic data layer. It defines the full
schemas for `final_sponsor`, `final_family`, and `final_child`; no active
`adj_*` or `*_submissions` sheet exists.

| Entity | Key | One row per |
|---|---|---|
| sponsor | `sponsor_id` | sponsor submission |
| family | `family_id` | stable family identity; duplicate submissions share it |
| child | `zite_record_id` | child submission, including duplicates and letterless extras |

All former dense columns and controls remain:

- Child: source/linkage (`zite_record_id`, `zite_created_at`, `created`,
  `family_id`, `angel_id`, `source`), editable data, `age_at_xmas`,
  `generated_info_sheet`, `screened`, `potential_duplicate`, and controls.
- Family: source/linkage, editable contact/address data, match assignment
  fields, `potential_duplicate`, and controls.
- Sponsor: source/linkage, editable data, computed age bounds, match
  assignment fields, `potential_duplicate`, and controls.
- Common controls: `status`, `ignore`,
  `clarification_notes`, `audited_at`. There is no `audited` Boolean;
  `status == "audited"` is authoritative.

A null data cell means absent; it never means "see raw". Ignored, orphaned,
duplicate, failed-AI, and raw-missing entities stay in their store. Output
eligibility is an explicit reader filter, not storage deletion.

### 2.1 Schema and read guards

- `validate_final_frame` requires the exact full column set and valid
  non-null, unique entity keys.
- `read_final` normalizes booleans/ints/text and fails a projected legacy
  final, an `adj_*` sheet without its final counterpart, a null/duplicate
  key, or a malformed store.
- `build_final_frame` validates before returning the complete typed frame.
- `read_eligible_final` applies the retired projection population:
  non-ignored families/sponsors; non-ignored children with non-null
  `family_id`. Audit UI intentionally reads the unfiltered full stores.
- Writes use atomic workbook replacement. Child writes always receive
  `xmas`; writes without `final_child` restore its formula caches.

## 3. Status Machine

`status_of` compares editable fields with the current baseline. Child
`special_needs` is a control and never counts as an edit. Linkage,
computed fields, assignments, generation markers, and process flags also do
not create an edit.

Precedence:

1. `ignored`
2. `audited`
3. `needs_clarification` when `clarification_notes` is nonblank
4. `in_progress` when an editable field differs
5. `new`

The stored status is persisted on every audit save. Machine diagnostics can
append notes without resetting stored status; the UI computes live detail
state from the baseline.

## 4. Baselines and Groups

Every screen run prepares all raw rows in memory through `ccat.preparation`
and supplies current finals for stable IDs. Audit loads those prepared rows
for comparisons but never recreates a submissions layer.

- Sponsor baseline: current prepared row restricted to linkage/editable
  columns.
- Family baseline: stable family group's current prepared survivor/first
  row; all duplicate submissions are available in `FamilyEntry.submissions`.
- Child baseline: latest-non-null combination of the current duplicate group
  (`created`, then `zite_record_id`), falling back to the prepared row. Each
  submission remains its own final row.
- A final row absent from current raw uses its stored effective values as
  baseline. It remains listed and editable rather than disappearing or being
  reconstructed from another source; it is never merged with a current
  duplicate that merely shares email/name.

Sibling DOB notes derive from the current `dob_conflict_log` / prepared DOB
diagnostics. `merge_anomoly` appends idempotently and never removes existing
text.

## 5. Startup Reconciliation

`ensure_final_sheets` is an append-and-backfill operation under the store
write lock:

1. Read and validate the three full stores.
2. Prepare all current raw rows against those stable identities.
3. Fail only when both prepared raw data and persistent stores are empty;
   schema-valid empty raw sheets are valid with populated finals.
4. Seed only genuinely missing entities using current group baselines,
   default controls, computed fields, and child `screened=False`.
5. New orphan rows get the orphan note and `needs_clarification`.
6. Never refresh existing rows, rewrite assignments, or project finals.
7. Atomically write only stores that actually changed.

A workbook with projected finals, sparse legacy schemas, or malformed keys
fails closed and directs the user to `ccat consolidate-migrate` or
`ccat audit-migrate`, as appropriate.

## 6. Saves and Ownership

`save_child`, `save_family`, and `save_sponsor` share `_save_row`:

1. Reject unknown payload fields.
2. Find the current prepared row by stable key. A raw-missing stored row is
   its own fallback baseline; no unknown-ID failure occurs solely because
   Zite no longer contains it.
3. Acquire `_WRITE_LOCK`; read the complete store; locate the stored row.
4. Start from the stored row, or seed from baseline when genuinely new or
   during ignore/unignore reset.
5. Apply only supplied editable fields; omitted editable fields persist.
6. Follow current prepared linkage and stable child family/angel values.
7. Set supplied controls, stamp `audited_at`, recompute child age or
   sponsor bounds.
8. Preserve module-owned fields from the stored row: child
   `generated_info_sheet`, `screened`, and `potential_duplicate`; family/
   sponsor assignment fields and `potential_duplicate`.
9. For non-ignored children, merge current sibling-DOB notes.
10. Recompute status against the baseline, upsert by key, validate the full
    store, and atomically write it.

Ignore semantics:

- Ignoring resets ordinary editable values to the baseline and then reduces
  the row to identity, stable family/angel linkage, controls, and module-owned
  values.
- Ignored child `family_id` and `angel_id` deliberately remain stored. They
  reserve generated/assigned angel identity and allow raw-missing unignore;
  output filters continue to exclude the ignored row.
- Module-owned values are deliberately preserved across ignore:
  generation marker, screening marker, duplicate flag, and assignments.
- Unignore reseeds ordinary editable values from the current baseline; prior
  edits are lost only in this explicitly confirmed operation. Stable linkage,
  module-owned fields, and raw-missing baseline isolation survive.
- The row always remains in its final store. Output readers continue to
  exclude it while ignored.

A child with null `family_id` cannot be marked audited. A letterless
duplicate child with a family is not treated as an orphan.

## 7. UI

`pages.py` implements:

- Dashboard with sponsor/family/child status counts.
- Sponsor and family lists with text/status filters.
- Family detail pages with every duplicate submission.
- Sponsor/family/child detail pages with editable fields, diff highlighting,
  status authority (`status == "audited"`), ignore confirmation/unignore
  confirmation, and
  clarification note editing.

The full stores intentionally include ignored and orphan rows so correction
workflows work. Listing/dashboard-level test scoping occurs in memory after
loading.

## 8. Migration

### 8.1 Old v1 bootstrap

`ccat audit-migrate --from OLD [--force]` reads the old workbook without
writing it and emits full persistent finals directly in the configured
target. It preserves documented old adjudication/final/control/assignment
behavior and clarification cutover statuses. It does not emit submissions or
adj sheets and refuses active final target content without force.

### 8.2 Full v2 consolidation

`ccat consolidate-migrate --from OLD --output NEW [--force --backup BACKUP]
[--report REPORT]` is the path for workbooks with complete `adj_*` stores.
It copies every field/control/ID as values, retains raw/log/report sheets,
creates a separate output, validates everything, and writes a JSON report.
Input remains hash-verified immutable; force requires a byte-identical
readable workbook backup of the output being replaced; output/report commit
restores the prior output on late failure.

### 8.3 Rollback

Restore the saved old config and workbook. Do not open a persistent-final
workbook with old projection-based code as though its full stores were
projections.

## 9. Tests

Relevant suites include:

- `test_audit_store.py`: schemas/keys, status, groups/baselines, startup
  append behavior, raw-missing retention, saves, ignore/unignore, module
  ownership, output filters, formulas.
- `test_audit_server.py`: restart discovery, startup wiring, unavailable raw
  guidance, no projection behavior.
- `test_audit_migrate.py`: direct persistent bootstrap and immutability.
- `test_persistent_integration.py`: end-to-end append/merge, resume,
  interruption, human/module state, and golden populations.
- `scripts/audit_ui_smoke.py`: every NiceGUI page renders against a
  disposable workbook.
