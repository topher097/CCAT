# CCAT Audit Architecture

> **Historical design record.** The NiceGUI audit application described below
> was removed. The interactive audit editor now lives in the Vue SPA served by
> `ccat serve` (FastAPI); the store/data rules in `src/ccat/audit/store.py`
> survive unchanged.

Historical description of the removed NiceGUI human audit tool and its
persistent-final data layer. This document reflects the implementation as of
its removal; superseded dense-adj and projection descriptions are removed.

## 1. Runtime and Commands (historical)

- CLI (removed): `ccat audit [--config PATH] [--test] [--restart]`.
- `server.run` (removed) optionally discovered/stopped stale audit processes
  (`--restart`), called `store.ensure_final_sheets`, installed global
  `AuditState`, and started NiceGUI at `audit.host:audit.port` (that config
  block is also removed).
- `--test` limited displayed lists to the first stable family/sponsor and its
  children after normal startup reconciliation. It did not reduce writes.
- `scripts/audit_ui_smoke.py` (removed) booted a throwaway redirected workbook
  and requested every page.
- One controlling process owned the workbook writes. Close Excel/LibreOffice
  and other CLI processes before audit runs.

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
- The store rules survive unchanged; the workbook writes behind them are gone.
  The database commits atomically instead, and the child writes that remain
  (import/export) still receive `xmas`.

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
- `test_audit_server.py` (removed with the server): restart discovery, startup
  wiring, unavailable raw guidance, no projection behavior.
- `test_audit_migrate.py`: direct persistent bootstrap and immutability.
- `test_persistent_integration.py`: end-to-end append/merge, resume,
  interruption, human/module state, and golden populations.
- `scripts/audit_ui_smoke.py` (removed): rendered every NiceGUI page against a
  disposable workbook.

## 10. Current implementation (2026-11 refactor)

The human audit surface is now the FastAPI + Vue application (`ccat serve`),
not the removed NiceGUI tool. The sections above still describe the
adjudication rules; this section describes how they are exposed.

### 10.1 Runtime

- `ccat serve` starts the FastAPI app under `/api/v1`; the Vue SPA is
  served from the same origin. `ccat worker` runs the durable job loop.
- The legacy `ccat audit` NiceGUI path was removed; the Vue SPA is the only
  editor and the API/worker are DB-only (one active store).
- Authentication is a server-side cookie session (hashed token, HttpOnly,
  SameSite=Strict, CSRF header on mutations). Roles: `read_only`,
  `auditor` (edit/audit), `coordinator` (pipeline), `admin` (users/config).
  The two bootstrap accounts (`cmendres`, `lmboswell`) are development-only
  passwordless identities; dev mode also auto-authenticates every request
  as `cmendres` (admin) so no login is needed locally.

### 10.2 Save semantics

- `PATCH /api/v1/seasons/{season}/{entity}/{key}` takes only the fields the
  user touched. Omitted means unchanged, explicit null clears, a value
  sets. The server derives the final status; a client cannot post an
  arbitrary status string.
- Every write carries `If-Match: <row_version>`. A stale version returns
  `412` with the current server row, its version, and the winner's
  `last_updated_by`/`last_updated_at` so the client can review or rebase.
  Nothing is overwritten silently.
- Ignore and unignore are separate, confirmed actions. Ignore keeps the
  identity skeleton and module-owned markers; unignore re-seeds the data
  columns from the raw baseline (existing edits to those rows are lost —
  the same destructive rule the removed NiceGUI tool applied).
- An audited row's values are final. A patch that changes any value on a
  row whose stored status is `audited` is refused with `409 row_audited`,
  and so is ignoring such a row. Undoing the audit (`status: "new"`) is
  the admin-only override (`403 admin_required`): an admin undoes the
  audit, changes the row, then marks it audited again. The rule lives in
  the audit route, not in the save service, so the duplication group's own
  audit still reconciles its member rows through
  `services.db_audit.save_entity_db`.
- "Mark audited" stays reachable whenever the operation is legitimate.
  When the row still carries unsaved edits the SPA asks first: save them
  and mark the row audited in one PATCH, or mark the stored values
  audited without them. The duplication group's "Mark audited" asks the
  same question before it saves the pending decisions.
- `audited_at` is stamped on every save, exactly as before. The human
  audit authority remains `status == "audited"`; there is no replacement
  Boolean and no reuse of the audit timestamp as a generic "last touched"
  marker. `last_updated_by`/`last_updated_at` are the attribution fields.
- Linkage, assignment, screening and generation columns are not writable
  through the audit API; the repository enforces per-module column
  ownership.

### 10.3 Baselines, status and lists

