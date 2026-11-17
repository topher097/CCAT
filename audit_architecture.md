# CCAT Audit Architecture v2 — Multi-Entity NiceGUI Audit Tool

Authoritative plan for remaking module 3 (human audit). This document
supersedes `pipeline_architecture.md` §7 for the audit tool; §4/§9 are
amended here and `pipeline_architecture.md` will be updated at
implementation time. No code has been written yet — this is the contract
the implementation must satisfy.

**v2 revision (this document):** the `adj_*` sheets are **dense** — they
carry the complete adjudicated record for every entity, with no
sparse-override sentinel nulls. The `final_*` sheets are **pure
projections** of the `adj_*` sheets and can be built from them alone,
without reference to the `raw_*` sheets. A new workbook
(`output/ccat_2026_v2.xlsx`, driven by `config/ccat_2026_v2.json`) hosts
the new system; the existing `output/ccat_2026.xlsx` is never modified —
completed audit data is migrated into the new workbook by a read-only
one-time bootstrap.

**Formula/parallelism update (this revision):** the live
`final_child.age_at_xmas` formulas and the parallel info-sheet
generation now on main are incorporated — every `final_child` writer
passes `xmas` (§7.2), adj sheets keep plain `age_at_xmas` values
(D13), and module 5's thread-pool machinery is preserved with its
flag writes retargeted to `adj_child` (§7.5).

## 1. Purpose and Scope

Replace the current stdlib `http.server` + static-HTML child-only audit UI
with a multi-page NiceGUI application (same pattern as
`src/ccat/clarify/`), and extend adjudication from children-only to all
three entities:

- **Sponsors** — adjudicated via a new `adj_sponsor` sheet, feeding
  `final_sponsor`.
- **Families** — adjudicated via a new `adj_family` sheet, feeding
  `final_family`; duplicate submissions (same primary email) shown
  side by side.
- **Children** — adjudicated via the existing `adj_child` sheet (now
  dense and human-editable), feeding `final_child`; duplicate
  submissions (same email + first name) shown side by side.

What stays the same:

- One Excel workbook as the inter-module exchange (§4), atomic sheet
  writes via `ccat.io.write_sheets` with the §9 ownership matrix
  (extended, see §4.4).
- Single human user, local server, `ccat audit` CLI entry.
- `zite_record_id` / `family_id` / `sponsor_id` join keys.
- The screening module's LLM pre-adjudication of `adj_child` (§6 of the
  pipeline doc) — retargeted to write dense rows (§7.3).
- The raw sheets' role as the read-only acquisition record and the
  audit UI's diff baseline.
- The live `final_child.age_at_xmas` formulas (pipeline doc §4):
  `write_sheets` stores the age as `=DATEDIF(<dob cell>, DATE(Y,M,D),
  "y")` with the age cached beside it, and restores the caches when a
  write re-saves the workbook without `final_child`. Extended to every
  new `final_child` writer (§7.2); adj sheets stay formula-free (D13).

What is removed:

- `src/ccat/audit/server.py` (http.server, JSON API, `AuditState`,
  queue/save API) and `src/ccat/audit/static/index.html`.
- The sparse-override adj model (null = "same as raw"). Nulls in adj
  sheets now mean only one thing: the value is genuinely absent.
- The `final_child.audited` Boolean as the readiness gate for matching
  and info-sheet generation — replaced by the saved `status` (§7.5).
- Frozen-row semantics: `audited` is a revocable checkbox; there is no
  409-on-resave. `--overwrite` and `--hide-completed` CLI flags are
  dropped (replaced by in-UI list filters, §10).

## 2. Current State (What Is Being Replaced)

- `ccat audit` serves one page: a flat queue of unaudited children with a
  two-column raw/adjudicated table, special-needs checkbox, clarification
  note, and Save/Mark-audited buttons. Only children are reviewed.
- `adj_child` is sparse and written solely by screening; the audit tool
  reads it and writes `final_child` directly.
- Sponsors and families are never audited by a human — `final_sponsor`
  and `final_family` are built by matchmaking from raw sheets verbatim.
- Acquire deduplicates families (coalesce, first non-null wins by
  `created` ascending) and children (latest submission wins) *before*
  writing `raw_family`/`raw_child`; the dropped submissions survive only
  as JSON blobs in `dedup_log`, invisible to the audit UI.
- All of this lives in `output/ccat_2026.xlsx` via
  `config/ccat_2026.json`. That workbook remains untouched; the new
  system runs against `output/ccat_2026_v2.xlsx` via
  `config/ccat_2026_v2.json` (§11).

## 3. Target Architecture Overview

```
Zite ──> (1) acquire ──> raw_sponsor / raw_family / raw_child  (VERBATIM —
                        every submission, duplicates included; no IDs)
              │
              v
        (2) screen ─────> sponsor/family/child_submissions  (cleaned + IDs)
                        duplicate_log / dob_conflict_log
                        email_validation_log / address_validation_log
                        adj_sponsor / adj_family / adj_child (dense rows,
                        potential_duplicate flags, notes; children get
                        LLM corrections — no personal data in the prompt)
              │
              v
        (3) audit ──────> adj_sponsor / adj_family / adj_child
                        (dense, complete, audit-controlled)
                        final_child  = projection of adj_child
              │
              v
        (4) match ──────> adj_family / adj_sponsor   (assignment columns)
                        final_family / final_sponsor = projections
                        match_report (incl. unresolved duplicates)
              │
              v
        (5) generate ───> adj_child.generated_info_sheet
                        (final_child projection refreshed)
```

Core invariants:

1. **adj is the single source of truth for adjudicated data.** Every
   `adj_*` row is dense: each data column holds the actual value that
   should flow downstream (initialized from raw, then edited by humans).
   A null adj cell means the value is genuinely absent — never "see
   raw".
2. **final_* sheets are projections.** `final_X` is a pure function of
   `adj_X`: drop the ignored rows, drop the pure control columns, keep
   everything else (§7.1). Nothing reads raw to build finals.
3. **Readiness is status-based.** A child participates in matching and
   info-sheet generation iff its saved `status` is `audited` (§5.3,
   §7.5).
4. **The old workbook is read-only.** The new workbook is bootstrapped
   by a one-time migration that copies raw data and completed audit
   state across (§11).

## 4. Workbook Contract Changes

### 4.1 New and changed sheets

