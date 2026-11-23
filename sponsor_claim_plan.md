# Assigned Angel ID conflicts — implementation plan

Status: implemented. Branch `feature/sponsor-duplicate-approval`, off
`main` (`b517f13`).

## 1. The problem

A sponsor signs up and claims the children they will sponsor through the
Zite column **"Assigned Angel ID Code"**, which this repo stores as
`final_sponsor.prior_assigned_angel_ids` (`src/ccat/db/models.py:274`).
The value is a free-text list of angel ids — a *child* claim, not a
sponsor key — parsed by `match.parse_prior_assigned`
(`src/ccat/match.py:106`).

The program also runs **generic Walmart sponsors**: placeholder rows that
hold the angel ids allocated to a store. A real person later signs up and
enters the same Assigned Angel ID to take that slot over.

Two things happen today, both wrong:

1. `reconcile_pre_assignments` (`src/ccat/match.py:406-415`) resolves a
   double claim by **earlier sponsor id wins**. The generic Walmart row
   was created first, so it keeps the child and the real claimant gets
   nothing.
2. `_prior_child_assignments` (`src/ccat/match.py:655-668`) raises
   `MatchError: duplicate angel assignments` and the reconcile run dies.

Nothing surfaces the conflict. The Duplications section only knows about
groups the screening run found by comparing *sponsor emails*
(`preparation.detect_duplicates`, `src/ccat/preparation.py:227-320`).

## 2. What we build

A **sponsor angel-claim contest**: two or more sponsors whose
`prior_assigned_angel_ids` name the same angel id, and that angel id
resolves to a real child.

- The contest is detected deterministically at screen time and lands in
  the same `duplicate_log` the Duplications section already reads.
