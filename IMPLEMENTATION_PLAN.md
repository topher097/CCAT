# Dispatch page — implementation plan

Design document: `dispatch_page_plan.md` (same directory). This file is the
execution plan: phases, files, checks, delegation map.

## Goal

Replace the `/generate` page with a `/dispatch` page that lets a
coordinator generate child info sheets and sponsor packets (in bulk, per
sponsor, or per child), draft each sponsor's packet email from the
existing HTML template, and **send** it from the program's Gmail account
with the packet PDFs attached.

**Only an `admin` may send.** Drafting stays `coordinator` or `admin`;
reading the work list stays open to any authenticated user. The send
button is admin-only in the UI and the send route is admin-only in the
API.

## Current behavior

- `frontend/src/views/GenerateView.vue` starts one `generate` job and
  lists artifacts. Nothing else.
- `src/ccat/services/db_generate.py` renders sheets (py3o + headless
  LibreOffice) and builds packets (Jinja2 draft + pypdf merge) into
  `<artifact_root>/sponsor_packets/<sponsor_id>/`.
- `data/sponsor_packet_email_2026.html` is a **copy-paste** draft: its
  header box tells the coordinator to paste it into a mail client and
  attach the two PDFs by hand.
- There is **no mail transport**: no SMTP, no Gmail API, no send route,
  no attachment code, no sender identity, no delivery record.
- `POST /seasons/{season}/manual-match/dispatch` stamps
  `final_sponsor.packets_dispatched_at` (filled-slot precondition). Only
  the manual matcher reads that column afterwards.

## Target behavior

1. `/dispatch` lists every eligible sponsor with its sheet/packet/email
   state and its blockers.
2. Generation: bulk sheets, bulk packets, one sponsor, one child — all
   through the existing `generate` job with two new options.
3. A popup drafts one sponsor's email from the template, shows the two
   attachments, and lets an admin send it. The send is recorded as an
   append-only attempt; a confirmed success stamps the sponsor as
   dispatched.

## Scope and constraints

- No new dependency. Gmail goes through `google-api-python-client`, which
  is already installed; SMTP stays a documented alternative, unbuilt.
- The email template stays authoritative: the popup edits the **subject**
  and a plain-text **note**. No raw-HTML editing.
- One send per sponsor per attempt; no bulk send button.
- `packets_dispatched_at` keeps its meaning ("the packet went out"). It
  freezes only the manual matcher, as it does today.
- The clarification email flow stays unsent.
- The existing `generate` job keeps its current behavior by default
  (`only="both"`).

## Baseline

Captured on `main` (a4f1de6) in the worktree before any change:

```sh
.venv/bin/python -m pytest -q            # expected: all green
cd frontend && npx vitest run             # expected: all green
.venv/bin/python scripts/export_openapi.py && git diff --stat frontend/openapi.json
                                   # expected: empty diff (contract is current)
```

Any failure that also fails on `main` is recorded against the baseline
and does not block a phase.

## Data shape

Three new structures, all in the domain's own vocabulary (no scattered
booleans):

**`sponsor_drafts`** — one row per `(season_id, sponsor_id)`: the current
editable draft. `version` for optimistic concurrency; `subject`, `note`,
`body_html` (generated), `to_email`, `bcc_emails`, `attachments`
(json, with sha256), `build_id` (the packet build it was rendered from).
No status column — the attempt log owns the state.

**`sponsor_send_attempts`** — one row per send attempt. A small state
machine:

```
        ┌──────────► sent     (provider accepted; sponsor stamped)
sending ┼──────────► failed   (provider refused; retryable)
        └──────────► unknown  (ambiguous; never auto-retried)
```

`state`, `message_id` (RFC 5322, generated before the send),
`provider_message_id`, the content snapshot (subject, body, recipients,
attachment hashes), `actor`, `user_id`, timestamps, `error`. A partial
unique index on `(season_id, sponsor_id) WHERE state='sending'` makes
"one send in flight per sponsor" a database rule.