| Sheet                 | Contents                                                        | Written by      |
|-----------------------|-----------------------------------------------------------------|-----------------|
| `raw_*`               | [CHANGED] Verbatim Zite records (module 1 only fetches; no cleaning, IDs, or dedup) | module 1 |
| `sponsor_submissions` | [NEW] Cleaned, ID-assigned sponsor rows, one per submission      | module 2        |
| `family_submissions`  | [CHANGED] Cleaned, ID-assigned family rows, duplicates included  | module 2        |
| `child_submissions`   | [CHANGED] Cleaned, ID-assigned child rows with `family_id`/`angel_id`/`age_at_xmas` | module 2 |
| `duplicate_log`       | [NEW] One row per flagged duplicate submission — nothing merged  | module 2        |
| `email_validation_log`| [NEW] One row per ZeroBounce email check (full verbatim response in `response_json`) | module 2 |
| `address_validation_log` | [NEW] One row per Nominatim address check (full verbatim response in `response_json`) | module 2 |
| `adj_sponsor`         | [NEW] Dense: complete adjudicated sponsor record + controls + `potential_duplicate` + assignment columns | module 2 (seed/flags), module 3 (data/controls), module 4 (assignments) |
| `adj_family`          | [NEW] Dense: complete adjudicated family record + controls + `potential_duplicate` + assignment columns | module 2 (seed/flags), module 3 (data/controls), module 4 (assignments) |
| `adj_child`           | [REMADE] Dense: complete adjudicated child record + controls + `potential_duplicate`; screening pre-fills | modules 2, 3, 5 |
| `final_child`         | Projection of `adj_child` (schema change, §7.1): **orphaned children (family_id null) excluded** | modules 3, 5    |
| `final_family`        | Projection of `adj_family` (schema change, §7.1)                | module 4        |
| `final_sponsor`       | Projection of `adj_sponsor` (schema change, §7.1)               | module 4        |

`dob_conflict_log` (now module 2) and `match_report` carry over.
`dedup_log` is gone: duplicates are flagged in `duplicate_log`, never
merged. `ccat.io.SHEETS` lists the full contract.

### 4.2 Submission sheets (module 2 addition)

Written by `ccat screen` from the verbatim `raw_*` rows: cleaned
locally (trivial capitalization/format fixes — never the AI), validated
with failures becoming `require_clarification` notes (rows are never
dropped), and ID-assigned:

- `sponsor_submissions`: sponsor config columns plus `sponsor_id`,
  `pref_age_min`, `pref_age_max`.