- The auditor resolves it in the Duplications section like any other
  group: leave one member undecided (it survives and keeps the angel),
  mark the others **ignore** (their rows are ignored, they leave the
  match pool, and the survivor's claim becomes the only claim).
- No sponsor id is ever renumbered. No ignored family or child ever
  loses its id to another row. Angel letters never skip.

## 3. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Detection lives in `preparation.detect_sponsor_claim_contests`, a pure function, and is called from `prepare_rows` after the children have their angel ids. | The `duplicate_log` is produced by `prepare_rows` and written by `screen` (`db_pipeline.py:548`). Keeping the detector pure makes it unit-testable with no database. |
| D2 | The contest rows reuse the existing `duplicate_log` table. No new column, no migration. | The table already carries `sheet`, and `sheet` is already the group's kind axis (`_SHEET_ENTITY`, `db_duplications.py:61`). A new sheet label `sponsor_angel_claims` maps to the `sponsor` entity, so every existing decision, trail and audit path works untouched. |
| D3 | The group key is the contested angel id itself (`"12A"`). | It is what the auditor needs to see, it is stable across re-screens (so the group id and the decision trail survive), and it cannot collide with an email group key. |
| D4 | The screen suggests the **latest** submission as the survivor. | The generic placeholder was created first; the person who signed up to take the slot is the later row. The suggestion is a default only — the auditor decides. |
| D5 | A contest where every claimant shares one email is not reported. | Same person, twice: the email duplicate group already owns it, and reporting it twice would ask the auditor to adjudicate the same row twice. |
| D6 | The loser is resolved with the existing `ignore` decision. | `ignore` keeps the sponsor id (`_IGNORE_RETAINED`, `audit/store.py:766-775`), blanks the claim, and drops the row out of the match pool (`match.py` sponsor pool is the non-ignored rows). The survivor then holds the only claim. |
| D7 | The match is unchanged. A pending contest does not reserve the angel. | The owner chose today's behaviour: the lower sponsor id keeps the angel until the auditor acts. The fix is in the audit, not in the matcher. |
| D8 | The Duplications list gets a `kind` filter and a badge; the entity filter is unchanged. | One list, two kinds of group. `kind` is `duplicate_submission` or `angel_claim_conflict`. |

## 4. Backend

### 4.1 `src/ccat/domain/__init__.py`

Move `parse_prior_assigned` here from `src/ccat/match.py:106-124`. It is
a pure primitive and `preparation.py` needs it; `match.py` already
imports from `ccat.domain`, and `ccat.match.parse_prior_assigned` keeps
working for `test/test_match.py:117`.

### 4.2 `src/ccat/preparation.py`

New pure function:

```python
def detect_sponsor_claim_contests(
    sponsor_rows: list[dict[str, Any]],
    child_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
```

- Parse each sponsor's `prior_assigned_angel_ids`.
- Keep only claims that name a child angel id present in `child_rows`.
- Group the claimants by angel id.
- Report an angel id when two or more sponsors claim it, unless every
  claimant has the same email (D5).
- Emit one `duplicate_log` row per claimant, shaped exactly like
  `detect_duplicates` emits:
  `sheet="sponsor_angel_claims"`, `group_key=<angel id>`,
  `zite_record_id`, `created`, `survivor` (latest submission, D4),
  `conflict_columns="prior_assigned_angel_ids"`, `conflict_details`
  naming the angel id and the claimants, `record=json.dumps(row)`.

`prepare_rows` calls it after `assign_angel_ids` / `_reconcile_child_ids`
so the child angel ids are known, and extends `duplicate_log`.

### 4.3 `src/ccat/services/db_duplications.py`

- `_SHEET_ENTITY` gains `"sponsor_angel_claims": "sponsor"`.
- New `_SHEET_KIND` map; `kind` is `"duplicate_submission"` for the
  three `*_submissions` sheets and `"angel_claim_conflict"` for
  `"sponsor_angel_claims"`.
- `list_groups` and `group_detail` return `kind`.
- `filter_groups` and `group_neighbours` take a `kind` argument, so the
  next/previous links stay inside the filtered list.

### 4.4 `src/ccat/api/dto.py` and `src/ccat/api/routes/duplications.py`

- `DuplicationListItem.kind` and `DuplicationDetail.kind` (default
  `"duplicate_submission"`).
- `GET /duplications` and `GET /duplications/{id}` take `kind`.

## 5. Frontend

- `frontend/src/api/duplications.ts`: a shared `DuplicationFilters` type
  carries `kind`; every helper sends it when it is set and not `all`,
  the same way `entity` works. `DuplicationKind` narrows the two values.
- `frontend/src/views/DuplicationListView.vue`: a kind chip group beside
  the entity chips ("All", "Duplicate submissions", "Assigned Angel ID
  conflicts"), a kind badge under each group's key, and `kind` in the
  URL filter round-trip.
- `frontend/src/views/DuplicationDetailView.vue`: for a contest the
  heading is the contested angel id, a block states what each decision
  does, and every member column shows its sponsor's Assigned Angel ID.
  The ignore dialog's wording is kind-aware.
- `frontend/openapi.json` and `frontend/src/api/schema.d.ts` are
  regenerated from the running API.

## 6. Invariants this feature must not break

| # | Invariant | Enforced by | Test |
|---|---|---|---|
| I1 | A sponsor's `sponsor_id` never changes, including when it is ignored or merged. | `_IGNORE_RETAINED` keeps it (`audit/store.py:766`); `_release_sponsor` keeps it (`db_duplications.py:864`); `_reconcile_sponsor_ids` keeps it by source id (`preparation.py:485`). | `test/test_duplications.py` |
| I2 | An ignored family's `family_id` is never handed to another family. | `_release_family` allocates above the maximum (`db_duplications.py:951`); `_reconcile_family_ids` keeps existing ids and allocates above the maximum (`preparation.py:653`). | `test/test_duplications.py` |
| I3 | An ignored child's `angel_id` is never handed to another child. | `_used_letters` counts ignored rows (`db_duplications.py:1484`); `_reconcile_child_ids` reserves every stored angel (`preparation.py:725`). | `test/test_duplications.py` |
| I4 | Angel letters do not skip: a family reads A, B, C. | `_free_letter` returns the lowest unused letter (`db_duplications.py:1509`); `_reconcile_child_ids` allocates contiguously above the reserved set (`preparation.py:771`). | `test/test_duplications.py` |

## 7. Tests

`test/test_sponsor_claims.py` (19 tests):

- the detector — no contest, a two-claimant contest, a three-claimant
  contest, a claim on an unknown angel, a same-email pair, a claim list
  with several ids, and the later submission as the suggested survivor;
- the whole path, from the live Zite column "Assigned Angel ID Code"
  through `import_table` and `prepare_rows` into the duplicate log;
- the Duplications section — the contest is listed and detailed with
  `kind="angel_claim_conflict"`, the kind filter picks it, the
  neighbours stay inside the filter;
- the resolution — ignoring the loser leaves the survivor the only
  claim, and no decision renumbers a sponsor;
- the id invariants I1 to I4 above.

`frontend/test/duplications.test.ts` covers the kind filter, the kind
badge, and the contest detail page.

## 8. Out of scope

- Changing the matcher (D7).
- Transferring a claim from the loser to the winner (the loser is
  ignored instead).
- Detecting a claim on an angel id that no child has — the matcher drops
  those with a warning today (`match.py:404-415`).
- Reporting a contest between two rows of the same email duplicate group
  (D5).