**`manifest.json`** — written beside the packet files: `build_id`,
`sponsor_id`, `season`, `subject`, the sponsor greeting, the angel list
(with gender, age, drop-off), the files with their sha256, and a
fingerprint over the assignment and the source sheets. The draft body is
rendered from the manifest, so the email and the attached PDF cannot
describe different children.

## Phases

Each phase ends green and is independently reviewable.

### P1 — Storage

**Outcome:** the three structures exist in the database and the
repository can write them.
**Files:** `src/ccat/db/models.py`, `src/ccat/db/repositories.py`,
`alembic/versions/<rev>_sponsor_dispatch.py`, `src/ccat/services/ports.py`
(the `MailTransport` protocol), `src/ccat/services/results.py`.
**Depends on:** nothing.
**Unit:** models + one Alembic revision + repository methods
(`save_draft`, `latest_draft`, `commit_dispatch`, `attempt_in_flight`).
**Check:** `.venv/bin/python -m pytest -q test/test_db_repo.py` and
`alembic upgrade head` against a scratch database.
**Stop:** if the migration cannot apply to a copy of the live schema,
stop and report.

### P2 — Mail transport

**Outcome:** the app can send one message with attachments, and can say
whether it was accepted, refused, or is unknown.
**Files:** `src/ccat/mail.py` (new), `src/ccat/config.py` (the
`dispatch` spec), `src/ccat/__init__.py` (the `ccat mail-authorize`
command), `.gitignore`.
**Depends on:** P1 (the protocol lives in `ports.py`).
**Unit:** the Gmail transport behind `MailTransport`, the three outcomes,
timeouts, and the operator consent command.
**Check:** `.venv/bin/python -m pytest -q test/test_mail.py`.
**Stop:** if the Gmail API call shape cannot be verified without a live
account, ship the transport behind the protocol with the fake as the
only proof and say so plainly in the report.

### P3 — Packet manifest

**Outcome:** every packet build writes `manifest.json` and registers the
artifacts with roles; a packet without a manifest is rebuilt.
**Files:** `src/ccat/services/db_generate.py`, `src/ccat/sponsor_packets.py`
(if needed), `data/sponsor_packet_email_2026.html` (the copy-paste box
becomes conditional), `src/ccat/db/artifacts.py` (if the role needs a
home).
**Depends on:** P1.
**Unit:** write the manifest beside the packet files, register the
artifacts with `meta={"sponsor_id", "role", "build_id", "sha256"}`, and
change the skip predicate to require a complete manifest.
**Check:** `.venv/bin/python -m pytest -q test/test_sponsor_packets.py
test/test_db_generate.py`.
**Stop:** if the manifest cannot carry the template's variables, stop
and report which variable is missing.

### P4 — Dispatch service and API

**Outcome:** the six endpoints exist and behave as specified.
**Files:** `src/ccat/services/db_dispatch.py` (new),
`src/ccat/api/routes/dispatch.py` (new), `src/ccat/api/dto.py`,
`src/ccat/api/__init__.py`, `src/ccat/db/repositories.py` (the
`"dispatch"` write-rights entries and `commit_dispatch`).
**Depends on:** P1, P2, P3.
**Unit:** list rows → list children → draft → edit → send, in that order.
**Check:** `.venv/bin/python -m pytest -q test/test_db_dispatch.py
test/test_api.py`.
**Stop:** if the send cannot be made atomic against the attempt table,
stop and report; do not ship a best-effort send.

### P5 — Generation options

**Outcome:** `only` and `child` scope the `generate` job; a
packets-only run skips sponsors it cannot build.
**Files:** `src/ccat/services/db_generate.py`, `src/ccat/services/results.py`.
**Depends on:** P3.
**Unit:** `only` first, then `child`, then the packets-only skip.
**Check:** `.venv/bin/python -m pytest -q test/test_db_generate.py`.
**Stop:** n/a.

### P6 — API contract

