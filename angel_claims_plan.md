# Assigned Angel ID claims — implementation plan

Status: implemented. Branch `feature/angel-claims`, off `main`
(`9e14478`), in the main worktree (no separate worktree).

## 1. The problem

A sponsor's **Assigned Angel ID** (`prior_assigned_angel_ids`, the Zite
"Assigned Angel ID Code") names the children the sponsor claims. The
program runs generic Walmart sponsors that hold the angel ids allocated
to a store, and a person who signs up at that store claims the same id
to take the slot over.

Two things were missing:

- the claim was only honoured when the matcher ran, so the audit stage
  never saw the child placed;
- a claim that could not be honoured was dropped with a log line
  (`match.py`: "unknown angel_id {id} dropped"). The sponsor silently got
  nothing and nobody noticed.

## 2. What we build

- **The claim is placed when the data is loaded.** The screening run
  places the claim on the child, so the audit stage sees the child
  already placed and can correct it.
- **A claim that cannot be honoured is flagged.** The sponsor gets a
  clarification note naming the angel id, its status becomes
  `needs_clarification`, and the auditor fixes it on the sponsor's own
  row — the claim field is editable there.

## 3. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | The claim is placed at screen time, when the data is loaded. | The audit stage then shows the claim in place, before the matcher decides anything. |
| D2 | The error is a clarification note on the sponsor row. | The codebase's existing mechanism (`ORPHAN_NOTE` does the same for an orphaned child): the note names the problem, the row's status becomes `needs_clarification`, and the auditor fixes it on the row. |
| D3 | "Not a duplicate" means no other sponsor claims that angel id. | A contested claim is adjudicated by the Duplications section; an uncontested claim that names no child is nobody's to settle but the auditor's. |
| D4 | A contested claim places nothing. | Two sponsors want one child; the Duplications section decides who keeps it. Nothing is assigned on a guess. |
| D5 | A claim never moves an already-placed child. | The claim is a first placement, not a move. The matcher re-applies every claim afterwards, so the claim still wins in the end. |
| D6 | The screen's AI step keeps the child's assignment columns. | They are module-owned (§6.1): the matcher and the manual matcher placed them, and a re-screen must not undo that. The AI rebuild dropped them, which also lost the claim. |

## 4. Backend

### 4.1 `src/ccat/services/db_assignments.py`

`apply_angel_claims(sponsors, children)`:

- groups the non-ignored sponsors' claims by angel id;
- an angel id with one claimant and a child is placed on that child
  (`assigned_sponsor_id`, `assignment_source = "auto"`), unless the
  child is already placed (D5);
- an angel id with two or more claimants places nothing (D4);
- an angel id no child has leaves `ANGEL_CLAIM_NOTE` on the claiming
  sponsor and moves its status to `needs_clarification`.

It returns the angel ids that named no child.

### 4.2 `src/ccat/services/db_pipeline.py`

The screening run reconciles the three stores, applies the claims, and
commits. The reconcile loop computes all three stores before committing
(the rows do not depend on each other), so the claims see the settled
child rows.

### 4.3 `src/ccat/screening.py`

`dense_final_row` now preserves the child's own assignment columns when
the AI rebuilds a row (D6).

## 5. Frontend

The sponsor's `Assigned Angel ID` field is offered in the sponsor editor
(it was hidden), so the auditor can correct a claim the screening run
flagged.

## 6. Tests

`test/test_angel_claims.py` characterizes the rule: an uncontested
claim is placed, a claim naming no child is noted, a contested claim
places nothing and notes nothing, a placed child keeps its placement, an
ignored sponsor claims nothing, an ignored child is not claimed, a claim
list places every child, and the note is appended once.

`test/test_db_pipeline.py` adds the end-to-end case: a screening run
places the claim on the child and leaves the unusable claim's note on
the sponsor.

## 7. Out of scope

- Changing the matcher. It still honours every claim, and a contested
  claim still goes to the lower sponsor id until the Duplications
  section settles it.
- A claim on an angel id another sponsor also claims: the Duplications
  section owns it, so no note is raised — and if no child has that angel
  id, neither the Duplications section nor a note covers it. That gap
  is narrow (two sponsors typing the same wrong id) and is left alone.
- Moving a placed child because a claim names it.
