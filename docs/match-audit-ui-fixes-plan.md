# Match & Audit UI Fixes — Implementation Plan

Branch: `feature/match-audit-ui-fixes` (off `main`)
Status legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked

## Findings

### 1 — Sponsors with an Assigned Angel ID show a free slot (bug)

Live data (`../CCAT/output/ccat.sqlite3`):

| sponsor | name | type | claims | placed |
|---|---|---|---|---|
| 39 | Walmart Sponsor 14 | Walmart | `100A` | 1 (Hazel, 100A) |
| 47 | Peyton Pakkala | Individual | `100A` | 0 |
| 35 | Walmart Sponsor 11 | Walmart | `36A` | 1 (Cashton, 36A) |
| 55 | Kadence Nichols | Individual | `36A` | 0 |

Root cause: a **contested claim** (two sponsors naming the same angel id).
`apply_angel_claims` (`src/ccat/services/db_assignments.py:55-115`) places
nothing when `len(claimants) > 1` (D4: the Duplications section decides —
but sponsor duplicate grouping never surfaces claim contests, so the
contest deadlocks silently). The Walmart placeholders claimed first and
were placed as single claimants; the individuals loaded later, lost the
contest, and show `remaining = num_children - assigned_count = 1` on the
match screen (`db_manual_match.py:773,1234`).

`angel_claims_plan.md` §1 documents the intended workflow: the generic
Walmart sponsor holds the id "and a person who signs up at that store
claims the same id **to take the slot over**." The takeover was never
implemented.

Fix design:

- **Takeover rule**: when a claim is contested, a non-Walmart claimant
  beats a Walmart claimant (module constant; Walmart is the generic
  placeholder type — `Individual`/`Organization` are real people). The
  winner takes the child even if the child is already placed **with the
  Walmart sponsor** (the takeover moves it, `assignment_source="auto"`).
  A child placed with a specific sponsor is never moved. Two specific
  claimants: unchanged, nothing placed (Duplications).
- `apply_angel_claims` — resolve contests by the rule above.
- `match.py` — claim parsing (`:357-447`, first-seen keeps, later
  claimants lose) and the duplicate-claim guard (`:612-656`, raises
  `MatchError`) must resolve Walmart contests the same way so a matcher
  run neither errors nor re-steals the angel; `MatchError` stays for
  specific-vs-specific contests.
- **Live data repair**: an Alembic **data** revision (repo precedent:
  `20261121_0006_manual_assignment`) re-applies the rule to the existing
  DB — move `final_child.assigned_sponsor_id` for contested Walmart
  claims, then recompute sponsor/family assignment summaries
  (`apply_sponsor_summary`/`apply_family_summary` semantics). Run with
  the normal `ccat db-upgrade` path (never hand-edit the live DB).
- Match screen needs no change of its own: once 47/55 hold their
  claimed angels, `assigned_count=1`, `remaining=0`, "full".

### 2 — Matched-state colors are inverted

"full" badge uses `status-ignored` (red): `frontend/src/components/SponsorCardList.vue:361`.
Repo already ships the right vocabulary in `frontend/src/assets/base.css:167-183`:
`check-ok` (green), `check-warn` (yellow), `check-bad` (red). Swap the
matched-state indicators to them: full → `check-ok`, partial/N-slots →
`check-warn`, empty/unassigned → `check-bad`. Only matched-state
indicators; audit-status badges (new/audited/ignored) keep their colors.

### 3 — "Match report (JSON)" link on the Registry page

`frontend/src/views/AuditLandingView.vue:262` (link) + `:47` (URL
builder for `GET /seasons/{season}/diagnostics/match-report`). Remove
the link and the now-dead helper code; keep the API endpoint itself.

### 4 — Family audit: children list lacks sponsor info

Family detail (`frontend/src/views/FamilyDetailView.vue`) lists the
family's children. Child list items already carry `assigned_sponsor_id`
(canonical child-grain column). The list needs: a matched indicator per
child and the sponsor's name hyperlinked to `/audit/sponsors/{id}`.
Sponsor display name is not in the child payload — add it server-side in
the child list extras (`src/ccat/api/routes/audit.py`, child branch).

### 5 — Sponsor audit: no list of assigned children

Sponsor detail (`frontend/src/views/SponsorDetailView.vue`) shows the
sponsor's own columns only. Add an "assigned children" list with the same
information as the family children list minus the sponsor/matched column
(implied by membership). Sponsor detail payload needs a `children`
array (`src/ccat/api/routes/audit.py` `_detail_payload` sponsor branch):
child name, angel id, family id/name (hyperlink to family page).

