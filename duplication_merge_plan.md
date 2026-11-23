# Duplication audit — the merged "Current (editable)" column

Status: implemented. Branch `feature/duplication-merge-current`, off `main`
(`9e14478`), in the main worktree (no separate worktree).

## 1. The problem

The Duplications section's **Current (editable)** column shows one
member's row. For a sponsor group that is wrong twice over:

- the sponsor's data is scattered over N submissions, and the column
  shows only one of them, so the auditor assembles the merged entry by
  hand;
- the **final sponsor number** is not the auditor's to choose. The
  column binds to whichever member is the surviving entry, so the
  merged entry inherits that member's `sponsor_id` — the auditor cannot
  say "keep *this* number".

For a family group the members share one row, so there is nothing to
choose from; for a child group each member owns its own row.

## 2. What we build

The Current column becomes a **merged row**: one entry built from every
submission in the group, and for a sponsor group the auditor picks which
member's row the merged entry takes over — which is the sponsor number
it keeps.

### 2.1 The merge rule

For each field, the merged value is the **newest member's non-blank
value**; where the newest member leaves the field blank, an older
member's value fills it.

The two orders the owner described produce this same result:

| entity | described order | result |
|---|---|---|
| sponsor | oldest first, then the newest overwrites | newest non-blank wins |
| family, child | most recent first, then older fills the gaps | newest non-blank wins |

So one rule implements both. The fold runs over the group's members
newest first and only fills a field that is still empty.

The values come from the members' **raw submissions** — the
`duplicate_log` record of each member. That is the only per-member data
a family group has (its members share one row), and it is what the
table's columns show.

Fields that are not editable (linkage, assignment, controls) are not
merged: they stay with the row the merged entry is written to. The
entity's key (`sponsor_id`, `family_id`, `angel_id`) is never merged
either — it belongs to the row.

### 2.2 How the merge becomes real

The merge is a **preview**: the server returns it, the auditor edits
it, and the frontend sends the **whole editable column** with both save
actions. What the column shows is what gets written.

The server writes only what it is handed. That is what keeps two
existing rules true:

- auditing the same group twice stays `200` — the second audit's column
  is read-only (the row is audited), so it carries no values;
- an audited row's values stay final — the group's adjudication never
  rewrites them behind the auditor's back.

### 2.2 The row the merged entry takes over

The merged entry is written onto one of the group's existing member
rows. The default is the **surviving entry** (the member left
undecided), exactly as today.

For a sponsor group the auditor may pick a different member's
`sponsor_id` in the Current column. That pick rebinds the column to
that member's row, so:

- the merged values and the `audited` status are written onto that
  member's row;
- the previously surviving member's row is closed out like a released
  member (`status = new`, awaiting its own audit);
- the members marked `ignore` keep their ignored rows, untouched;
- a pick naming a member whose row is ignored is refused, because an
  ignored row is an identity skeleton and cannot become the audited
  entry;
- a pick naming a number outside the group is refused.

No sponsor is renumbered. The pick moves the merged **data** onto an
existing row; it never moves a number from one row to another. The
store forbids that anyway: `final_sponsor`'s primary key is
`(season_id, sponsor_id)`, `commit_finals` refuses to drop a stored key
(`EntityRemovalError`), and no path in the system deletes a final row.

### 2.3 What the auditor sees

- The Current column's header says the column holds the group's merged
  entry and which member's row it is written to.
- Every member is already a column of the table
  (`DuplicationDetailView.vue:721`), so a group of three sponsors shows
  three columns and all three feed the merge.
- A sponsor group gets a **Sponsor number** row in the Current column:
  a select over the group's member numbers, defaulting to the
  surviving entry's. Picking another number rebinds the column.
- A field whose merged value differs from every member's submitted
  value is badged `differs` — the marker now means "the auditor
  changed it", not "it differs from the first submission".

## 3. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | The merge is computed server-side and returned in the group detail as `current`. | The backend owns the rule; the frontend renders it. It also means the merge survives a page reload and a new screening run. |
| D2 | The merge reads the members' raw submissions, not their screened rows. | A family group's members share one row, so the raw records are the only per-member data. |
| D3 | One merge rule, not one per entity. | The two orders the owner described coincide. |
| D4 | The sponsor number is a **selector**, not a written value. | It picks the row the merge lands on. It is stripped before the save, so the row's own key is untouched and no ownership rule changes. |
| D5 | A pick naming an ignored member's row is refused. | An ignored row is reduced to its identity skeleton; it cannot become the audited entry. |
| D6 | The merged column's non-editable fields come from the bound row. | Linkage, assignment and control columns are module-owned; a merge must not move them. |
| D7 | The detail returns the merged **editable fields**, not a whole row. | The frontend composes them over whichever member's row the column is bound to, so rebinding the column needs no server round-trip. |

## 4. Backend

### 4.1 `src/ccat/services/db_duplications.py`

- `_merge_submissions(members) -> dict[str, Any]`: fold the members'
  raw records, newest first, newest non-blank wins.
- `_merged_current(entity, members) -> dict[str, Any]`: the fold,
  limited to the entity's editable fields.
- `group_detail` returns `current` (the merged editable fields) and
  `current_row_key` (the row it is written to).
- `mark_audited` accepts `values["sponsor_id"]` for a sponsor group:
  it names the target row, the previously surviving row is closed out
  with `status = new`, and the merged values are saved onto the target.

### 4.2 `src/ccat/api/dto.py`

`DuplicationDetail` gains `current: dict[str, Any]` and
`current_row_key: str`.

## 5. Frontend

- `frontend/src/api/duplications.ts`: the detail type carries
  `current` and `current_row_key`.
- `frontend/src/views/DuplicationDetailView.vue`:
  - the Current column's draft seeds from `detail.current`, and the
    read-only cells read from it;
  - a sponsor group gets a **Sponsor number** row: a select over the
    group's member numbers, whose change rebinds the column to that
    member's row (`boundKey`);
  - the pick is sent with the group audit request, where the backend
    applies it;
  - the `differs` badge compares against every member's submitted value;
  - the header explains what the column holds.

## 6. Tests

`test/test_duplication_merge.py`:

- the merge rule: newest non-blank wins; a blank in the newest falls
  back to an older member; three members; a field only the oldest
  holds; the fold is per field, not per row;
- the detail returns the merged row and the row it is written to;
- `mark_audited` with a picked `sponsor_id` writes onto that member's
  row, closes the previous survivor out with `status = new`, and leaves
  ignored rows alone;
- a pick naming an ignored member is refused; a pick outside the group
  is refused;
- the sponsor number invariant still holds: no row's `sponsor_id`
  changes.

`frontend/test/duplications.test.ts`: the merged column renders, the
sponsor number select rebinds the column, and the pick reaches the
audit request.

## 7. Out of scope

- Moving a sponsor number from one row to another (the store forbids
  it; the pick chooses which existing row the merge lands on).
- Deleting a final row.
- Merging the entity's key, linkage, assignment or control columns.
- Changing which member survives — the decisions still do that. The
  sponsor number pick chooses the row the merged entry is written to.