**Outcome:** `frontend/openapi.json` and `frontend/src/api/schema.d.ts`
describe the new endpoints.
**Files:** `frontend/openapi.json`, `frontend/src/api/schema.d.ts`
(generated).
**Depends on:** P4, P5.
**Unit:** the two commands.
**Check:** `git diff --stat frontend/openapi.json frontend/src/api/schema.d.ts`
shows the new routes; `.venv/bin/python -m pytest -q test/test_api.py`
still green.
**Stop:** n/a.

### P7 — The page

**Outcome:** `/dispatch` lists sponsors and starts generation.
**Files:** `frontend/src/views/DispatchView.vue` (new),
`frontend/src/api/dispatch.ts` (new),
`frontend/src/components/DispatchGenerationBar.vue` (new),
`frontend/src/components/DispatchSponsorTable.vue` (new),
`frontend/src/utils/sponsor.ts` (new),
`frontend/src/router/index.ts`, `frontend/src/App.vue`,
`frontend/src/views/HomeView.vue`.
**Depends on:** P6.
**Unit:** route and nav first (the page compiles), then the work list,
then the generation bar.
**Check:** `cd frontend && npx vitest run`.
**Stop:** n/a.

### P8 — The email popup

**Outcome:** an admin can draft, review and send one sponsor's email
from the popup, and see the outcome.
**Files:** `frontend/src/components/SponsorEmailModal.vue` (new),
`frontend/src/views/DispatchView.vue`, `frontend/src/api/dispatch.ts`.
**Depends on:** P7.
**Unit:** open and preview, then edit subject/note, then send and the
three outcomes.
**Check:** `cd frontend && npx vitest run`.
**Stop:** n/a.

### P9 — Documentation and full verification

**Outcome:** the docs describe what shipped, and the whole suite is green.
**Files:** `pipeline_architecture.md`, `docs/limitations.md`,
`docs/deployment.md`, `README.md`, `docker-compose.yml`,
`config/ccat_2026.json`, `config/ccat_docker.json`.
**Depends on:** P1-P8.
**Unit:** docs per file, then the full check.
**Check:** `pre-commit run ruff-format --all-files && pre-commit run ruff
--all-files && pre-commit run ty --all-files && .venv/bin/python -m
pytest -q && cd frontend && npx vitest run`.
**Stop:** n/a.

## Delegation map

| Lane | Scope (exclusive) | Runs after |
|---|---|---|
| Lead | P1, P2, P3, P4, P5, P6 — the backend core and the contract | — |
| Lane A | `test/test_mail.py`, `test/test_db_dispatch.py` — the new backend tests | P4 |
| Lane B | `frontend/src/**`, `frontend/test/dispatch.test.ts` — the page and the popup | P6 |
| Lane C | `docs/**`, `README.md`, `pipeline_architecture.md` | P8 |

Disjoint writable targets: Lane A writes only `test/`, Lane B only
`frontend/`, Lane C only docs. The lead owns `src/ccat/**`, the Alembic
revision, the config, and the generated contract. No two lanes write the
same file.

Each lane gets the design document, the exact behavior contract, and its
verification command. A lane reports the files it wrote and the output
of its check; the lead re-runs every check.

## Authority

Local work only: code, tests, docs, commits on `feature/dispatch-page`.
No push, no pull request, no deployment, and **no real email**. The Gmail
transport is exercised against a fake in the tests; a live send needs the
operator consent from `ccat mail-authorize` and an account that may send
as the program's From address, and that is a human step.

## Completion checklist

- [ ] `pre-commit run ruff-format --all-files`, `ruff`, `ty` clean.
- [ ] `.venv/bin/python -m pytest -q` green.
- [ ] `cd frontend && npx vitest run` green.
- [ ] `frontend/openapi.json` and `schema.d.ts` regenerated and committed.
- [ ] `/dispatch` reachable; `/generate` redirects; the send button is
      admin-only in the UI and the send route is admin-only in the API.
- [ ] The docs match the code.
- [ ] Every phase committed separately, in phase order.