### 6 — Acquire/Screen pages → Registry cards

- `/acquire` and `/screen` routes + nav entries removed;
  `PipelineCommandView.vue` deleted (its only consumer). Old URLs
  redirect to `/audit`. `PipelineRunForm.vue` is kept — `StatsView.vue`
  reuses it.
- `AuditLandingView.vue` gains an **Acquire** and a **Screen** run card
  above the Children/Families/Sponsors/Duplications nav cards, same
  card grid styling, each embedding `PipelineRunForm command="acquire|screen"`.
- Role-limited to admins/coordinators: cards render only when
  `session.hasRole('admin', 'coordinator')`. The backend already gates
  enqueue on `require_roles("coordinator", "admin")`
  (`src/ccat/api/routes/jobs.py:127`), so the limit is real, not cosmetic.

## Lane plan

Disjoint file scopes, same worktree. The three delegated fixer lanes
stalled without writing (recon-only round-trips), so the orchestrator
implemented every lane directly; findings below were verified against
the code, not subagent reports.

- **Lane A — claim takeover (item 1)**: `ccat/domain` (placeholder
  constant), `db_assignments.py` (`_claim_takeover`),
  `preparation.py` (contest detection skips settled contests),
  `match.py` (`reconcile_pre_assignments` + `_prior_child_assignments`
  resolve the same way), Alembic revision `20261126_0011`, tests
  (`test_angel_claims.py`, `test_sponsor_claims.py`, `test_match.py`,
  `test_db_upgrade.py`).
- **Lane B — audit screens (items 2+4+5)**: `SponsorCardList.vue`
  matched-state badge, `FamilyDetailView.vue` sponsor column,
  `SponsorDetailView.vue` assigned-children card, `routes/audit.py`
  (`assigned_sponsor_name` extra + exact `sponsor_id` filter on
  `list_children`), `api/audit.ts` + openapi/schema regen, tests.
- **Lane C — registry restructure (items 3+6)**: `AuditLandingView.vue`
  (JSON link removed; role-limited Acquire/Screen `PipelineRunForm`
  cards above the nav grid), `router/index.ts` (routes removed, old
  addresses redirect to `/audit`), `App.vue` nav, deleted
  `PipelineCommandView.vue`, tests.

## Verification record

- `pytest`: 989 passed (full suite; 1 pre-existing head-pin assertion
  updated to the new revision). `test_db.py` ty/pre-commit: the 4
  `unresolved-import` diagnostics in `test_db_clarify.py` /
  `test_db_generate.py` fail identically on `main` — pre-existing.
- `npx vitest run`: 288 passed (28 files). `npm run build`: clean.
- `pre-commit run ruff/ruff-format --all-files`: passed after
  ruff-format reformats.
- Alembic `20261126_0011` validated on a copy of the live DB
  (takeover + summaries + idempotent downgrade/re-upgrade), then
  applied to the live DB with `ccat db-upgrade` (backup at
  `output/ccat.sqlite3.bak-claim-takeover-pre`): angel `100A` (Hazel)
  → sponsor 47 (Peyton Pakkala), angel `36A` (Cashton) → sponsor 55
  (Kadence Nichols); Walmart placeholders 39/35 freed back to one open
  slot each.

## Decision worth reviewing

**Contested claims now resolve by specificity instead of deadlocking.**
When a specific sponsor (Individual/Organization) claims an angel a
generic Walmart placeholder also claims, the specific sponsor takes
the child over — including moving a child already placed with the
placeholder; a manual placement or a specific sponsor's child is never
moved. Two specific claimants (or placeholders only) still defer to
the Duplications section. This implements the documented store-signup
workflow (`angel_claims_plan.md` §1: the person "claims the same id to
take the slot over"); the previous behavior (D4: place nothing, wait
for an audit that never surfaced the contest) is what made sponsors
#47/#55 show a free slot. If you would rather keep contests manual,
revert `db_assignments._claim_takeover` + the matcher changes and drop
revision `20261126_0011`'s data pass.

## Tasks

- [x] 0. Plan file created (this file)
- [x] 1. Lane A — claim takeover: contest rule, matcher, Alembic repair, tests
- [x] 2. Lane B — matched-state colors; family children sponsor indicator; sponsor children list
- [x] 3. Lane C — Registry gains Acquire/Screen (role-limited); JSON link removed; routes/nav cleaned
- [x] 4. Reconcile writers; regenerate API types; review full diff
- [x] 5. Full verification: pytest, vitest, frontend build, pre-commit
- [x] 6. Update plan to final state; summary