- `family_submissions`: family config columns plus `family_id`
  (duplicate submissions share the family's id).
- `child_submissions`: child config columns plus `family_id`,
  `angel_id`, `age_at_xmas` (duplicate submissions of the same child
  share the family id; only the group's survivor keeps an `angel_id`).

All three are sorted by (`created`, `zite_record_id`) ascending.
Rebuilt fully on every screen run.

Notes:

- Every submission is present, duplicates included — the audit UI's
  duplicate-group view reads these sheets.
- The verbatim `raw_*` sheets remain the untouched Zite record; the
  audit store prefers the submissions rows and falls back to raw rows
  for legacy workbooks (prepared rows missing from a partial
  submissions sheet are appended from raw).

### 4.3 Dense adj sheet schemas

Common control columns on all three adj sheets (audit-owned; other
modules never write them):

| Column                | Type    | Meaning                                        |
|-----------------------|---------|------------------------------------------------|
| `status`              | String  | Derived (§5.3); recomputed by audit on every save. Never edited directly. |
| `audited`             | Boolean | The "information is audited" checkbox.         |
| `ignore`              | Boolean | The "ignore" flag — row excluded from its final sheet; data columns nulled per §5.5. |
| `require_clarification` | String | Free-text clarification note. Null/empty = none. |
| `audited_at`          | String  | ISO timestamp stamped on every audit save of the row. |

`adj_child` additionally carries `screened` (Boolean) — set by module 2
when it writes/pre-fills the row (see §7.3). With dense rows the mere
presence of a row no longer proves screening happened (audit seeds rows
too), so the marker is explicit.

**`adj_child`** (key `zite_record_id`, String; one row per child in
`raw_child`, dense):

- acquire-owned linkage (read-only in the UI): `zite_record_id`,
  `zite_created_at`, `created`, `family_id` (Int64), `angel_id`,
  `source`.
- adjudicable data (audit-editable): `first_name`, `primary_email`,
  `dob`, `age_reported`, `gender`, `need_1`, `need_2`, `desire_1`,
  `desire_2`, `shoe_size`, `shoe_size_type`, `pant_waist_size`,
  `pant_inseam`, `pant_size_letter`, `pant_size_type`, `jean_size`,
  `shirt_size_letter`, `shirt_size_type`, `favorite_colors`,
  `interests`, `other_notes`, `potential_anomoly`, `special_needs`
  (String — the multi-select result as a semicolon-joined list, e.g.
  `"Autism; other: speech delay"`; empty = none).
- computed (read-only, recomputed at save when inputs change):
  `age_at_xmas` (Int64; from the adjudicated `dob` — since adj is the
  truth, the computed age follows adjudicated edits). Stored as a
  plain integer — the live `DATEDIF` formulas exist only in
  `final_child` (§7.1) — and doubles as the fallback value
  `write_sheets` keeps when a row's `dob` is missing or unparsable
  (D13).
- module-5-owned: `generated_info_sheet` (Boolean).
- controls: the five common columns + `screened`.

**`adj_family`** (key `family_id`, Int64; one row per family in
`raw_family`, dense):

- acquire-owned (read-only): `zite_record_id`, `zite_created_at`,
  `created`, `source`, `accepts_disclaimer`.
- adjudicable data: `primary_email`, `first_name`, `last_name`, `phone`,
  `street_address`, `apt_floor`, `city`, `zip_code`, `state`,
  `secondary_first_name`, `secondary_last_name`, `secondary_email`,
  `secondary_phone`, `gift_pickup_location`.
- module-4-owned assignment columns (read-only in the audit UI):
  `assigned_sponsor_id` (Int64), `assigned_angel_ids` (String),
  `num_angels_in_family` (Int64).
- controls: the five common columns.

**`adj_sponsor`** (key `sponsor_id`, Int64; one row per sponsor in
`raw_sponsor`, dense):

- acquire-owned (read-only): `zite_record_id`, `create` (this season's
  config key for the sponsor created timestamp), `source`,
  `accepts_disclaimer`.
- adjudicable data: `email`, `sponsor_type`, `organization_name`,
  `prior_assigned_angel_ids`, `first_name`, `last_name`, `phone`,
  `num_children`, `age_preference`, `gender_preference`,
  `dropoff_location`.
- computed (read-only, recomputed at save): `pref_age_min`,
  `pref_age_max` (from the adjudicated `age_preference`).
- module-4-owned assignment columns (read-only in the audit UI):
  `assigned_angel_ids` (String), `num_angels_assigned` (Int64).
- controls: the five common columns.

Every adj data column holds a real value wherever one exists (dense). A
null cell is a genuinely missing value (e.g. no `organization_name` for
an individual sponsor), not a "same as raw" sentinel.

### 4.4 Ownership matrix (§9 amendment)

| Sheet                 | acquire | screen | audit | match | generate | migrate |
|-----------------------|---------|--------|-------|-------|----------|---------|
| `raw_*`, submissions, `dedup_log`, `dob_conflict_log` | write | - | read | - | - | write (bootstrap copy only, §11) |
| `adj_child`           | -       | write (data cells + `screened`, §7.3) | write (data + controls) | read | write (`generated_info_sheet` only) | write (bootstrap, §11) |
| `adj_sponsor`         | -       | -      | write (data + controls) | write (assignment columns only) + read | read | write (bootstrap, §11) |
| `adj_family`          | -       | -      | write (data + controls) | write (assignment columns only) + read | read | write (bootstrap, §11) |
| `final_child`         | -       | -      | write (projection) | read | write (projection refresh) | write (bootstrap, §11) |
| `final_family`        | -       | -      | -     | write (projection) | read | write (bootstrap, §11) |
| `final_sponsor`       | -       | -      | -     | write (projection) | read | write (bootstrap, §11) |
| `match_report`        | -       | -      | -     | write | -         | - |

`WRITABLE_SHEETS` in `ccat.io` is updated accordingly. The `migrate`
writer exists only for the one-time bootstrap of a fresh workbook
(§11); it never writes a workbook that already contains adj/final
sheets, and it never writes the `--from` (old) workbook.

Key consequences versus v1:

- Match no longer reads or writes `raw_*` at all — the sponsor/family
  pools come straight from the dense adj sheets.
- Match's writes to `adj_family`/`adj_sponsor` are column-scoped
  (assignment columns only); audit's writes to the same sheets never
  touch the assignment columns (preserved on every family/sponsor
  save).
- Module 5's `generated_info_sheet` flag moves from `final_child` to
  `adj_child` (the final is a projection, so the flag must live in the
  source of truth); module 5 then refreshes the `final_child`
  projection.

## 5. Adjudication, Status, and Ignore Model

### 5.1 Dense adjudication

An adj row's data columns ARE the adjudicated record — initialized from
the entity's baseline (§5.2), pre-filled by screening where it ran
(§7.3), then edited by the human. The audit UI diffs adj against the
raw baseline to show what changed; raw is never consulted when building
finals.

### 5.2 Baselines (initialization and diff reference)

Baselines come from the prepared submissions rows (§4.2; legacy
workbooks fall back to their raw rows):

- **Sponsor**: the sponsor's prepared submission row.
- **Family**: the group's survivor submission (earliest `created`) —
  duplicates share one `family_id`, so the survivor seeds the adj row;
  the group's other submissions appear side by side in the UI (§6.1)
  and the whole group is flagged `potential_duplicate` (§6.4).
- **Child**: the combined duplicate-group values (§6.2) — the kept
  (latest) submission with nulls filled from earlier submissions.

### 5.3 Status state machine

One enum, shared by all three entities, stored as a snake_case string:

| Status               | Color  | Meaning                                              |
|----------------------|--------|------------------------------------------------------|
| `new`                | blue   | Never touched by a human; adj equals baseline; no note. |
| `in_progress`        | yellow | At least one adjudicated data value differs from the baseline. |
| `needs_clarification`| orange | A clarification note is present.                     |
| `ignored`            | red    | Ignore flag checked.                                 |
| `audited`            | green  | Audited checkbox checked.                            |

Computed as a pure function, evaluated live in the UI and persisted on
save:

```
status(ignore, audited, has_note, has_edits):
    if ignore:               return "ignored"
    if audited:              return "audited"
    if has_note:             return "needs_clarification"
    if has_edits:            return "in_progress"
    return "new"
```

Precedence rationale: `ignored` and `audited` are explicit human
decisions and override everything; between the two derived states, a
note is the actionable item (someone must follow up), so it outranks
"in progress". A note + edits with `audited` unchecked shows
`needs_clarification`; checking `audited` flips it to `audited` while
the note stays stored. Unchecking `audited` recomputes from note/edit
state — the dynamic behavior requested.

Definitions:

- `has_edits` — any adjudicable data column differs (string-normalized
  compare) from the entity's baseline (§5.2). Screening corrections
  count: the adjudicated record *is* changed and a human still has to
  confirm or annotate it. So does the duplicate-group combine pre-fill
  where it fills blanks the kept submission left empty — deliberate:
  a merged record deserves a human look.
- `has_note` — `require_clarification` non-empty after strip. The
  auto-filled orphan note (§8.7) counts, which correctly makes orphans
  surface as `needs_clarification`.
- Special-needs selection is a control, not an override: selecting
  options does not by itself set `in_progress` (mirrors today's
  checkbox, which never affected queue state).
- Computed columns (`age_at_xmas`, `pref_age_min/max`) and assignment
  columns are excluded from `has_edits` (they follow their inputs).

The stored `status` column is a denormalization recomputed on every
audit save (never edited directly), so the workbook can be filtered in
Excel without opening the UI — and so downstream modules can gate on it
(§7.5).

### 5.4 Readiness

An entity is **ready** when its saved `status` is `audited`. Matching
and child info-sheet generation gate children on exactly this (§7.5).
(The explicit `audited` Boolean column and `status == "audited"` are
equivalent by construction — §5.3 — but downstream modules read
`status`, one uniform gate for all three sheets.)

### 5.5 Ignore semantics

Checking **Ignore** and saving:

1. `ignore` is set true; `status` becomes `ignored`.
2. The row **stays in the adj sheet** but is reduced to its identifying
   skeleton: every column except the ones below is set to null —
   - the key (`zite_record_id` / `family_id` / `sponsor_id`),
   - `zite_record_id`,
   - `first_name` and `last_name` where the entity has them,
   - the primary contact email (`email` for sponsors, `primary_email`
     for families/children),
   - the control columns (`status`, `audited`, `ignore`,
     `require_clarification`, `audited_at`, `screened`).
   Everything else — data, computed, and assignment columns — is
   nulled. This keeps ignored rows visibly identifiable in the sheet
   ("which rows are removed") while carrying no payload.
3. The row's projection is excluded from the final sheet, so ignored
   entities disappear from `final_*`, matching, packets, and reports.

The UI confirms before saving an ignore (it is destructive to the adj
data). **Un-ignoring** re-seeds the data columns from the baseline
(§5.2) — previous adjudicated edits to that row are lost, and the
confirm dialog says so. Assignment columns re-populate on the next
`ccat match` run.

### 5.6 Group-level ignore

- Sponsors have no grouping (acquire does not dedup sponsors); ignore
  applies to the sponsor.
- Family ignore = the whole submission group is one family row; the
  ignore flag lives on that row.
- Child ignore = the whole duplicate group: the group has exactly one
  adj row (the canonical/kept submission's), so the single flag covers
  every submission. "Ignore applied to all but one entry" is satisfied
  structurally — non-canonical submissions never have adj or final
  rows at all; the group view labels them "not in final_child
  (duplicate)" and marks the canonical row "canonical".

## 6. Duplicate Submission Groups

### 6.1 Family groups

Groups are rebuilt on every audit page load by joining:

- `family_submissions` grouped by `primary_email` (rows without email
  are singleton groups), ordered by (`created`, `zite_record_id`)
  ascending — the exact order acquire's dedup uses;
- the `raw_family` survivor row for the group (matched on
  `primary_email`).

The family detail page shows one column per submission — `raw_1` …
`raw_n` — plus the adjudicated column (the dense `adj_family` row).
Raw columns where the submissions disagree (two or more distinct
non-null values across the group) get a conflict marker (amber dot /
border on the cell), mirroring what `dedup_log` recorded, so the
auditor knows which fields were contested.

### 6.2 Child groups

Same mechanism: `child_submissions` grouped by
(`primary_email`, `first_name`) (rows without either are singletons),
ordered by (`created`, `zite_record_id`) **descending** — the child
dedup keeps the *latest* submission. The kept submission's
`zite_record_id` is the group's canonical key and the id carrying the
`adj_child` row.

The child detail page shows `raw_1 … raw_n` (newest first, so `raw_1`
is the kept/latest submission) plus the adjudicated column (the dense
`adj_child` row), which is pre-filled by **combining the matched
entries**: per field, the most recent non-null value across the group
(the latest submission's value when it has one, falling back to earlier
submissions for fields the latest left blank). This preserves the
"latest wins" pipeline rule while filling gaps from earlier
submissions.

### 6.3 Baselines and groups

The baseline used for diffing and status (§5.2) is the group-aware
value: the coalesced survivor for families, the combined value for
children. A dense adj row that exactly equals its baseline is "new"
(nothing adjudicated yet); any deviation is an edit.

## 7. `final_*` Generation and Pipeline Integration

### 7.1 Projection rule

For each entity, `final_X = project(adj_X)`:

1. Drop every row with `ignore == true`.
2. **`final_child` only:** drop orphaned children — rows with a null
   `family_id` (no matching family registration). They stay in
   `adj_child` for the audit tool to fix and never reach
   `final_child` (so downstream modules — match joins, generate, stats
   `--raw`, clarify — never see them).
3. Drop the pure control columns: `audited`, `ignore`, `screened`
   (child only).
4. Keep everything else — all data columns, computed columns,
   assignment columns, `special_needs`/`potential_anomoly`/
   `generated_info_sheet` (child), `require_clarification`, `status`,
   and `audited_at`.
5. Column order = the documented final-sheet order.

`final_child` therefore gains `status` and loses the `audited` Boolean;
`special_needs` is the joined String in both adj and final (see D4,
§14). `final_family`/`final_sponsor` gain `status` and keep all
assignment columns. The projection is one shared, unit-tested helper
(§9.2); each owner module calls it after its writes.

`final_child.age_at_xmas` stays a live formula column (pipeline doc
§4). The projection passes adj's `age_at_xmas` through, but
`write_sheets` recomputes every row's age from its `dob` at write time
and stores it as `=DATEDIF(<dob cell>, DATE(Y,M,D), "y")` with the age
as the cached value — so an adjudicated `dob` fix carries its matching
age into the final automatically. The adj column is authoritative in
the final only for rows whose `dob` is missing or unparsable (the
written value is kept, with a logged warning). `final_family`/
`final_sponsor` carry no formulas.

### 7.2 Who projects what

- **Audit** — projects `final_child` from `adj_child`: at startup
  (after seeding, §7.4) and after every child save. Family/sponsor
  saves update only `adj_family`/`adj_sponsor`; their finals refresh on
  the next `ccat match` (full or `--skip-match`) run — the documented
  workflow: *audit, then match*.
- **Match** — computes assignments, writes them into the assignment
  columns of `adj_family`/`adj_sponsor`, then projects
  `final_family`/`final_sponsor`. `--skip-match` reconciles assignments
  inside the adj sheets (carrying over existing values, propagating
  `prior_assigned_angel_ids`) and re-projects the finals without
  running the algorithm.
- **Generate (module 5)** — writes `generated_info_sheet` into
  `adj_child` and refreshes the `final_child` projection.
- **Migrate (bootstrap, §11)** — builds all adj sheets in the new
  workbook and projects all three finals once.

Two io-layer rules bind every projection writer (pipeline doc §4):

- Any `write_sheets` call that includes `final_child` must pass
  `xmas=cfg.xmas`; omitting it raises `SheetFormulaError` (the ages
  cannot be stored as live formulas without the Christmas date).
  Audit's startup projection, `save_child`, module 5's refresh, and
  migrate's step 5 all pass it.
- Any `write_sheets` call that re-saves the workbook *without*
  `final_child` — adj-only saves, screening's `adj_child` writes,
  match's assignment writes and family/sponsor projections — triggers
  io's automatic capture/restore of the `final_child` formula caches
  (openpyxl drops cached results on re-save; `ccat.io` re-embeds
  them, keeping pipeline reads non-null). No store-side code is
  needed, but every workbook write implicitly reads `final_child`'s
  sheet XML, and tests must assert the caches survive adj-only saves
  (§12).

### 7.3 Screening interplay (module 2 changes)

- Screening writes **dense** `adj_child` rows for every child it
  screens: the child's raw values overlaid with the model's corrections
  (only the model-owned fields: `dob`, `need_1`, `need_2`, `desire_1`,
  `desire_2`, the size columns, `favorite_colors`, `interests`,
  `other_notes`, `potential_anomoly` — the current
  `RESPONSE_COLUMNS`), with `screened = true` and default controls.
  For a child whose model output is "no changes", the dense row simply
  equals the raw row.
- Target selection: children whose adj row is absent **or** has
  `screened != true` (the explicit marker replaces the old
  row-presence invariant, because audit also seeds rows).
- If audit has already seeded a row (a human worked ahead of screening),
  screening applies a correction to a model-owned field only when that
  field still equals the baseline (untouched by the human); fields the
  human already changed are left alone. Controls are always preserved.
- `--redo` / `--overwrite`: replace the model-owned data cells per the
  rules above; never touch human edits in other fields, controls, or
  assignment/generated columns.

### 7.4 Audit seeding (`ensure_adj_sheets`)

`ccat screen` seeds all three adj sheets itself now (pipeline arch
§6.9): dense rows seeded from the prepared submissions (§5.2/§6),
computed columns filled, `potential_duplicate` flags and
validation/duplicate notes applied.  On audit startup the store runs an
idempotent backstop: every prepared entity still missing an adj row
(e.g. a legacy workbook never re-screened) gets one — dense data
columns seeded from the baseline, computed columns filled
(`age_at_xmas`, `pref_*`), assignment/generated columns null/false,
controls defaulted (`status` "new", `audited`/`ignore` false,
`screened` false, no note). Then `final_child` is re-projected.

### 7.5 Downstream modules

- **Match** — reads `adj_child` (pool: `status == "audited"`, `ignore
  != true`, non-null `angel_id`), `adj_family` and `adj_sponsor`
  (exclude `ignore == true`; all sponsor/family data — `num_children`,
  `age_preference`, `prior_assigned_angel_ids`, location — comes from
  the dense adj rows, so the v1 raw⊕adj overlay logic disappears).
  Unresolved duplicates (same email, both non-ignored) that reach the
  pools are reported in `match_report` (kind `duplicate`) — screening
  flags them (§6.4); the user resolves them by ignoring the extras.
  Special-needs sponsor matching: a child counts as special-needs iff
  its `special_needs` string is non-empty. Writes assignments +
  `num_angels_in_family` (count of the family's non-ignored `adj_child`
  rows) into adj, projects finals, writes `match_report` as today.
  `pref_age_min`/`pref_age_max` are read from adj (audit keeps them
  consistent with the adjudicated `age_preference`).
- **Generate (module 5 + 5b)** — reads unchanged (`final_child` joined
  with `final_family`/`final_sponsor`), with one gate change: a child's
  info sheet is generated only when `final_child.status == "audited"`
  (replacing the old `audited == true` check — module 5 currently gates
  on family assignment, which stays). Writes the flag into
  `adj_child.generated_info_sheet` and refreshes the projection with
  `write_sheets(..., xmas=cfg.xmas)`. The parallel machinery stays
  exactly as built: thread pool of `info_sheet.max_workers` workers
  (default 4; `--max-workers` override; clamped to pending rows), one
  LibreOffice profile per worker, tqdm progress, Ctrl+C cancelling
  queued rows, killing in-flight soffice trees as a process group, and
  committing finished flags in one atomic write (exit 130; interrupted
  children stay pending). One adaptation: the flag-commit path — both
  the normal end-of-run write and the interrupt commit — now writes
  `adj_child` (a whole-sheet dense upsert that must preserve every
  other column; reuse the store's save helpers, which have no NiceGUI
  imports, rather than rebuilding frames ad hoc) and refreshes
  `final_child` in the same atomic `write_sheets` call, so a crash can
  never leave the flag and the projection out of step.
- **Clarify (module 7)** — reads `final_child.require_clarification`
  (kept by the projection) and `final_family`/`raw_child` as today; no
  changes.
- **Stats (module 6)** — reads the final sheets as today; no changes
  (it never gated on `audited`).

## 8. UI Specification (NiceGUI)

Global shell on every page: header with season title, a left/top nav
(Dashboard / Sponsors / Families), and the page content. Colors:
`new` = blue, `in_progress` = yellow, `needs_clarification` = orange,
`ignored` = red, `audited` = green (`ui.badge` colors).

### 8.1 Dashboard (`/`)

Grid of two large buttons (cards), each navigable:

- **Sponsors** → `/sponsors`. Card stats: total sponsors, counts per
  status (new / in progress / needs clarification / ignored / audited),
  and "audited X / N (P%)".
- **Families** → `/families`. Card stats: total families, counts per
  status, plus child audit progress: children audited X / N (P%), child
  needs-clarification count.

Stats are computed at page load from the adj sheets; a low-frequency
`ui.timer` refreshes them while the page is open. Clicking a card
navigates (`ui.navigate.to`).

### 8.2 Sponsors list (`/sponsors`)

List view of **all** sponsors (ordered by `sponsor_id`). Each row:
sponsor number (`sponsor_id`), affiliation (`sponsor_type`), contact
name (`first_name` + `last_name`), organization name when present, and
the status badge. A text filter and a status filter (including "all")
narrow the list — this replaces the old `--hide-completed` flag.
Clicking a row opens `/sponsors/{sponsor_id}`.

### 8.3 Sponsor detail (`/sponsors/{sponsor_id}`)

Layout (mirrors the current child audit page's table, one field-name
column on the left):

- Header: sponsor number, contact/org identity, dynamic status badge,
  and the control row — **Audited** checkbox, **Ignore** checkbox,
  **Save** button, and prev/next navigation within the sponsor list.
- Field table (`ui.table`): columns `Field | Raw value | Adjudicated
  value`. Protected columns (key, `zite_record_id`, `create`, `source`,
  `accepts_disclaimer`, `pref_age_min/max`, assignment columns) show as
  read-only rows with a greyed adj cell (the "absent" convention from
  today's UI). Adjudicated cells are inputs/textarea (multiline for long
  text); enum fields render as selects bounded by the config
  vocabulary. A cell whose value differs from the raw baseline gets the
  yellow "diff" highlight — same indication of modification as the
  current child audit.
- Field order — important first: `sponsor_type`, `organization_name`,
  `first_name`, `last_name`, `email`, `phone`, `num_children`,
  `age_preference`, `gender_preference`, `dropoff_location`,
  `prior_assigned_angel_ids`. Secondary at the bottom (read-only):
  `pref_age_min`, `pref_age_max`, `accepts_disclaimer`, `create`,
  `source`, `zite_record_id`, `assigned_angel_ids`,
  `num_angels_assigned`.
- Clarification notes: a `ui.textarea` ("require_clarification").
- The status badge recomputes live from the current editor state
  (§5.3); Save persists the dense adj row + controls + recomputed
  `status` (recomputing `pref_*`), preserves assignment columns, then
  re-renders. Checking **Ignore** prompts the §5.5 destructive-action
  confirmation; after save the table shows the identity skeleton and an
  "Un-ignore (restores raw values)" action with its own confirmation.

### 8.4 Families list (`/families`)

List view of all families (ordered by `family_id`). Each row:
`family_id`, contact first/last name, number of valid children (count
of `adj_child` rows linked to the family — orphans are attached to no
family and are not counted), and the status badge. Filter controls as
on the sponsor list. Below the family list, an **Orphaned children**
section lists children with null `family_id` (see §8.7). Clicking a
family opens `/families/{family_id}`.

### 8.5 Family detail (`/families/{family_id}`)

- Header: family id, contact name, dynamic status badge, control row
  (Audited / Ignore checkboxes, Save, prev/next).
- Family field table: columns `Field | raw_1 | … | raw_n | Adjudicated`
  (§6.1). One submission per raw column; a family with a single
  submission shows just `raw_1`. Cells where submissions conflict are
  conflict-marked (§6.1). The adjudicated column is the dense
  `adj_family` row — pre-filled with the coalesced values, editable,
  with the diff highlight against the coalesced baseline.
- Family field order — important first: `first_name`, `last_name`,
  `primary_email`, `phone`, `street_address`, `apt_floor`, `city`,
  `zip_code`, `state`, `gift_pickup_location`, then the secondary
  contact block (`secondary_first_name`, `secondary_last_name`,
  `secondary_email`, `secondary_phone`). Secondary (read-only):
  `accepts_disclaimer`, `created`, `zite_created_at`, `source`,
  `zite_record_id`, `assigned_sponsor_id`, `assigned_angel_ids`,
  `num_angels_in_family`.
- Clarification notes textarea; ignore/un-ignore per §5.5.
- **Children list view**: every child of the family (from `adj_child`,
  ordered by angel letter). Each row: child number (1-based position in
  the family, matching angel-letter order), `angel_id`, first name,
  date of birth, gender, and the child's status badge. Clicking opens
  `/families/{family_id}/children/{zite_record_id}`.

### 8.6 Child detail (`/families/{family_id}/children/{zite_record_id}`)

Same structure as sponsor/family detail:

- Header: child number + angel id + name, dynamic status badge, control
  row (Audited / Ignore / Save, prev/next within the family's
  children).
- Field table: `Field | raw_1 | … | raw_n | Adjudicated` when the child
  has duplicate submissions (§6.2), else `Field | Raw | Adjudicated`.
  The adjudicated column is the dense `adj_child` row, pre-filled per
  §6.2, diff-highlighted against the baseline.
- Child field order — important first: `first_name`, `dob`,
  `age_at_xmas` (read-only, computed), `gender`, `special_needs` (the
  selector, below), `need_1`, `need_2`, `desire_1`, `desire_2`,
  `shoe_size`, `shoe_size_type`, `pant_waist_size`, `pant_inseam`,
  `pant_size_letter`, `pant_size_type`, `jean_size`,
  `shirt_size_letter`, `shirt_size_type`, `favorite_colors`,
  `interests`, `other_notes`, `potential_anomoly`. Secondary
  (read-only): `age_reported` (shown beside the computed age, with a
  mismatch warning), `primary_email`, `created`, `zite_created_at`,
  `source`, `zite_record_id`, `family_id`, `angel_id`,
  `generated_info_sheet`, `screened`.
- **Special needs selector**: checkbox list defaulting to none, with
  options `ADHD`, `Autism`, `Down Syndrome`, `other`. Checking `other`
  reveals a free-text input for the custom value. Stored as the
  semicolon-joined list (§4.3), e.g. `"ADHD; other: speech delay"`.
- Clarification notes textarea (the orphan note auto-fills for
  orphans, §8.7).
- Save: persist the dense `adj_child` row (data + controls + recomputed
  `status` and `age_at_xmas`, `generated_info_sheet` preserved),
  preserve nothing else to touch, then re-project `final_child`
  (§7.2). Orphan rule: `audited` cannot be checked while `angel_id` is
  null.

### 8.7 Orphans (children with no family)

Orphaned children (null `family_id`/`angel_id`) appear in the
**Orphaned children** section of `/families` with a warning badge.
Their detail pages show the orphan banner and keep the existing rule:
`audited` cannot be checked while `angel_id` is null (the checkbox
disables; the row can still be saved with a note — status
`needs_clarification` via the auto-filled note). Once a matching family
registration appears (re-run `ccat acquire`), the child links and the
audit can complete.

### 8.8 Shared components

Implemented once, reused by all three detail pages:

- `status_badge(status)` — colored `ui.badge`.
- `audit_table(field_specs, raw_columns, adj_values, baseline)` — the
  Field | raw_1..raw_n | Adjudicated table with diff/conflict
  highlighting and per-field editors.
- `controls_row(entity)` — Audited/Ignore checkboxes + Save + prev/next
  (+ the ignore/un-ignore confirmations).
- `notes_editor()` — the require_clarification textarea.
- `special_needs_selector()` — the checkbox list + custom input.
- `entity_list_row(...)` — the list-view cards.

All workbook reads/writes go through `background.io_bound` (clarify
pattern); feedback via `ui.notify`; errors never kill the page.

## 9. Backend Design

### 9.1 Package layout

```
src/ccat/audit/
  __init__.py        # AuditError
  store.py           # pure data layer — no NiceGUI imports
  migrate.py         # one-time bootstrap from the old workbook (§11)
  pages.py           # NiceGUI pages + shared components
  server.py          # run(): load state, seed sheets, ui.run
```

`static/` and the http.server code are deleted. The process-discovery
helpers (`find_audit_processes`, `stop_audit_processes`,
`_is_audit_command`, `_ancestor_pids`) move into the new `server.py`
unchanged — `--restart` keeps working with NiceGUI.

### 9.2 `store.py` — data layer (unit-testable without a browser)

Pydantic models: `SponsorEntry`, `FamilyEntry`, `ChildEntry` (identity,
raw baseline, group submissions, dense adj row, derived status), plus a
`ControlState` (audited/ignore/note) shared by all three.

Functions (signatures, not implementations):

- `ensure_adj_sheets(cfg) -> None` — §7.4 seeding; idempotent; its
  `final_child` re-projection passes `xmas=cfg.xmas` (§7.2).
- `project_final(adj_frame, final_columns) -> pl.DataFrame` — the
  shared §7.1 projection helper.
- `load_audit_data(cfg) -> AuditData` — one read of the adj/raw/
  submission sheets; builds entries, groups (§6), orphan list, and
  dashboard stats.
- `compute_status(has_edits, has_note, audited, ignore) -> str` — the
  §5.3 pure function.
- `coalesce_family_group(submissions) -> dict` — first-non-null by
  created order (semantics identical to `dedup_families`).
- `combine_child_group(submissions) -> dict` — most-recent-non-null
  (§6.2).
- `reduce_ignored(row) -> dict` — the §5.5 identity-skeleton nulling.
- `save_sponsor(cfg, sponsor_id, payload) -> SponsorEntry` /
  `save_family(...)` / `save_child(...)` — dense upsert (assignment and
  `generated_info_sheet` columns preserved), computed-column refresh
  (`age_at_xmas` via `ccat.acquire.compute_age_at_xmas` — the same
  helper `ccat.io` uses for the final formulas; `pref_*`), status
  recompute, ignore reduce/restore, atomic sheet write — `save_child`'s
  `final_child` write passes `xmas=cfg.xmas`, sponsor/family adj-only
  writes lean on io's automatic cache restore (§7.2); `save_child`
  re-projects `final_child` (reusing today's `dob_conflict_notes` +
  `merge_anomoly` helpers for the idempotent conflict-note merge into
  `potential_anomoly`).
- A module-level `threading.Lock` serializes read-modify-write cycles
  (single human user, but multiple browser tabs must not interleave).

Reading the whole workbook per page load is fine at this program's
scale (hundreds of rows); `load_audit_data` may cache by workbook
mtime if profiling ever demands it.

### 9.3 `pages.py` / `server.py` — NiceGUI layer

Follows `ccat.clarify.server` exactly: a module-global `AuditState`
(cfg + last-loaded `AuditData`) set by `run()`; `@ui.page` handlers;
`ui.run(host=cfg.audit.host, port=cfg.audit.port, title=..., reload=
False, show=True)`. Route parameters via `@ui.page('/sponsors/{sponsor_id}')`.
NiceGUI is already a project dependency (clarify); no new dependencies.

## 10. Config, CLI, and Workbook Hosting

### 10.1 New config and workbook (created with this document)

- `config/ccat_2026_v2.json` — a verbatim copy of
  `config/ccat_2026.json` (same Zite references, table specs,
  vocabularies, AI screening and clarify prompts, matchmaking,
  info-sheet, report, and sponsor-packet settings) with exactly three
  changed paths:
  - `excel_file`: `./output/ccat_2026_v2.xlsx` (the new workbook),
  - `log_file`: `./output/ccat_2026_v2.log`,
  - `match_report_csv`: `./output/ccat_2026_v2_match_report.csv`.

  The copy picks up `info_sheet.max_workers` (the parallel-generation
  setting, validated positive by `InfoSheetSpec`); the existing
  `config/ccat_2026_v2.json` already carries it. Re-copy (or re-diff)
  whenever `config/ccat_2026.json` changes before cutover.
- `output/ccat_2026.xlsx` and `config/ccat_2026.json` remain the
  untouched system of record; no command run with the old config ever
  writes the new workbook and vice versa. The clarify output
  directories are shared deliberately (family ids are stable across the
  copy-based bootstrap, so existing email drafts remain valid).
- `DEFAULT_CONFIG` in `ccat.__init__` reads the ``DEFAULT_CONFIG``
  environment variable (exported by ``.envrc``) and falls back to
  `config/ccat_2026.json`.  The user's cutover (§14 Q1) points the
  environment at `config/ccat_2026_v2.json`; the in-repo fallback flips
  only when that switch is committed.  Tests pin the fallback.
- Both audit servers default to port 8642 — only one runs at a time;
  `--restart` handles a stale process.

### 10.2 CLI

- `ccat audit [--config PATH] [--test] [--restart]`:
  - `--restart` — unchanged (stops a stale audit process first).
  - `--test` — keeps parity with the other modules: all lists are
    limited to the first family (lowest `family_id`), its children, and
    the first sponsor; the dashboard reflects the reduced scope.
  - `--overwrite`, `--hide-completed` — **removed**. No frozen rows
    (audited is a revocable checkbox); completed-hiding is a list
    filter in the UI.
- `ccat audit-migrate --config config/ccat_2026_v2.json --from
  ./output/ccat_2026.xlsx` — the one-time bootstrap (§11). Refuses to
  run if the target workbook already contains adj sheets (override
  requires an explicit `--force` that re-creates them from the source).
- `ccat.io`: `SHEETS`, `WRITABLE_SHEETS`, `READABLE_SHEETS` updated per
  §4.4 (including the new `migrate` writer).
- Config schema (`AuditSpec` etc.): **no changes required.** The
  special-needs option list (ADHD / Autism / Down Syndrome / other) is
  a UI constant; if it needs to vary per season it can later move into
  `config.audit` — not proposed now (YAGNI).

## 11. Migration — Bootstrap of the New Workbook

One-time, `ccat audit-migrate --config config/ccat_2026_v2.json --from
./output/ccat_2026.xlsx`. The old workbook is opened **read-only**
(polars reads only; no `write_sheets` call ever targets it). Steps:

1. **Guard** — refuse when the new `excel_file` already contains adj or
   final sheets (or `--force` was given).
2. **Copy raw layer** — `raw_sponsor`, `raw_family`, `raw_child`,
   `dedup_log`, `dob_conflict_log` copied verbatim from the old
   workbook. `family_submissions`/`child_submissions` do not exist in
   the old workbook; they are created empty and fill on the next
   `ccat acquire` run (until then the UI shows single-column groups —
   correct, since the copied raw rows are already the deduped
   survivors). Copying (rather than re-pulling from Zite) makes the
   bootstrap deterministic: the new workbook starts byte-identical on
   the raw layer, so every id joins exactly.
3. **Build dense `adj_child`** — for every `raw_child` child:
   - start from the raw row;
   - overlay the old sparse `adj_child` overrides (where present);
   - for children with an old `final_child` row, take the old final
     row's data columns as the dense values (it is the most recent
     adjudicated truth: raw ⊕ adj ⊕ human audit edits), and set the
     controls from it: `audited`, `require_clarification`, `audited_at`
     copied; `special_needs` Boolean true → the string
     `"other: unspecified"`; `generated_info_sheet` copied. The old
     final's `age_at_xmas` cells are live formulas, but polars reads
     their cached values (io's cache-restore keeps them non-null), so
     the takeover sees plain integers;
   - `screened` = the child had an old `adj_child` row;
   - `status` computed (§5.3).
4. **Build dense `adj_family` / `adj_sponsor`** — seeded from the raw
   rows (families/sponsors were never audited in the old system);
   assignment columns copied from the old `final_family`
   (`assigned_sponsor_id`, `assigned_angel_ids`) and `final_sponsor`
   (`assigned_angel_ids`, `num_angels_assigned`) so existing matches
   survive without re-running the algorithm;
   `num_angels_in_family` recomputed from the new `adj_child`. Controls
   default (`status` "new" — accurate: never human-audited).
5. **Project all three finals** (§7.1; the `final_child` write passes
   `xmas=cfg.xmas` so the live `DATEDIF` formulas are injected into
   the new workbook — no retrofit script needed) so downstream modules
   work immediately; log a summary (entities migrated, audited counts,
   children with notes, assignments carried over).
6. **Never write the `--from` workbook.**

Post-migration workflow: `ccat audit --config config/ccat_2026_v2.json`
(human audit continues; `ensure_adj_sheets` is a no-op), `ccat match
--config ... --skip-match` when finals need reconciling after audit
edits, `ccat acquire --config ...` when fresh Zite data is wanted
(after which the submission sheets appear and the duplicate-group
columns light up; adj joins survive on `zite_record_id`, and
`family_id`/`sponsor_id` are stable under acquire's deterministic
ordering).

## 12. Testing Plan

- `test/test_audit_store.py` (new, the bulk of coverage):
  - `compute_status`: full precedence table, dynamic recompute on
    audited-deselect.
  - `ensure_adj_sheets` seeding: dense rows equal baselines, controls
    defaulted, idempotent.
  - `project_final`: ignored rows excluded, `audited`/`ignore`/
    `screened` dropped, `status`/notes/`audited_at` kept, column order.
  - `reduce_ignored`: exactly the retained columns survive; everything
    else null; un-ignore re-seeds from baseline.
  - `coalesce_family_group` / `combine_child_group`: ordering, null
    fall-through, conflicting values, email-less rows.
  - `save_child` → `final_child` re-projection: dense round-trip,
    special-needs string, orphan audited rejection, ignore → exclusion,
    dob-conflict merge idempotence, `generated_info_sheet` and
    assignment preservation.
  - `save_sponsor`/`save_family`: dense upserts, `pref_*` recompute,
    status persistence.
  - Formula integration (pipeline doc §4 mechanics): `save_child` and
    `ensure_adj_sheets` pass `xmas` (a `final_child` write without it
    raises `SheetFormulaError` — regression guard); after a child save
    the final's `age_at_xmas` cells are formulas whose cached values
    match the adjudicated `dob`; adj-only saves (sponsor/family)
    leave the `final_child` formula caches intact.
- `test/test_audit_migrate.py` (new): old-workbook fixture (raw +
  sparse adj_child + final_child with audited/notes/bool special needs
  + final_family/final_sponsor assignments) → new workbook: dense adj
  correctness, control backfill, assignment carry-over, projections,
  refusal when target non-empty, source workbook never written.
- `test/test_acquire.py`: submission sheets written pre-dedup, sorted,
  content = cleaned rows (including rows that later fail pandera).
- `test/test_screening.py`: dense writes, `screened` marker, target
  selection (absent row or `screened != true`), human-edit protection,
  control preservation on redo/overwrite.
- `test/test_match.py`: adj-based pools (no raw reads), status gate,
  ignore exclusion, special-needs string test, assignment writes into
  adj, final projections, `--skip-match` reconcile.
- `test/test_child_info_generate.py` (existing — extended; the
  thread-pool rework's tests stay): `status == "audited"` gate, flag
  target `adj_child.generated_info_sheet`, projection refresh with
  `xmas`, interrupt path committing flags + projection atomically,
  other `adj_child` columns preserved by the flag upsert.
- Old `test_audit.py` http-server/queue tests are removed with the old
  server — including its recent `xmas` pass-through updates; the
  formula mechanics themselves stay covered by `test_io.py`.
  Still-relevant pure helpers (`dob_conflict_notes`, `merge_anomoly`)
  keep their tests under the new store module.
- NiceGUI UI smoke tests (optional follow-up, `nicegui.screen`):
  dashboard renders, list → detail navigation, save round-trip,
  ignore/un-ignore flow.

## 13. Implementation Phases

Each phase ends green: `ruff format`, `ruff check --unsafe-fixes`,
`ty check`, `pytest`.

1. **Data layer plumbing** — `io.py` sheet/matrix updates (incl. the
   `migrate` writer; the `xmas`/formula-injection plumbing already on
   main is reused as-is); acquire submission sheets; dense adj
   schemas + screening dense writes with the `screened` marker.
2. **Store layer** — models, seeding, groups, status machine, dense
   saves, ignore reduce/restore, projection helper.
3. **Match rework** — adj-based pools, status gate, assignment writes
   into adj, final projections (both full and `--skip-match`).
4. **UI foundations** — package restructure (delete http server +
   static), `server.run`, dashboard, shared components, sponsor list +
   sponsor detail pages.
5. **UI completion** — families list + family detail (groups, children
   list) + child detail (special-needs selector, duplicates) +
   orphans.
6. **Migration + close-out** — `ccat audit-migrate` bootstrap of
   `output/ccat_2026_v2.xlsx`, CLI flag changes, README +
   `pipeline_architecture.md` §4/§7/§9/§13.4 amendments (merge with
   the newer §4 live-formula and §13.4 parallel-generation sections —
   the audit rework replaces the old §7 audit-tool text but must keep
   the formula mechanics), optional UI smoke tests.

## 14. Decisions and Open Questions

Decisions made in this plan (flag any you want changed):

- **D1 — dense adj sheets**: adj rows carry the complete adjudicated
  record; no sparse "null = same as raw" sentinels. Nulls mean genuinely
  absent values.
- **D2 — finals are projections**: `final_*` built solely from `adj_*`
  (drop ignored rows + `audited`/`ignore`/`screened` controls; keep
  `status`, notes, `audited_at`). Audit projects `final_child` on save;
  match writes assignments into `adj_family`/`adj_sponsor` then projects
  the family/sponsor finals; module 5 writes `generated_info_sheet`
  into `adj_child`.
- **D3 — all data columns are adj-editable** (including `first_name`
  and `gender`); protected read-only columns are keys, linkage
  (`family_id`, `angel_id`), computed (`age_at_xmas`, `pref_*`),
  module-owned (assignments, `generated_info_sheet`, `screened`), and
  provenance (`zite_record_id`, timestamps, `source`,
  `accepts_disclaimer`). Under the dense model an adj `first_name` edit
  no longer disturbs dedup grouping (groups derive from the submissions
  sheets at display time only).
- **D4 — special needs as a string everywhere**: `adj_child` and
  `final_child` both carry the joined list (e.g. `"ADHD; other: speech
  delay"`); match treats non-empty as special-needs. The old Boolean
  disappears (migration maps true → `"other: unspecified"`).
- **D5 — status is derived, stored on save** for Excel filterability
  and downstream gating; the UI computes it live.
- **D6 — readiness = `status == "audited"`** for matching and child
  info-sheet generation; the `audited` Boolean column is kept only as
  the explicit checkbox state (equivalent by construction).
- **D7 — ignore is destructive and row-skeletonizing**: ignored adj
  rows keep only key, zite id, names, primary email, and controls;
  un-ignore re-seeds from baseline (edits lost, confirmed in UI);
  assignment columns re-populate on the next match run.
- **D8 — duplicate pre-fill**: family = coalesce first-non-null;
  child = most-recent-non-null (latest submission wins, blanks filled
  from earlier submissions).
- **D9 — submissions sheets** (vs. reconstructing groups from
  `dedup_log`): the log stores only the *coalesced* survivor + dropped
  duplicates, so exact per-submission display is impossible from it.
- **D10 — new workbook + config**: `config/ccat_2026_v2.json` →
  `output/ccat_2026_v2.xlsx` (created with this document);
  `output/ccat_2026.xlsx` is never modified; the copy-based
  `audit-migrate` bootstrap moves completed audit state across.
- **D11 — screening marker**: explicit `screened` column replaces the
  row-presence invariant; screening never clobbers human edits or
  controls.
- **D12 — adj keys**: `sponsor_id`/`family_id` (Int64) and
  `zite_record_id` (String); same stability assumptions the pipeline
  already makes (IDs deterministic under acquire's ordering rules).
- **D13 — age formulas coexist with the dense adj model**: adj sheets
  store `age_at_xmas` as a plain Int64 (no formulas outside
  `final_child`); `final_child` keeps the live `DATEDIF` formulas,
  with `write_sheets` recomputing each row's age from its `dob` at
  write time, so an adjudicated `dob` fix carries its matching age
  without store-side special-casing. Every `final_child` write passes
  `xmas=cfg.xmas` (`SheetFormulaError` otherwise); adj-only writes
  lean on io's automatic formula-cache restore. The adj `age_at_xmas`
  value reaches the final only for rows with a missing/unparsable
  `dob`.

Open questions (defaults chosen; confirm or override):

1. **Cutover timing** — decided: the `DEFAULT_CONFIG` environment
   variable (`.envrc`) points at `config/ccat_2026_v2.json`, so bare
   `ccat` commands use the v2 workbook; the repo-side fallback flips
   when the switch is committed.  The old config/workbook get archived
   when the user is satisfied.
2. Ignoring a family removes it from matching — should its audited
   children be excluded from the match pool too (plan: yes, §7.5), or
   re-pooled as unattached angels (not supported by the
   family-indivisibility rule, so effectively yes)?
3. Dashboard shows two buttons only (children reached via families) —
   correct, or add a third "Children" button with a global child list?
4. `--test` retained on `ccat audit` (first family + first sponsor
   scope) — keep, or drop in favor of pure unit tests?
5. Family/sponsor adj edits reach `final_family`/`final_sponsor` only
   on the next `ccat match` run (audit projects only `final_child`) —
   acceptable, or should audit also re-project the family/sponsor
   finals on every save (would need an ownership exception)?