- Baselines are unchanged: family baseline is the survivor/first submission,
  child baseline is the latest-non-null duplicate-group combination with a
  prepared-row fallback, and a raw-missing row supplies its own stored
  values.
- Status precedence is `ignored`, then `audited`, then
  `needs_clarification` (nonblank note), then `in_progress` (editable
  baseline difference), then `new`. Linkage, computed, assignment and
  process fields never make a row "in progress"; child `special_needs` is a
  control, not an editable comparison field.
- Audit lists intentionally include ignored, orphaned and raw-missing rows.
  Counts, filters and the home overview are computed server-side.

### 10.4 Tests

`test/test_audit_store.py` still characterizes the store rules (now backed
by SQLite), `test/test_services.py` the tri-state save service, and
`test/test_api.py` the HTTP contract (role matrix, tri-state PATCH, the
A/B stale-version scenario, ignore/unignore, IDOR and error mapping).

### 10.5 The Duplications section

The Audit tab's **Duplications** section adjudicates the duplicate groups
the last screening run detected. It reads `duplicate_log` (one row per
group member) and shows each group twice over: the raw submission
(`duplicate_log.record`, plus the group's conflicting columns and values)
beside the screened result (the `final_*` row the member landed in).

`src/ccat/services/db_duplications.py` owns the section;
`src/ccat/api/routes/duplications.py` exposes it.

| Route | Purpose |
|---|---|
| `GET /seasons/{season}/duplications` | The group list (paginated, searchable, filterable by entity) |
| `GET /seasons/{season}/duplications/{id}` | One group's raw and screened members |
| `POST /seasons/{season}/duplications/{id}/members/{record_id}` | One confirmed decision |
| `POST /seasons/{season}/duplications/{id}/audit` | Mark the group audited |
| `POST /seasons/{season}/duplications/{id}/children` | A split family's child assignments |

A group id is the SHA-256 of `(sheet, group_key)` truncated to 16 hex
characters, so it survives a new screening run that redetects the group.

The detail page is the child audit page turned sideways: rows are fields,
columns are the raw submissions, and the last column is the group's
current screened row, editable. Every raw column carries the group's two
decisions in its header.

| Column | Contents |
|---|---|
| Field | The field's label and its conflict marker |
| Raw submission (one per member) | The member's verbatim submission value |
| Current (editable) | The surviving entry's screened value; editable, and carried by the group's audit request |

A field **conflicts** when the submissions disagree on it. Matching
values do not conflict, and neither does a field one submission leaves
blank while another fills in. Conflicting rows carry the `diff-changed`
highlight, exactly like the child audit page marks a changed field.

#### 10.5.1 The three states of a submission

Every submission in a group ends in exactly one of three states, and the
auditor's **decisions** decide which:

| state | how a submission gets there | what it means |
|---|---|---|
| **surviving entry** | the auditor leaves it undecided | this submission *is* the entity; the group is audited with it |
| **released** | the auditor marks it *not duplicate* | it is genuinely another entity: it leaves the group, gets its own id, and becomes an ordinary row in the audit with status `new` |
| **discarded** | the auditor marks it *ignore* | the submission is thrown away: its row is reduced to the identity skeleton |

The **surviving entry is the one submission the auditor left undecided**.
The duplicate log's `survivor` flag is the screening run's *suggestion*:
it picks the default `Current (editable)` column and it is badged
`suggested survivor` in the member table, but it is not the authority.
The auditor may discard the flagged survivor; another undecided
submission then survives.

Two decisions are available per member, and every member keeps both —
including the flagged survivor, which may be discarded or released like
any other submission:

- `not_duplicate` — the member is a unique entity. It receives a unique
  id of its own: a child gets the lowest free angel letter in its
  family, a family submission becomes its own family row with a new
  `family_id`, and a sponsor keeps the `sponsor_id` it already owns.
  `potential_duplicate` is cleared.
- `ignore` — the member is ignored, through the ordinary audit ignore
  action (`services.db_audit.ignore_entity_db`), so the destructive
  skeleton reset and the un-ignore reseed are unchanged. A member's
  decision only ever touches a row that member owns **exclusively**: the
  group's shared family row belongs to the surviving entry, so discarding
  one registration records the decision and leaves the family row alone
  (a family is destroyed from the family's own row page, not from here).

Every decision is appended to the `duplication_audit` table (season,
sheet, group key, member, decision, assigned id, actor, timestamp). The
trail is append-only: a re-decision adds a row. The current decision of
a member is its newest trail row.

`final_child.angel_id` is not unique while a group is pending: every
member of a pending child group carries the group's single letter, so the
duplicate is one angel until the audit says otherwise. The family's angel
count and assigned angel lists therefore count distinct letters, and
info-sheet generation writes one sheet per angel. The screening run
assigns the shared letter after its AI commits (`sync_pending_angel_letters`),
because the AI commits carry the prepared (letterless) duplicate rows.

#### 10.5.2 Marking the group audited

A group is `pending` (the number of undecided submissions is not exactly
one), `resolved` (exactly one submission is undecided) or `audited` (the
auditor signed the group off). The first two are derived from the
decision trail; `audited` is recorded in the `duplication_state` table
(season, sheet, group key, status, audited at and by, reconciled at).

**Mark audited** (`POST /duplications/{id}/audit`) takes an optional body:

```json
{ "values": { "first_name": "Marcus" }, "expected_version": 7 }
```

`values` are the surviving entry's reviewed `Current (editable)` values
(canonical DTO names; the route maps them back to the legacy columns) and
`expected_version` is that row's `row_version`. Without `values` the
stored row is finalized as it stands. The audit then, in order:

1. refuses unless exactly one submission is undecided — zero leaves the
   group without a surviving entry ("this group has no surviving entry;
   …"), two or more leave it with more than one ("this group has more
   than one surviving entry; …");
2. closes out the other members — a released member's row status is
   overridden to `new` (it keeps its data and becomes editable again, so
   the auditor signs it off on its own page), a discarded member's own
   row stays `ignored` and untouched (a family group's submissions share
   one row, so a discarded registration has no row of its own);
3. saves the **surviving entry once**, with the carried values and the
   `audited` status in the same save (`services.db_audit.save_entity_db`),
   so the ownership matrix, the `audited_at` stamp and the status
   derivation are unchanged;
4. re-derives the angel columns of every affected family
   (`_refresh_angel_columns`), because the rollup counts only audited
   children and the audit save never touches assignment columns;
5. records the group's audited state — the flag that says the affected
   `final_*` tables were reconciled with the adjudication.

Each distinct row is saved at most once, so a family group whose
submissions share one family row audits cleanly.

**The group's adjudication outranks a row-level audit.** It sets every
member's status regardless of what that row's status was before: a
released member that was audited on its own goes back to `new`. It does
**not** get to rewrite the values of an already-audited surviving entry:
the audit re-affirms it without values (auditing the same group twice
stays `200`) and refuses a carried value change with `409 row_audited` —
the same code and meaning as the row pages, pointing at the admin
override. The row page's `duplicate_pending` guard is unchanged.

The group's audit is not one transaction: it is several commits, and the
group state row is written last. A crash halfway leaves a partially
overridden group, which the group's own `audited` flag does not record.

#### 10.5.3 A split family's children

A family submission says nothing about its children: the link is the
child's `primary_email`, resolved once and then frozen, and `family_id`
is not editable. Nothing in the data says which of two same-email
registrations a child belongs to — except the submission times.

The group page therefore attributes each child to the family entry that
was **in force when the child was submitted**: the latest group member
whose submission time is at or before the child's. A child that predates
every member belongs to the first one (the group's earliest
registration). The attribution is a hint that turns the split into a
decision rather than an accident of the email; the auditor still assigns
the children.

When a family group is split, the group page lists the group's children
(`GET /duplications/{id}` → `children`) and the auditor assigns each one
to the surviving family or to the released family
(`POST /duplications/{id}/children` with
`{"assignments": {"<child record id>": <family id>}}`). The server writes
the child's new `family_id`, re-issues its angel letter in that family
(the letter is prefixed with the family id, so a child moved to family 2
becomes `2A`), and re-derives **both** families' rollups. Children the
auditor leaves alone stay with the surviving family, and the page says
so. The duplication module is the only writer of a child's `family_id`
outside preparation, exactly as it already is of a child's `angel_id`.

#### 10.5.4 Navigation

The audit lists keep their filters in the query string, so a detail
link carries them and the back button restores the place in the table
the user came from. The detail endpoints take the list's own filter
parameters (`q`, `status` for the entity lists; `q`, `entity` for the
duplications) and return the neighbouring entries, computed from the
same ordering the list uses — so "next" and "previous" always mean the
adjacent row of the table the user is looking at.

### 10.6 Tests

`test/test_duplications.py` characterizes the service (list, detail, the
shared letter, the not-duplicate and ignore decisions, the audit trail,
marking a group audited) and the screening integration;
`test/test_api.py` adds the HTTP contract for the routes;
`frontend/test/duplications.test.ts` covers the list, the decision
table, the pending decisions and the ignore confirmation;
`frontend/test/audit-nav.test.ts` covers the back button and the
neighbour links.
