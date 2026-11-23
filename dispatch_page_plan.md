# Dispatch page — implementation plan

Status: plan only. No code written yet.

Replaces the `/generate` page with a `/dispatch` page that owns the last
mile of the program: generate the child information sheets and the sponsor
packet, draft the sponsor email from the existing HTML template, and send
it from the program's Gmail account with the packet PDFs attached.

---

## 1. Summary

The `/generate` page today starts one pipeline job and lists the files it
produced. It stops there: the coordinator still has to open the packet
directory, read the drafted email, copy it into a mail client, attach two
PDFs by hand, and then mark the sponsor as dispatched somewhere else.

The new `/dispatch` page closes that gap. It replaces `/generate` and
becomes the last mile of the program:

- **Generate** child info sheets, sponsor packets, or both — in bulk, or
  for one sponsor, or for one child.
- **See** every sponsor that still needs to go out, and exactly why any
  sponsor is blocked (no email, no packet, missing sheets, quota not
  filled, already sent).
- **Draft** the sponsor email from the existing HTML template, edit the
  subject and add a note, and see exactly which files will be attached.
- **Send** it from the program's Gmail account with the packet PDFs
  attached, from a popup, one sponsor at a time, with the result
  recorded.

The heavy work (PDF rendering) stays in the durable job worker. The new
work — storing a draft, sending one mail, stamping the sponsor — is
small, explicit and auditable.

It is not the first time this system transmits a child's details: the
screening prompt, the clarification prompt and the ZeroBounce checks
already do that, and their approval gates are recorded in
`docs/limitations.md`. What is new is that this is the first time a
child's information sheet is **mailed to a third party**. That makes the
send path a new trust boundary (D10, §8) rather than a feature, and it
is why the plan treats a send as an auditable attempt (D17) rather than
a boolean.

Three facts from the review shape the whole plan:

1. **There is no mail transport in this repository.** No SMTP, no Gmail
   API, no send endpoint, no attachment code, no sender identity, no
   delivery record. `generate_email` is an LLM prose generator, and the
   `send` argument in `clarify/email_gen.py` sends a *prompt to a model*,
   not a mail. Everything the send path needs is new (D8, §5.3).
2. **The Google OAuth token is Drive-scoped and expired.** Gmail needs
   its own scope and its own token, or the Drive integration breaks
   (D8, §8.2).
3. **"Dispatched" already means something** — `packets_dispatched_at`
   records that the packets went out, and the manual matcher then refuses
   to move that sponsor's children. The page reuses that meaning rather
   than inventing a second one (D3).

The plan keeps the existing contracts intact: generation stays in the
`generate` job, packets keep their layout, the sponsor email template
stays authoritative, the clarify flow stays unsent, and `docs/
limitations.md`'s "no automatic email sending" decision is deliberately
reversed for sponsor packets only — and rewritten, not reinterpreted
(§13).

---

## 2. Scope

### In scope

- A new `/dispatch` route (replacing `/generate`) with:
  - bulk generation of child info sheets,
  - bulk generation of sponsor packets,
  - generation for one selected sponsor or one selected child,
  - a per-sponsor work list with generation state, packet state and dispatch
    state.
- Email drafting: render the existing sponsor packet template into a
  preview, edit the subject and add a plain-text note in a popup, and see
  the packet PDFs that will be attached.
- Sending: deliver the drafted email from the Google/Gmail account with the
  attachments, then stamp the sponsor row as dispatched.
- Audit trail of what was sent, when, to whom, with which attachments.

### Out of scope (explicitly not built)

- A sponsor-facing portal or self-service accounts (unchanged from
  `docs/limitations.md`).
- Bulk auto-send without a per-sponsor human confirmation: every send is an
  explicit action in the popup. There is no "send all" button — a hundred
  emails with one click is not a feature this program needs.
- Sending the **clarification** emails. That flow stays draft-only, and
  the existing guard in `test/test_api.py` keeps it that way.
- Editing the packet PDF or the info sheet content from the dispatch page
  (that stays in the audit tool and the templates).
- Retyping the child information sheet template; the `.ott` template and
  the sponsor email template stay as the source of truth.
- A bounce handler or a delivery report from Gmail (the message id is
  recorded, so a bounce can be chased by hand).
- Any change to the eligibility rules in `pipeline_architecture.md` §13/§13b
  for the existing paths: the combined run and the per-sponsor path keep
  today's strict validation. The one addition is the packets-only mode
  (D14), which skips a sponsor it cannot build instead of failing the
  whole job.
- Any change to the packet directory layout, or to the Drive
  integration.
- Fixing the per-sheet full-store rewrite in `db_generate.py` (a real
  cost, a separate change).

---

## 3. The current system, condensed

### 3.1 The generate page today

`frontend/src/views/GenerateView.vue` (186 lines, no scoped CSS) does three
things and nothing else:

1. `PipelineRunForm command="generate"` — the only action. It collects
   `test` / `overwrite` / `sponsor` / `max_workers`, confirms in a
   `ConfirmDialog`, then `POST /api/v1/seasons/{season}/jobs {job_type:"generate"}`
   and redirects to `/jobs/:id`.
2. An artifact table fed by `GET /api/v1/seasons/{season}/artifacts?kind=`
   with a `kind` filter (`all` / `child_sheet` / `sponsor_packet`).
   Downloads are `<a href>` to `artifactDownloadUrl(id)`.
3. A "child generation state" table fed by
   `GET /api/v1/seasons/{season}/children?page_size=200`, showing
   `generated_info_sheet` as a badge.

Progress and failures are **not** on this page; they live on
`/jobs/:id` (SSE plus a 2 s poll). Route registration is
`frontend/src/router/index.ts:148-153`; the nav entry is
`frontend/src/App.vue:21`; `HomeView.vue:207-221` links to it too.

There is no per-sponsor view of the page: `sponsor` is only a CLI-style
option on the run form, and an **unscoped** run covers every pending
eligible child (`db_generate.py:896-902`); a run with `sponsor` set is
scoped to that sponsor's children, its validation and its packet
(`db_generate.py:1033-1042`).

### 3.2 Info sheet generation (domain contract)

`pipeline_architecture.md` §13 is the behavioral contract and it still
holds:

- Input: eligible `final_child` rows (non-ignored, non-orphan) whose
  `status == "audited"`, whose **own** `assigned_sponsor_id` is not null,
  and whose `generated_info_sheet` is false. The child's row is the
  canonical assignment: `FAMILY_JOIN_COLUMNS` deliberately carries no
  sponsor column (`child_info_generate.py:101-107`), so the child's
  `assigned_sponsor_id` is the one that counts. `--overwrite` ignores the flag.
- Render: `data/child_information_sheet_2026.ott` via `py3o`, converted by
  headless LibreOffice (`soffice --convert-to pdf`) with an isolated
  `-env:UserInstallation` profile per worker.
- Output: `output/child_info_sheets/<location>/AT<season>_<angel_id>.pdf`,
  where `<location>` is the family's pickup location bucketed through
  `config.location_groups` (`hancock` / `calumet` / `unknown`).
  Overwriting is idempotent; generation is what writes the marker.
- Parallelism: `info_sheet.max_workers` (default 4), clamped to the pending
  row count; each worker gets its own scratch dir and LibreOffice profile.
- Failure: per-row failures are logged and skipped; `InfoSheetError`
  reports the failed angel ids and a rerun picks up exactly those rows.
- Flag: `generated_info_sheet` is committed **per item** after the PDF is
  atomically renamed into place (`pipeline_architecture.md` A.4).

### 3.3 Sponsor packet generation (domain contract)

`src/ccat/sponsor_packets.py`, run as the packet phase of `ccat generate`.
For every output-eligible sponsor with assigned angels it writes
`output/sponsor_packets/<sponsor_id>/` containing:

- `sponsor_packet_<sponsor_id>.html` — the email draft, rendered from the
  Jinja2 template `data/sponsor_packet_email_2026.html` with `subject`,
  `sponsor`, `bcc_emails`, `season`, `instructions_filename`,
  `child_information_filename`, `angel_count`, `angels[]`.
- `Sponsor <sponsor_id> - Child Information <season>.pdf` — the sponsor's
  info sheets merged into one PDF, ordered by family id then natural angel
  letter, with outline bookmarks per angel.
- a copy of `Sponsor Instruction 2026.pdf`.

Reruns skip a sponsor whose packet files already exist; `--overwrite`
regenerates. A missing info sheet contributes no pages **in the merge
helper's fallback** (`db_generate.py:655-668`), but that is not what a
normal run does: ordinary packet generation validates the sheets first
and aborts when one is missing (`db_generate.py:1032-1035`).
Packet membership uses eligible finals independently — the family and
child filters do not cascade.

### 3.4 "Dispatch" already has a meaning in this codebase

`packets_dispatched_at` lives on the sponsor row
(`src/ccat/db/models.py:295`) and is stamped by the manual-match module
when a sponsor is dispatched (`src/ccat/services/db_manual_match.py:1661-1678`):

- the sponsor must have every child slot filled (`num_children`),
- the manual matcher refuses to `assign` or `release` a sponsor's
  children afterwards (`db_manual_match.py:1320-1342`). The automatic
  matcher and the audit paths do not read the column, so the stamp is
  not a general freeze (D3),
- `POST /api/v1/seasons/{season}/manual-match/dispatch` returns `409`
  with code `conflict` when the stamp is already present (the
  `packets_dispatched` code belongs to the assignment guards).

The frontend already surfaces this: `components/SponsorCardList.vue`
computes a `dispatchBlock()` reason (already dispatched / quota not filled /
slot still free) and renders a `dispatch-packets` button. The new page
must reuse this meaning rather than invent a second one.

### 3.5 Email today

- `data/sponsor_packet_email_2026.html` is a **copy-paste draft**: its
  header box tells the coordinator to copy the body into their mail client,
  put the sponsor in To, the program addresses in BCC, and attach the two
  PDFs by hand.
- The clarification email flow (`src/ccat/services/db_clarify.py`) also
  only *drafts*. `docs/limitations.md` lists "No automatic email sending.
  Clarification drafts are generated and edited, never sent" as a
  deliberate scope decision of the 2026-11 refactor.
- There is no endpoint anywhere that returns rendered email HTML for
  preview, and no endpoint that sends mail.
- The repo root holds `ccat_google_credentials.json` and
  `ccat_google_token.json` (both gitignored), which are Google OAuth
  client credentials plus a token — not yet wired to a mail transport.

So the dispatch page is the first feature in this system that both
*composes* and *sends* mail. It reverses a documented decision, and the
plan has to treat the send path as a new trust boundary (see §8).

### 3.6 Two live generation paths

| Path | Entry | Orchestrator | Where artifacts land |
|---|---|---|---|
| API / job (current) | `POST /api/v1/seasons/{season}/jobs` with `job_type="generate"` → `jobs.handlers.generate` → `services/db_generate.generate` | durable `Worker` (`src/ccat/jobs/worker.py`) | `storage.artifact_root` (`output/artifacts`) + rows in the `artifacts` table |
| CLI (legacy workbook) | `ccat generate` → `child_info_generate.run` | thread pool in-process | `info_sheet.output_dir` / `sponsor_packets.output_dir` (`output/child_info_sheets`, `output/sponsor_packets`) |

The DB service reuses the legacy pure helpers by import (`_nested_data`,
`_pending_rows`, `_render_one`, `_sheet_stem`, `FAMILY_JOIN_COLUMNS`,
`SPONSOR_JOIN_COLUMNS`, `_angel_sort_key`) and reimplements only the
file-placing, the DB marker write and the packet writing
(`src/ccat/services/db_generate.py:58-79`).

`db_generate.generate(cfg, actor, options, progress, cancel)` takes
`options = {test, overwrite, sponsor, max_workers}` (max 16) and returns

```python
GenerateResult(
    sheets_generated=[...], sheets_failed=[...], sheets_skipped=[...],
    packets_built=int, packets_skipped=int,
    info_sheet_dir=str, packets_dir=str)
```

`src/ccat/services/results.py:112`. It is a `StrictResult`
(`extra="forbid"`), so any new field needs a model change.

Sheet phase details that matter to the dispatch page:

- `_pending_rows` gate: `status == "audited"` **and**
  `assigned_sponsor_id is not null` **and** `generated_info_sheet` false
  (unless `overwrite`).
- Render → convert → validate (`pypdf`, ≥1 page) → `os.replace` into
  `<artifact_root>/child_sheets/<location>/AT<season>_<angel_id>.pdf`; the
  rename is atomic because the scratch dir lives on the same filesystem.
- After each sheet: `register_file(repo, "child_sheet", ...)` and
  `commit_finals("generate", "child", ...)` sets `generated_info_sheet=True`.
- Packet phase (skipped on `test`): `_validate_sheets_exist` →
  `_sponsors_with_angels` → `_build_packets` writes
  `<artifact_root>/sponsor_packets/<sponsor_id>/` and registers artifacts of
  kind `sponsor_packet`, `sponsor_instructions`, and — on `overwrite` —
  `sponsor_packet_manual` (the previous HTML, preserved before it is
  replaced).
- Failure shape: per-sheet failures are recorded in `sheets_failed` and
  logged, and the job still **succeeds**; only hard errors (`InfoSheetError`,
  `SponsorPacketError`) fail the job. But `_validate_sheets_exist` checks
  *all* eligible sponsored children, so one failed sheet aborts the whole
  packet phase and the job ends `failed` after a partially successful sheet
  phase.

Artifact registration is `src/ccat/db/artifacts.py`: `register_file(...)`
hashes the file, relocates it under `<artifact_root>/<kind>/` when it lives
outside, and inserts an `artifacts` row
(`id, season_id, kind, path, filename, content_type, size_bytes, sha256,
meta, created_by, created_at`). `resolve_download` refuses symlink escapes.
Downloads are audited for the sensitive kinds (`child_sheet`,
`sponsor_packet`, `clarify_draft`, `clarify_draft_fragment`,
`csv_export`, `stats_report` — `api/routes/artifacts.py:22-30`).

### 3.7 Jobs

`src/ccat/jobs/registry.py`: `JOB_TYPES = {acquire, screen, match,
generate, stats, clarify_generate, clarify_export, reconcile}`;
`PIPELINE_CLASS = {acquire, screen, match, generate, stats,
clarify_generate}` — one active pipeline-class job per season, so a second
`generate` enqueue returns `409` with the active job id.

States: `queued → running → succeeded | failed | cancelled`, plus the
transient `cancel_requested` and a requeue-to-`queued` backoff for
retryable errors. `InfoSheetError` / `SponsorPacketError` are **not**
retryable (`jobs/handlers.py:207-217`); `ZiteError`, transport errors,
`ConnectionError` and `TimeoutError` are.

`JobContext.progress(phase, completed, failed, total)` is throttled
(`worker.progress_throttle_seconds`, default 1 s) and written to
`jobs.progress` plus a `job_events` row of kind `progress`.
`cancel_check()` raises `JobCancelRequested`; the sheet phase drains the
pool, keeps the already-committed items and re-raises.

### 3.8 The existing "dispatch" endpoint

`POST /api/v1/seasons/{season}/manual-match/dispatch`
(`src/ccat/api/routes/manual_match.py:363-372`) takes
`ManualDispatchRequest{sponsor_id, expected_versions}` and:

- refuses if a `match` job is running,
- refuses with `409` (`conflict`) if `packets_dispatched_at` is set — the
  `packets_dispatched` code is what the *assignment* guards raise
  (`db_manual_match.py:1336`), not what this endpoint raises,
- refuses unless the assigned count equals `num_children` ("all child slots
  must be filled before dispatch"),
- then writes `packets_dispatched_at = now(UTC)` and a `manual_dispatch`
  security-audit event.

After the stamp, the manual matcher's `assign` and `release` on that
sponsor's children are refused (`db_manual_match.py:1320-1342`). The
automatic matcher and the audit paths do not read the column, so the
stamp is not a general freeze (D3).
`packets_dispatched_at` is owned by the `manual_match` module
(`db/repositories.py:184-191`); `packets_dispatched` is a derived bool
(`api/dto.py:739-740`), there is no column of that name.

Roles: `read_only | auditor | coordinator | admin`. Job enqueue and
dispatch require `coordinator`/`admin`; entity edits require `auditor`.

---

## 4. Design decisions

### D1 — The dispatch page owns the last mile, the job owns the heavy work

Generation stays in the durable worker (`job_type="generate"`); the page
only starts jobs and reads state. Nothing on the page runs a PDF render or
a packet build in an HTTP request.

### D2 — Scope lives in the `generate` job options, not in a new job type

`JobCreate.job_type` is a `Literal` (`api/dto.py:406-415`) and the frontend
mirrors it in `api/jobs.ts` (`JOB_TYPES`). Adding a job type means touching
the DTO literal, the registry, the handler map, the frontend tuple, the
OpenAPI export and `schema.d.ts` — for work the existing `generate` job
already does.

Instead the `generate` job gains two options:

- `only: "sheets" | "packets" | "both"` (default `"both"`, i.e. today's
  behavior), and
- `child: str | None` — a single child's **`zite_record_id`**, which
  implies `only="sheets"`. A bare angel id is accepted only when it
  resolves to exactly one eligible row (§5.7).

Existing `options.sponsor` already scopes to one sponsor. With `only` the
coordinator can express every action the page offers:

| Action | Job options |
|---|---|
| All pending info sheets | `{only: "sheets"}` |
| All sponsor packets | `{only: "packets"}` |
| One child's info sheet | `{child: "<zite_record_id>"}` (implies `only="sheets"`) |
| One sponsor's packet | `{sponsor: 12, only: "packets"}` |
| One sponsor, sheets and packet | `{sponsor: 12}` |

### D3 — A sponsor is "dispatched" when its email is sent, not when its packet is built

`packets_dispatched_at` is set when the packets go out
(`db_manual_match.py:1630-1699`). What the code enforces today is
narrower than the name suggests: **only the manual matcher's `assign` and
`release` refuse to touch a dispatched sponsor**
(`db_manual_match.py:1320-1342`). The automatic matcher does not read the
column, and the audit paths do not either.

So the plan does not promise a general freeze. It promises one answer to
one question — "did this sponsor's packet go out?" — set on a successful
send (D17), plus the freeze that already exists in the manual matcher.

What happens when the **automatic** matcher runs after a send is an open
owner decision (§14.8): either the automatic matcher learns to skip
dispatched sponsors, or the coordinator accepts that a late automatic run
can move a child whose packet was already emailed. Until that is decided,
the page says "Dispatched", never "Locked", and the popup says "Sending
marks this sponsor as dispatched" — not "locks the assignment".

Building a packet must **not** stamp it: `db_generate` does not, and the
two phases stay separate.

The write is done by the dispatch module, so the sponsor write-rights map
(`db/repositories.py:184-191`) gains a `"dispatch"` owner for
`packets_dispatched_at` next to `"manual_match"`, and
`WRITABLE_ENTITIES` gains `"dispatch": frozenset({"sponsor"})` so the
module may write sponsors at all. The existing manual-match endpoint keeps
its own ownership and its own guards.

A sponsor who receives a **physical** packet never gets an email.
`final_sponsor.physical_packet` already marks those sponsors, and for
them the page calls the **existing**
`POST /manual-match/dispatch` endpoint, which carries the filled-slot
guard, the manual-match assign/release guard, the optimistic version
check and the audit event. It needs the sponsor's `row_version`, so the
dispatch row carries `sponsor_row_version` and the page submits it.
The new code does not reimplement that stamp, and there is no second
"dispatched" meaning.

### D4 — Sending is one request per sponsor, not a job

A single email is one API call plus one attachment read. The popup needs an
immediate success/failure, and the job machinery (throttled progress, SSE,
cancellation, retry budget) buys nothing for one recipient. Sending is a
synchronous `POST` with an explicit result.

Bulk work that *is* slow — rendering hundreds of PDFs — stays in the job.

### D5 — Drafts are stored rows, not files

Today the sponsor draft is only an HTML file in the packet directory and
the artifact registry stores it with no usable metadata — an empty
mapping becomes `NULL` (`db/artifacts.py:135`,
`db/repositories.py:2013`) — so there is no sponsor id, no recipient and
no subject. A popup that edits a draft and later sends it needs
a row it can version, address and audit.

**Table `sponsor_drafts`** — one row per `(season_id, sponsor_id)`, the
current editable draft:

```
season_id, sponsor_id        composite primary key
version                      int, optimistic concurrency (If-Match)
subject                      text
body_html                    text   (the exact body to send, generated from the template)
note                         text   (plain-text addition, inserted before the sign-off)
to_email                     text   (sponsor email at draft time)
bcc_emails                   json   (list[str], copied from the config at draft time)
attachments                  json   (list[{artifact_id, role, filename, content_type, size_bytes, sha256}])
build_id                     text   (the packet build this draft was rendered from, D16)
created_by / created_at / edited_by / edited_at
```

**Table `sponsor_send_attempts`** — one row per send attempt. This is the
record of what left the building. The **history** is append-only by
attempt id: no attempt is deleted and no attempt's content snapshot is
rewritten. The **lifecycle fields** (`state`, `finished_at`, `error`)
transition in place, because an attempt is `sending` and then becomes
`sent`, `failed` or `unknown`.

```
id                           text, uuid (the attempt id)
season_id, sponsor_id        composite primary key with id
draft_version                int    (the draft version that was sent)
state                        text   ("sending" | "sent" | "failed" | "unknown")
message_id                   text   (RFC 5322 Message-ID, generated before the send)
provider_message_id          text   (Gmail's id, null until known)
subject                      text   (snapshot)
body_html                    text   (snapshot)
to_email, bcc_emails         text / json (snapshot)
attachments                  json   (snapshot, with sha256)
actor, user_id               text / int
started_at, finished_at
error                        text   (short, sanitized)
```

A partial unique index on `(season_id, sponsor_id) WHERE state =
'sending'` makes "one send in flight per sponsor" a database rule rather
than an application convention.

The draft row carries **no** status: `email_status`, `email_sent_at` and
`email_error` are derived from the latest attempt, so there is no second
source of truth to keep in step.

The separation matters. Editing a draft after a send must not change the
record of what was sent; a retry must not overwrite the first attempt;
and a failed attempt is as much a part of the audit trail as a sent one.
`clarification_drafts` already appends a row per version
(`db/repositories.py:2164-2221`); the attempt log follows that precedent
rather than inventing a new one.

### D6 — The template stays authoritative; the body is not free-form HTML

The email body comes from `data/sponsor_packet_email_2026.html` rendered by
the same `jinja2.Environment(autoescape=True).from_string(...)` call the
packet builder uses. Two changes to the template:

- the "copy this into your mail client" header box is dropped when the
  draft is rendered for sending (it is instructions for the old manual
  flow, and it must not go out to a sponsor),
- the sign-off is preceded by an optional coordinator note.

The coordinator edits the **subject** and the **note**, and nothing else.
The BCC list comes from the config and is not editable in the popup. The
angel table, the file names and the greeting stay generated, because a
hand-typed angel table is where wrong children get emailed, and a
hand-edited body could disagree with the attachments it ships with.

There is no raw-HTML editing. A plain-text note covers the real need
(drop-off hours, a phone number, a change of plan) without opening a
hole where the body and the attached PDF can drift apart. If a season
needs more, that is an open question (§14).

### D7 — The attachment list is derived from the packet, not typed by hand

Attachments are the two files the packet builder already produces:
`Sponsor <id> - Child Information <season>.pdf` (the merged child sheets)
and the sponsor instruction PDF. The draft stores their artifact ids and
file names; the send path resolves them through the artifact registry
(`db/artifacts.py:resolve_download` already refuses symlink escapes) and
reads them off disk.

### D8 — Gmail API with a separate token, not SMTP

Both are possible; the repo already has a Google OAuth desktop client and
`google-api-python-client` as a dependency.

- **Gmail API** (`users.messages.send`): reuses the repo's Google auth
  pattern, returns a message id for the audit trail, no new dependency.
  Cost: the cached token is Drive-scoped
  (`scopes: [".../auth/drive"]`) and expired, so Gmail needs a **separate
  token file and a separate scope set** — and `gdrive.py` hardcodes both
  the credential path and `SCOPES`, so the two clients must not share a
  token or the Drive flow breaks.
- **SMTP** (`smtplib.SMTP_SSL` to `smtp.gmail.com:465`): stdlib only, no
  scope work, but it needs an app password, which requires 2FA on the
  account, and it yields no message id.

Decision: **Gmail API**, with the `gmail.send` scope and the same
`InstalledAppFlow` + refresh-token pattern as `gdrive.py`. The existing
OAuth **client** file is reused — a second client would change the
project, the consent screen and the verification state for nothing — and
only the **token** is separate (`ccat_google_gmail_token.json`), so the
Drive integration is untouched.

One consent with the new scope is required, and it is an operator step
(`ccat mail-authorize`), not a code problem. If the deployment host
cannot complete a desktop OAuth consent, the fallback is SMTP with an app
password — the transport sits behind a small protocol, so the swap is
one implementation.

The Gmail verification rules are not a detail: an external app left in
"Testing" status gets seven-day refresh tokens, and the **authorizing
Google account** is shown an "unverified app" warning on the consent
screen. Sponsors see the program address, not the OAuth app, but the
account that is sending is the one whose reputation is at stake. That is
an owner decision (§14.2).

The sender identity is configuration, never inferred from the Google
account: `dispatch.sender_email`, `dispatch.sender_name`,
`dispatch.reply_to`. There is no From address anywhere in the system
today.

### D9 — The BCC list is the existing program address list

`sponsor_packets.bcc_emails` (three program addresses in
`config/ccat_2026.json`) is what the template already prints. The send
path uses it as the BCC and the popup shows it read-only: the archival
copy of every sponsor mail is a program rule, not a per-message choice.
The sponsor never sees the BCC list.

### D10 — Sending is a new trust boundary, so it gets the strictest treatment

Sending transmits a child's wish list, sizes, interests and notes to a
third party. The rules that follow from that:

- `coordinator` or `admin` only, plus CSRF, exactly like the existing
  dispatch endpoint (`routes/manual_match.py:372`).
- Every attempt writes a `security_audit_events` row **and** a
  `sponsor_send_attempts` row, including the attempts that failed or
  ended unknown — a failed attempt still read the child's details — and
  including the ones the recovery sweep turns into `unknown`. The record carries: the actor's username **and** user id, the
  sponsor, the attempt id, the **To address and every BCC address**, the
  message id, the provider message id, the attachment names **and their
  sha256 hashes**, the outcome, and the timestamp. Filenames alone do not
  identify what was sent.
- The attempt row is committed before the transport call (D17), so a
  crash cannot produce a silent double-send, and an attempt that may or
  may not have been delivered is never presented as a failure.
- No body, no address and no attachment content is ever written to a log
  line. The request middleware already logs only method, path, status,
  user and roles (`api/__init__.py:169-209`); the send path keeps it that
  way, and provider errors are sanitized **structurally** — the stored
  error is a category and a short safe phrase, never the provider's
  message copied through, because a provider error can echo the
  recipient address or part of the body.
- The artifact download audit is not the egress record: `resolve_download`
  does not write an `artifact_download` event (only the HTTP route
  does), and a send is not a download. The attempt log is the egress
  record.

### D11 — The page replaces `/generate` and keeps the old URL alive

`/generate` becomes a router redirect to `/dispatch`, the nav entry and the
home card point at `/dispatch`, and `GenerateView.vue` plus its test are
removed. Keeping the redirect is one line and avoids dead bookmarks and
old links in email threads.

### D12 — `packets_dispatched_at` keeps the filled-slot precondition

The manual-match dispatch refuses unless the assigned count equals
`num_children` ("all child slots must be filled before dispatch"). Sending
a packet to a sponsor who is still expected to take more children is the
same mistake, so the send path applies the same precondition. Sponsors
who are short of their quota are shown as blocked on the page, not failed
at send time.

### D13 — Drafting is a separate, side-effect-free step

"Draft" renders the template and stores the row; it never sends and never
touches `packets_dispatched_at`. A coordinator can draft every ready
sponsor, read them all, and then send them one by one.

### D14 — A packets-only run skips the sponsors it cannot build

`db_generate`'s packet phase calls `_validate_sheets_exist`
(`db_generate.py:469-524`), which collects the missing angel ids and
raises if any required sheet is missing, and the whole job fails. That is
right in the combined run (`only="both"`): the sheet phase has just run,
so a missing sheet means a sheet failed and the coordinator needs to know.

It is wrong for a packets-only run. The coordinator pressed "build the
packets"; one child whose sheet failed must not stop the other forty
sponsors from getting their packets.

So in `only="packets"` mode the packet phase computes the eligible
sponsors, **skips** the ones that are missing a sheet, and reports them
in a new result field `packets_blocked: list[{sponsor_id, angel_ids}]`.
That field is separate from the existing `packets_skipped`, which means
"the packet files already existed": two different outcomes, two counters.
The job still succeeds. The page shows the count before the run ("Generate
eligible packets (K)") and the blocked list after it.

`only="both"` keeps today's strict behavior unchanged, and the
per-sponsor packet path (`{sponsor: id}`) keeps it too: one sponsor's
missing sheet is that sponsor's problem, and the job reports it.

### D15 — One new file, and it lives with the packet

Packets and sheets keep living under `storage.artifact_root`
(`output/artifacts/sponsor_packets/<sponsor_id>/`). The draft body is a
database row, not a file, so there is no third copy of the email on disk.

The one new file is the packet's **manifest**
(`<packet dir>/manifest.json`, D16), and it lives beside the files it
describes.

### D16 — A packet carries a build manifest, and the draft is rendered from it

The draft body and the attached PDF must describe the same children. Today
they do not have to: the packet PDF is built once, the draft renders the
*current* assignments, and a later assignment change leaves a new angel
table attached to an old PDF. Worse, artifact ids are not immutable
content references — a rebuild registers a new `artifacts` row for the
same path, so the old row and the new row resolve to the same mutable
file.

So the packet build writes `manifest.json` next to the files, in the
packet's own directory:

```
build_id          uuid
sponsor_id        int
season            str
created_at        timestamp
subject           str          (the subject the email will carry)
sponsor           {first_name, email}          (the greeting and To)
angels            list of {angel_id, child_id, gender, age, dropoff}
items             list of {role, filename, sha256, size_bytes,
                          child_ids, angel_ids}
fingerprint       sha256 over (child ids, angel ids, per-sheet sha256)
```

- The manifest carries everything the template needs — the angel list
  **with gender, age and drop-off**, the greeting name, the subject and
  the season — so the draft body can be rendered from the manifest alone,
  with no live query and no chance of rendering today's assignment
  against yesterday's PDF.
- The manifest does **not** carry artifact ids. The files are built
  first and registered afterwards (`db_generate.py:1043-1049`), so an
  id cannot exist before the file does. The artifact ids are resolved
  after registration by `(build_id, role)`, and the artifact rows carry
  the build id in their `meta`.
- The **draft body** is rendered from the manifest's angel list.
- The **send path** refuses when the manifest's fingerprint no longer
  matches the current assignment and the current sheet hashes: the
  coordinator re-drafts (and regenerates the packet) first.
- The **attachment bytes** are read once, hashed, and those exact bytes
  are what the transport sends. Nothing is re-opened by path afterwards.
- The packet files and the manifest are written to temporary names and
  renamed into place, so a reader never sees a half-written PDF or a
  manifest without its files.
- The packet skip predicate now requires a **complete manifest**, not
  just "the HTML and the PDF exist". A packet built before this change
  has no manifest, so it is rebuilt on the next run; until then the
  sponsor shows the blocker `manifest_missing` and cannot be drafted.

`manifest.json` is registered as an artifact of kind `sponsor_packet`
with the role `manifest`, so the dispatch page can find it without
knowing the packet directory layout.

### D17 — The send is an attempt row, not a boolean

Sending a mail is the one irreversible thing this page does, and a
network failure does not prove the message was not accepted. The plan
therefore treats a send as an **attempt** with a durable lifecycle:

1. **One transaction** does all of it: apply the coordinator's typed
   subject and note to the draft, bump the draft version with a
   conditional update (`WHERE version = ?`), and insert the attempt row
   (`state="sending"`, a generated RFC 5322 `Message-ID`, a snapshot of
   subject, body, recipients and attachment hashes). The attempt is
   durable only if the sender wins the right to call the transport — a
   loser of the version check inserts nothing, so there are no orphan
   `sending` rows. The partial unique index
   `(season_id, sponsor_id) WHERE state = 'sending'` backs this up.
2. Call the transport **outside** the database transaction.
3. On success, commit `state="sent"`, the provider message id, the
   sponsor stamp and the audit event **in one transaction**. If that
   write fails, the attempt is still `sending`; the recovery sweep
   (§7.4) marks it `unknown` — never `failed`.
4. On a transport error, the state is `failed` only when the error
   proves non-delivery (an authentication rejection, a rejected
   recipient). A timeout, a dropped connection or an ambiguous 5xx is
   `unknown`.
5. `unknown` is never auto-retried. The page says "we do not know if this
   went out" and the coordinator checks the Gmail Sent folder (the
   deterministic `Message-ID` makes the search possible) before deciding.

Every attempt — sent, failed or unknown — writes its audit event in the
same transaction as its terminal state.

Without this, a timeout followed by "Retry" sends the packet twice.

### D18 — The transport is off the event loop, and sending waits for the pipeline

- The transport is synchronous (Gmail's `execute()`, `smtplib`), so it
  runs through `asyncio.to_thread` with an explicit connect/read timeout.
  A request must never block the API's event loop.
- Sending is refused while a **pipeline-class** job is active —
  `acquire`, `screen`, `match`, `generate`, `stats`, `clarify_generate`
  (`jobs/registry.py:44-57`). A match run can move a sponsor's children
  under the send, and a generate run can replace the attachments. The
  page disables the send button and says which job is in the way.
- The refusal is enforced **at write time**, not by a check before the
  send. A precheck alone is a time-of-check/time-of-use hole: a pipeline
  job can be enqueued a moment later, and a manual assignment can be
  committed during the transport call. So the two sides share the same
  database mechanism:
  - `enqueue_job` refuses a pipeline-class job while a send attempt is
    in flight for that season,
  - the manual matcher's `assign` / `release` commits check for an
    in-flight attempt **inside their own write transaction**,
  - the audit saves that touch a dispatched sponsor's **recipient** (the
    sponsor's email) or its **output eligibility** (the ignore flag)
    check for an in-flight attempt the same way — otherwise the address
    or the eligibility can change while the mail is in flight,
  - and the send's own transaction checks for an active pipeline job and
    re-validates the assignment and the recipient (B1).

  The in-flight attempt row *is* the reservation: it is committed before
  the transport call, and it is what the other two sides look for.
- The same exclusion explains the 409 the coordinator would otherwise
  meet when enqueuing generation: the exclusion covers every
  pipeline-class job, not just another `generate`.

---

## 5. Backend design

### 5.1 Configuration

New `DispatchSpec` in `src/ccat/config.py`, added to `Config` as
`dispatch: DispatchSpec = DispatchSpec()`:

```
enabled: bool = False
transport: Literal["gmail_api", "smtp"] = "gmail_api"
sender_email: str = ""
sender_name: str = "The Copper Country Angel Tree Team"
reply_to: str = ""
gmail_client_file: str = "ccat_google_credentials.json"   # the existing Drive client
gmail_token_file: str = "ccat_google_gmail_token.json"     # a token for gmail.send
smtp_host: str = "smtp.gmail.com"
smtp_port: int = 465
smtp_username: str = ""
smtp_password_env: str = "CCAT_SMTP_PASSWORD"
require_filled_slots: bool = True
max_attachment_bytes: int = 25_000_000
```

`enabled` defaults to off, so a deployment without the Gmail token
starts fine and the page shows "mail is not configured" instead of a send
button that fails. Secrets stay out of the JSON config: the app
password is read from the environment variable named by
`smtp_password_env`, the way `api.session_secret_env` and
`email_check.api_key_env` already work.

The `bcc_emails` list stays where it is
(`sponsor_packets.bcc_emails`) — one program address list, not two.

### 5.2 Schema and storage changes

One Alembic revision after `20261121_0008_sponsor_packets` with the two
tables from D5: `sponsor_drafts` and `sponsor_send_attempts`.

Three changes ride along:

- **Write ownership.** `db/repositories.py` gains `"dispatch"` in *both*
  maps: `WRITABLE_ENTITIES["dispatch"] = frozenset({"sponsor"})` and
  `ALLOWED_CHANGES["dispatch"] = {"sponsor": frozenset(SPONSOR_DISPATCH_COLUMNS)}`.
  An entry in `ALLOWED_CHANGES` without one in `WRITABLE_ENTITIES` is
  refused — the module may not write sponsors at all.
- **A dedicated repository method** `commit_dispatch(...)` that writes the
  attempt row, the sponsor stamp and the audit event in one transaction.
  It does not go through `commit_assignments`, which would demand the
  child and family stores and their versions for a write that touches
  only the sponsor.
- **Artifact registration.** `_build_packets` returns registration entries
  carrying the sponsor id, the **role** (`email_html`, `packet_pdf`,
  `instructions_pdf`, `manifest`), the manifest id and the sha256, and
  `register_file(...)` stores them in `meta`. Today the HTML and the
  merged PDF share the kind `sponsor_packet`, so the role has to be
  explicit: a sponsor id alone does not say which file is which.

  Existing rows carry no usable metadata — `register_file` stores
  `None` for an empty mapping (`db/artifacts.py:135`,
  `db/repositories.py:2013`), so the column is `NULL` for them — and a
  rerun will not backfill them, because packets are skipped when their
  files exist. The lookup therefore falls back to the deterministic
  root-relative path the `artifacts.path` column already holds
  (`sponsor_packets/<sponsor_id>/<filename>`,
  `db/artifacts.py:115-126`) and takes the newest row for that path — no
  substring search over paths and no migration of the artifact table.
  The lookup handles `NULL` and `{}` metadata alike.
- **The packet skip predicate changes** to require a complete manifest
  (D16), not just "the HTML and the PDF exist". A packet built before
  this change is rebuilt on the next run; until it is, the sponsor
  carries the blocker `manifest_missing` and cannot be drafted.

No column is added to `final_sponsor`: `packets_dispatched_at` is already
the "went out" stamp, and the new tables carry the detail.

### 5.3 The mail transport

The protocol lives in `src/ccat/services/ports.py` next to
`SeasonStore`; the implementation lives in `src/ccat/mail.py`:

```python
class Attachment(NamedTuple):
    filename: str
    content_type: str
    payload: bytes
    sha256: str

class SendRequest(NamedTuple):
    sender: str
    sender_name: str
    reply_to: str
    to: str
    bcc: Sequence[str]
    subject: str
    html_body: str
    message_id: str          # RFC 5322, generated before the send
    attachments: Sequence[Attachment]

class SentMessage(NamedTuple):
    provider_message_id: str
    accepted_at: datetime

class MailTransport(Protocol):
    def send(self, request: SendRequest) -> SentMessage: ...
```

A failure is not one thing, so the transport reports three outcomes:

| Outcome | Meaning | Retryable |
|---|---|---|
| `sent` | The provider accepted the message. | — |
| `rejected` | The provider refused it (bad credentials, no such recipient, quota refused). The message was not accepted. | yes |
| `ambiguous` | A timeout, a dropped connection, an ambiguous 5xx. The message may or may not have been accepted. | **no** |

Only `rejected` is retryable. `ambiguous` becomes `unknown` (D17).

**Gmail.** The existing OAuth **client** file is reused; only the *token*
is separate, so the Drive integration is untouched:

- Scope `https://www.googleapis.com/auth/gmail.send` — the right scope for
  `users.messages.send`. `gmail.compose` is only needed for Gmail-side
  drafts, and there is no `mail.send` scope.
- `ccat_google_gmail_token.json` sits next to the Drive token and holds a
  refresh token for the new scope.
- An **operator-run** command, `ccat mail-authorize`, performs the
  desktop consent once and writes the token. It never runs inside a
  request: the repo's `run_local_server` (`gdrive.py:54-57`) opens a
  browser, which a headless host cannot do.
- The refreshed token is written back atomically (temp file plus rename),
  so a crash mid-refresh cannot corrupt it.
- The `From` address must be the authenticated account or a verified
  send-as alias. A configured sender the account cannot send as is
  refused by Gmail, not by us — so the rehearsal sends a real message
  first.
- The body is the RFC 5322 message built with `email.message.EmailMessage`
  (multipart, one part per attachment) and sent as base64url `raw` to
  `users().messages().send(userId="me")`.

**OAuth reality check (Phase 0, owner decision).** `gmail.send` is a
sensitive scope. An external app left in Google's "Testing" publishing
status gets refresh tokens that expire after seven days, and the
**authorizing Google account** is shown an "unverified app" warning on
the consent screen. Sponsors see the program address, not the OAuth app,
but the sending account's reputation is what is at stake. Production
needs the app verified and published, or an app inside the
organization's own Workspace project, or the SMTP alternative. That is a
policy decision (§14.2), and it is why Phase 0 comes before any code.

**SMTP (documented alternative, not built first).**
`smtplib.SMTP_SSL` to `smtp.gmail.com:465` with an app password read from
the environment variable named in the config. It is a second
implementation behind the same protocol, written only when someone
decides the OAuth route is wrong.

**Timeouts and the event loop.** Both transports are synchronous, so they
run through `asyncio.to_thread` with an explicit connect and read
timeout. A hung provider must not hold the API's event loop, and a
timeout must surface as `ambiguous`, never as `rejected`.

### 5.4 The dispatch service

New `src/ccat/services/db_dispatch.py`, following the `db_clarify.py`
shape (async, typed results, explicit store).

**Season binding.** Every function takes the season-bound repository the
route resolved (`api/deps.py:48-80`) instead of opening its own store
with `_store(cfg)`. The route serves whatever season row exists under
the URL name and refuses only a season that does not exist at all
(`deps.py:73-80`), so the two bindings are not the same thing by
construction: `_store(cfg)` always uses `cfg.season`. Binding the service
to the repository the route already resolved keeps the season correct,
and it avoids opening a second engine and session per request. Every
row, artifact and attempt is bound to that repository's season.

The actor's **username and user id** are both passed, for audit parity
with the rest of the system.

```python
async def list_dispatch_rows(repo, cfg, actor, user_id, options) -> DispatchListResult
async def list_dispatch_children(repo, cfg, actor, options) -> DispatchChildListResult
async def draft_sponsors(repo, cfg, actor, user_id, options) -> DraftResult
async def get_draft(repo, cfg, actor, sponsor_id) -> DispatchDraftView
async def update_draft(repo, cfg, actor, user_id, sponsor_id, patch, expected_version) -> DispatchDraftView
async def send_draft(repo, cfg, actor, user_id, sponsor_id, patch, expected_version) -> DispatchSendResult
```

`patch` carries the coordinator's typed subject and note, so one request
sends what the popup shows (§6.5).

**`list_dispatch_rows`** — one row per eligible sponsor with assigned
angels, joining what the page needs to show:

| Field | Source |
|---|---|
| `sponsor_id`, `first_name`, `last_name`, `organization_name`, `sponsor_type`, `email`, `physical_packet` | `final_sponsor` |
| `assigned_count`, `num_children`, `assigned_angel_ids` | children join + `final_sponsor.num_children` |
| `sheets_ready_count`, `missing_sheet_angel_ids` | `final_child.generated_info_sheet` for the sponsor's children |
| `packet_status` | `"missing"` / `"ready"` / `"stale"`, from the `sponsor_packet` artifacts |
| `packet_pdf_artifact_id`, `packet_created_at`, `instruction_pdf_artifact_id` | the `artifacts` rows for this sponsor |
| `draft_version`, `draft_subject` | `sponsor_drafts` |
| `email_status` | derived: `"not_drafted"` / `"draft_ready"` / `"draft_edited"` from the draft row when there is no attempt, otherwise `"sending"` / `"sent"` / `"failed"` / `"unknown"` from the latest attempt |
| `email_sent_at`, `email_error` | the latest attempt |
| `packets_dispatched`, `packets_dispatched_at` | `final_sponsor` |
| `sponsor_row_version` | `final_sponsor.row_version`, for the manual-match dispatch call |
| `duplicate_recipient_sponsor_ids` | other sponsors sharing the same address |
| `blockers` | derived: `no_email`, `missing_sheets`, `slots_not_filled`, `already_sent`, `packet_changed`, `manifest_missing`, `mail_not_configured`, `pipeline_job_running` |

The list response also carries the totals the page header needs:
`sponsor_count`, `assigned_children`, `sheets_ready`, `sheets_missing`,
`packets_ready`, `packets_missing`, `dispatched_count`.

`email_status`, `email_sent_at` and `email_error` are derived from the
**latest attempt**, not stored on the draft row, so there is one source
of truth (D5).

`sheets_ready_count` is the **marker**, not the file. A marker with no
file on disk would make a sponsor look ready when it is not, so the
packet phase (D16) is what proves the files exist; the list reports the
marker and the packet status separately.

**`list_dispatch_children`** returns the assigned children with
`generated_info_sheet`, their sheet artifact id and their sponsor id —
the rows behind the page's "child sheet exceptions" drawer, so a missing
sheet can be generated for one child without leaving the page.

**`draft_sponsors`** — for each eligible sponsor with a built packet and a
usable recipient, render the template **from the packet's manifest**
(D16) and upsert the draft row. It is side-effect free apart from the
draft rows. Options: `sponsor_ids` (list), `all` (every ready sponsor),
`overwrite` (re-render an existing draft). The manifest gives the angel
list and the attachment hashes, so the draft and the attachments are
guaranteed to describe the same children.

**`update_draft`** — subject and note; `If-Match: version` like the
clarify draft endpoint (`routes/clarify.py:188`). The note is inserted
into the template body before the sign-off and the body is re-rendered,
so the angel table stays generated. There is no raw-HTML override (D6).

**`send_draft`** — the ordered steps (D17):

1. Load the sponsor, the draft, the manifest and the artifacts.
2. Re-check the blockers (no email, no packet, missing sheets, slots not
   filled, already sent, a pipeline job running) → `409` with the reason.
3. Verify the manifest fingerprint against the current assignment and the
   current sheet hashes → `409 packet_changed` when they differ.
4. Read the attachment bytes **once**, hash them, and check the sizes
   against `max_attachment_bytes`. Every attachment's computed hash must
   equal the hash recorded in the draft: the manifest's fingerprint
   covers the assignment and the source sheets, but a merged PDF or an
   instruction file can change without either changing. Those exact
   bytes are what the transport sends.
5. **One transaction**: apply the typed subject and note to the draft,
   bump its version with a conditional update (`WHERE version = ?`), and
   insert the attempt row (`state="sending"`, the generated `Message-ID`,
   the content snapshot). The attempt is durable only if the sender wins
   the right to call the transport (D17).

   **Inside that same transaction** the sender re-reads the sponsor and
   the assignment and re-validates: the manifest fingerprint against the
   current assignment, the recipient against the sponsor's current email,
   and the sponsor's `row_version` against the one the draft was drawn
   from. Steps 1-4 happen before the transaction and can be stale by the
   time it starts — a manual assignment or an audit edit can commit in
   between — and a reservation that was taken on stale facts would mail
   the wrong child to the wrong address. A mismatch aborts the
   transaction, so no attempt exists and nothing is sent.
6. Call the transport off the event loop, outside any transaction.
7. On `sent`: commit `state="sent"`, the provider message id, the sponsor
   stamp and the audit event **in one transaction**.
8. On `rejected`: `state="failed"` with a short sanitized error, plus the
   audit event; no stamp; retryable.
9. On `ambiguous`: `state="unknown"`, plus the audit event; no stamp;
   **not** retryable from the page.

A failed or unknown send never leaves the sponsor stamped: the stamp
happens only on a confirmed success.

### 5.5 Results and DTOs

`src/ccat/services/results.py` gains one field on the existing result
and two new models:

```python
# services/results.py — GenerateResult gains:
packets_blocked: list[dict[str, object]] = []   # [{sponsor_id, angel_ids}] in a packets-only run

class DispatchResult(StrictResult):
    drafts_created: int = 0
    drafts_skipped: int = 0
    sponsors: list[int] = []
    blocked: dict[str, list[int]] = {}

class DispatchSendResult(StrictResult):
    sponsor_id: int
    state: Literal["sent", "failed", "unknown"]
    attempt_id: str = ""
    message_id: str = ""
    draft_version: int = 0        # the version after the transition, so a retry can use it
    sent_at: str = ""
    error: str = ""
```

`draft_version` matters: starting the send bumps the draft version, so a
retry that reuses the version the popup was opened with would be refused
as stale. The result carries the current version, and the popup uses it.
(The draft row has no status of its own — the attempt carries the
state.)

`src/ccat/api/dto.py` gains `DispatchRowView`, `DispatchTotals`,
`DispatchListView`, `DispatchChildView`, `DispatchChildListView`,
`DispatchDraftView`, `DispatchDraftRequest`, `DraftBatchRequest`,
`DispatchSendRequest`.

`DispatchSendRequest` carries `{version, subject, note}` — the version
the coordinator was shown and the edits they typed — so one request
sends what the popup shows (D17, N1).

### 5.6 API routes

New module `src/ccat/api/routes/dispatch.py`, mounted with the standard
`current_user` dependency (`api/__init__.py:143-164`), prefix
`/api/v1/seasons/{season}/dispatch`:

| Method | Path | Body → Response | Role |
|---|---|---|---|
| GET | `` | → `DispatchListView` | any authenticated |
| GET | `/children` | → `DispatchChildListView` | any authenticated |
| POST | `/drafts` | `DraftBatchRequest{sponsor_ids?, all?, overwrite?}` → `DispatchListView` | coordinator |
| GET | `/sponsors/{sponsor_id}/draft` | → `DispatchDraftView` | any authenticated |
| PATCH | `/sponsors/{sponsor_id}/draft` | `DispatchDraftRequest` + `If-Match: version` → `DispatchDraftView` | coordinator |
| POST | `/sponsors/{sponsor_id}/send` | `DispatchSendRequest{version, subject, note}` → `DispatchSendResult` | coordinator |

Roles are a set, not a hierarchy: `require_roles` tests for an
intersection (`api/auth.py:437-463`), so every mutating route names
both — `require_roles("coordinator", "admin")`, the way the existing
manual routes do. Every mutation carries the CSRF header requirement. No path contains `clarif`, so the
guard in `test/test_api.py:1800-1806` ("no route that mentions clarify
also sends") still holds — that test exists precisely because the refactor
decided drafts are never sent, and the dispatch page is the deliberate
exception in a different namespace.

The send endpoint is the only route in the system that causes an outbound
message. It is worth a comment at the route saying so, pointing at D10
and D17.

Marking a **physical** sponsor as dispatched is not a route here: the
page calls the existing `POST /manual-match/dispatch` with the sponsor's
`row_version` (`sponsor_row_version`).

### 5.7 Generation job options

`src/ccat/services/db_generate.py`:

- `options` gains `only: str = "both"` and `child: str | None = None`,
  validated like `max_workers` today (`InfoSheetError` for a bad value).
- The sheet phase runs when `only in ("sheets", "both")`.
- The packet phase runs when `only in ("packets", "both")` and is skipped
  when `child` is set.
- In `only="packets"` mode the packet phase skips a sponsor whose
  children are missing a sheet and reports it (D14). In `only="both"`
  mode the strict validation is unchanged.
- `child` is a **`zite_record_id`**, not an angel id. The angel id is
  not unique while duplicates are pending, and `_one_row_per_angel`
  (`db_generate.py:784-817`) silently keeps the first row — so "exactly
  one child" would not be guaranteed. A bare angel id is accepted only
  when it resolves to exactly one eligible row, and is rejected as
  ambiguous otherwise.
- A `child` selection ignores the `generated_info_sheet` flag for that
  child, so an existing sheet can be rebuilt. The flag is cleared **before**
  `_pending_rows` filters, not after.
- The combinations are validated up front: `child` with `only="packets"`
  is refused, `child` with `sponsor` is refused unless the child belongs
  to that sponsor, and an unknown or ineligible child is an error rather
  than a job that quietly generates nothing.

`src/ccat/api/dto.py` `JobCreate.options` stays `dict`, so the option
names need no schema change. `/runs/generate/settings` is **not** touched:
`only` and `child` are per-run options the page passes directly, and
`GenerateRunSettings` (`api/dto.py:1024-1037`) does not need to learn
about them. `frontend/src/api/jobs.ts` has no option registry to extend
either.

### 5.8 Regenerating the API contract

After the DTO changes, and **before** the frontend work that depends on
them:

```sh
.venv/bin/python scripts/export_openapi.py      # refresh frontend/openapi.json
cd frontend && npm run gen:api                   # refresh frontend/src/api/schema.d.ts
```

Both files are tracked, so both are part of the change.

### 5.9 What does not change

- `db_generate`'s eligibility rules for the existing paths (the combined
  run and the per-sponsor path keep their strict validation), the
  `generated_info_sheet` ownership, the artifact kinds (the roles are
  metadata inside them), the packet directory layout (the manifest joins
  the files in it), the LibreOffice conversion seam, the job states and
  the retry policy.
- The clarify flow, which stays a separate, unsent, family-facing thing.
- `docs/limitations.md`'s "no automatic email sending" line — it is
  replaced, not reinterpreted (see §13).
- The Drive integration and `gdrive.py`, which is not modified.

---

## 6. Frontend design

### 6.1 The one-sentence job of the page

Let a coordinator see which sponsors still need to go out, generate any
missing child info sheets or sponsor packets in bulk or for one record,
review the pre-rendered email with its two attachments, and send it from
the program's Gmail account.

### 6.2 Information architecture

Four regions, top to bottom:

1. **Header + active job strip** — title, one-line subtitle, `Refresh`,
   and a compact strip when a **pipeline-class** job is queued or running
   (`acquire`, `screen`, `match`, `generate`, `stats`,
   `clarify_generate`): job id, phase, `completed / total`, and a link to
   `/jobs/:id`. The exclusion is per season, not per job type: a
   generation request while a `match` job runs is refused with `409` as
   well. The strip names the job that is in the way, so the coordinator
   does not meet a bare 409.
2. **Generation card** — the bulk actions, the `Overwrite` checkbox, and
   a collapsed "generate one" disclosure for a single sponsor or a
   single child.
3. **Sponsor work list** — the table, the filters, the row actions.
4. **Child sheet exceptions** — a `<details class="card">` listing the
   assigned children whose sheet is missing, with a `Generate sheet`
   button per row. Collapsed when nothing is missing.

Deliberately **not** on this page: the live SSE event tail and the
cancel button (they stay on `/jobs/:id`); editing a sponsor's email or a
child's wish list (that stays on the audit pages, reachable with the `↗`
button); assigning or releasing children (that stays on `/match`);
the flat table of every artifact from every run (the downloads the
coordinator needs are per sponsor and per child, in context); a "send
all" button; and the `test` / `max_workers` debug flags.

### 6.3 The work list

A `table.data`, not `SponsorCardList.vue` (that component is a card
picker for `/match`; the dispatch page needs tabular alignment).
The helper logic — name, organization, remaining slots, the dispatch
block reason — is shared, but it has to be **extracted**: those helpers
are local to `SponsorCardList.vue`'s `<script setup>` and are not
exported. They move to a new `frontend/src/utils/sponsor.ts` that both
components import.

| # | Column | Shows | Source |
|---|---|---|---|
| 1 | Sponsor | `#<sponsor_id>` chip, name (organization or first + last), affiliation, `↗` details | `sponsor_id`, `first_name`, `last_name`, `organization_name`, `sponsor_type` |
| 2 | Recipient | The sponsor's email, or a `No email on file` badge; a `physical packet` pill when `physical_packet` is true | `email`, `physical_packet` |
| 3 | Assignment | `assigned_count / num_children`, the angel ids, `Filled` / `1 slot open` / `Unassigned` | `assigned_count`, `num_children`, `assigned_angel_ids` |
| 4 | Child sheets | `X / Y ready`, plus the missing angel ids | `sheets_ready_count`, `missing_sheet_angel_ids` |
| 5 | Packet | `Ready` + a `PDF` download link + the build time, or `Missing` | `packet_status`, `packet_pdf_artifact_id`, `packet_created_at` |
| 6 | Email & dispatch | `Not drafted` / `Draft edited` / `Sending…` / `Sent <time>` / `Send failed`, and `Dispatched` when `packets_dispatched` is true (never "Locked" — see D3) | `email_status`, `email_sent_at`, `email_error`, `packets_dispatched`, `packets_dispatched_at` |
| 7 | Actions | The row's buttons (§6.4) | — |

Every badge carries text as well as colour, so the table is readable
without colour.

**Filters** (two rows, `.filters`): quick chips `Needs action` (default),
`Ready to send`, `Missing sheets`, `Missing packet`, `Missing email`,
`Send failed`, `Dispatched`, `All sponsors`; then a search box (sponsor
id, name, organization, email, angel id), `Affiliation`, `Dispatched`,
`Assignment`, `Sort` selects, a `Showing X of Y sponsors` counter and a
`Clear filters` button.

**Sort**: readiness (failed first, then ready to send, then missing
packet, then missing sheets, then dispatched), sponsor id, name,
assigned children.

### 6.4 The action surface

Two levels, so there is no wall of buttons.

**Bulk (the generation card):**

| Button | Job options | Confirm |
|---|---|---|
| `Generate pending sheets (N)` | `{only: "sheets"}` | normal `ConfirmDialog` |
| `Generate eligible packets (K)` | `{only: "packets"}` | normal `ConfirmDialog` |
| `Generate sheets & packets` | `{}` (both phases) | normal `ConfirmDialog` |
| `Regenerate all sheets (M)` / `Regenerate all packets (P)` | same, with `overwrite: true` | `ConfirmDialog` with `dangerous` |

The `Overwrite completed files` checkbox flips the first two labels and
adds the danger confirm. Its helper line says what overwrite does:
rebuilds existing files and backs up the previous email HTML to
`.manual.html`.

**Single item ("generate one" disclosure):**

- `Sponsor ID` + `Generate sponsor packet` → `{sponsor: id, only:
  "packets"}`.
- `Child record ID` + `Generate child sheet` → `{child: id, only:
  "sheets"}`.

**Per row:**

| Row state | Buttons | Job options |
|---|---|---|
| Packet missing, sheets ready | `Generate packet` (secondary) | `{sponsor: id, only: "packets"}` |
| Sheets missing | `Generate packet` (secondary) | `{sponsor: id}` — sheets **and** packet, because a packet-only run would fail the sheet validation for this sponsor |
| Packet ready, not dispatched | `Draft & send…` (primary), `Regenerate` (secondary) | `Regenerate` → `{sponsor: id, only: "packets", overwrite: true}` (without `overwrite` the existing files are skipped and nothing is rebuilt) |
| Send failed | `Retry send…` (primary) | — |
| Send outcome unknown | `Check Gmail` (secondary) — no retry button | — |
| Dispatched, email sent | `View email` (secondary) | — |
| `physical_packet` true, not dispatched | `Mark dispatched` (secondary) | — |

`Mark dispatched` does not send anything: it stamps the sponsor through
the **existing** `POST /manual-match/dispatch` endpoint, which already
carries the filled-slot guard, the manual-match assign/release guard,
the optimistic version check and the audit event. No new endpoint is
needed for it.

`Regenerate` asks for a danger confirm, because it replaces files that a
coordinator may have reviewed.

Sending is disabled while a pipeline-class job is running (D18): a match
run can move a sponsor's children under the send, and a generate run can
replace the attachments. The button says which job is in the way.

### 6.5 The email draft popup

`SponsorEmailModal.vue`, modelled on `EntityModal.vue` (focus save and
restore, dirty-close guard) and `HoldDialog.vue` (gating, keyboard
handling). One popup at a time: the parent holds a single
`activeSponsorId`.

Layout, top to bottom:

```
+--------------------------------------------------------------------+
| Email sponsor #14 — Jane Doe (Houghton Rotary)            [✕ Close] |
| Ready to send · 3 assigned Angels (14A, 14B, 22A)                  |
+--------------------------------------------------------------------+
| From:   angeltree@example.org (program Gmail)              read-only |
| To:     jane.doe@example.org                              read-only |
| BCC:    coordinator@example.org, archive@example.org       read-only |
| Subject: [ Angel Tree 2026 — your 3 Angels                  ]        |
+--------------------------------------------------------------------+
| Attachments (2 PDFs)                                                |
|  • Sponsor Instruction 2026.pdf (142 KB)                 [Download] |
|  • Sponsor 14 - Child Information 2026.pdf (488 KB)      [Download] |
+--------------------------------------------------------------------+
| Email body (preview only)                                            |
| +------------------------------------------------------------------+ |
| | Dear Jane, …                                                      | |
| |  [Angel ID | Gender | Age | Drop-off location]                    | |
| +------------------------------------------------------------------+ |
| Note (optional, plain text):                                         |
| [ The hancock drop-off closes at 4pm on Friday.                 ]    |
+--------------------------------------------------------------------+
| Sending emails the 2 PDFs to jane.doe@example.org and marks sponsor  |
| #14 as dispatched.                                                   |
| [Send failed: …]                                                    |
| [Save draft]                                   [Cancel] [Send email] |
+--------------------------------------------------------------------+
```

- **Envelope**: From, To and BCC are read-only. The To address is the
  sponsor's address on record; if it changed since the draft was written,
  the popup says so and the send is refused until the draft is redrawn.
  Fixing an address happens on the sponsor record (`Edit sponsor record
  ↗`), so the database and the sent mail never drift apart.
- **Subject**: an editable text input, pre-filled with
  `Angel Tree <season> — your <N> Angel(s)`. Required.
- **Attachments**: the two files the packet holds, with their names and
  sizes, each downloadable. A missing file disables Send and says which
  one is missing.
- **Body**: a preview, not an editor. The body is the template rendered
  from the packet's manifest, shown in an `<iframe sandbox="" :srcdoc>`,
  so the email's inline CSS renders as it will in Gmail and nothing from
  the server HTML can run in the app's origin.
  - The **note** is the only editable part of the message body. It is
    plain text, inserted into the template before the sign-off, and the
    body is re-rendered from the template when it changes.
  - There is no raw-HTML editing (D6): the angel table, the file names
    and the greeting stay generated, because a hand-typed angel table is
    how the wrong child gets emailed, and a hand-edited body can
    disagree with the PDF it ships with.

- **Send button states**: `Send email` (idle) → `Sending…` (disabled,
  and the close button and the backdrop are inert while it runs) →
  `Retry send` (after a rejection, with the error in a `role="alert"`
  banner above the buttons) → `Check Gmail` (after an ambiguous
  outcome, where retrying could mail the packet twice).

  The send call carries the subject and note the coordinator has typed,
  together with the draft version, and applies them in the same
  transaction that writes the attempt. There is no window where the
  message that goes out is older than what the popup shows, and the
  "Save draft" button is a convenience, not a precondition.

  The result carries the new draft version, so a retry after a failure
  uses the current version rather than the one the popup was opened
  with.

  A failure keeps the popup open with every edit intact. Success closes
  the popup and puts a notice on the page: `Sent packet email to Jane
  Doe (jane.doe@example.org). Sponsor #14 is now marked dispatched.`

- **Keyboard**: on open, focus moves to the Subject field (or the close
  button in read-only mode); Tab cycles inside the dialog; Esc asks
  `Discard unsaved email changes?` when the draft is dirty and closes
  otherwise; Esc is ignored while a send is in flight; on close, focus
  returns to the row button that opened the popup.

### 6.6 States

| Region | State | What the coordinator sees |
|---|---|---|
| Page | No season | `No active season loaded.` and nothing else |
| Page | Read-only role | `Read-only: you need the coordinator or admin role to generate packets or send sponsor emails.` The list and the downloads stay usable; the mutating buttons are disabled |
| Job strip | Running | `Generation job #42 is running (sheets: 18 of 45 completed).` + `Open job #42` |
| Job strip | Partial failure | `Generation job #42 finished with 2 failed items.` + `Open job #42`. The count comes from the terminal job result's `sheets_failed` list, not from `progress.failed` — the sheet phase never increments it (`db_generate.py:971-1006`). |
| Work list | Loading | `Loading dispatch work list…` (`role="status"`) |
| Work list | No sponsors | `No sponsor has assigned children yet. Run matching or assign families first.` + a link to `/match` |
| Work list | Filter misses | `No sponsors match these filters.` + `Clear filters` |
| Work list | Load error | The error text + `Retry` (`role="alert"`) |
| Row | Partial | `2 / 3 ready` with `Missing: 14B`; `Send failed` with the reason |
| Child exceptions | Nothing missing | Collapsed: `Child info sheets (45 of 45 generated — 0 missing)` |
| Child exceptions | Sheets missing | Open: `Child info sheets (42 of 45 generated — 3 missing sheets block packet generation)` |
| Popup | Loading | `Loading email draft and attachments…`; Send disabled |
| Popup | Stale (412) | `This sponsor's record or draft changed in another tab.` + `Reload draft` |

### 6.7 Accessibility

- Focus order: `Refresh` → job link → `Overwrite` → the bulk buttons →
  the "generate one" disclosure → the filter chips → search and selects
  → the table → the child-exception summary.
- Every row button carries an `aria-label` naming the sponsor
  (`Draft and send email for sponsor #14 Jane Doe`), so a screen reader
  user never has to read the table row to know what a button does.
- The popup is `role="dialog" aria-modal="true"` with a labelled title,
  traps Tab, and restores focus to its opener.
- Every status badge pairs its colour with its text.
- The send button's disabled state is a real `disabled` attribute, not a
  class.

### 6.8 Copy

| Where | String |
|---|---|
| Nav, home card, `<h1>` | `Dispatch` |
| Subtitle | `Generate child info sheets and sponsor packets, then review and send each sponsor's packet email.` |
| Bulk buttons | `Generate pending sheets (N)`, `Generate eligible packets (K)`, `Generate sheets & packets`, `Regenerate all sheets (M)`, `Regenerate all packets (P)` |
| Packets button helper | `K sponsors can be built now. Sponsors whose children are still missing a sheet are skipped and listed after the run.` |
| Overwrite | `Overwrite completed files` — `Off by default: skips sheets and packets that already exist. On: rebuilds existing files and backs up the edited email HTML to .manual.html.` |
| Confirm (normal) | `Run generation` / `Renders pending child info sheets and builds sponsor packets from completed sheets. The run is queued as a durable job.` / `Enqueue generation` |
| Confirm (overwrite) | `Overwrite generated files?` / `Overwrite re-renders child info sheets and sponsor packets that already exist. Existing packet HTML files are backed up to .manual.html before regeneration.` / `Overwrite and generate` |
| Single item | `Generate one sponsor or child`, `Sponsor ID`, `Generate sponsor packet`, `Child record ID`, `Generate child sheet` |
| Work list heading | `Sponsor packets (12 of 48 dispatched)` |
| Chips | `Needs action`, `Ready to send`, `Missing sheets`, `Missing packet`, `Missing email`, `Send failed`, `Dispatched`, `All sponsors` |
| Search | `Search sponsor #, name, organization, email, angel ID` |
| Counter | `Showing 12 of 48 sponsors` |
| Row buttons | `Generate packet`, `Regenerate`, `Draft & send…`, `Retry send…`, `View email`, `Mark dispatched` |
| Child exceptions | `Child info sheets (42 of 45 generated — 3 missing sheets block packet generation)`, `Generate sheet`, `Regenerate sheet` |
| Popup title | `Email sponsor #14 — Jane Doe` |
| Popup dispatch notice | `Sending emails the 2 PDFs to jane.doe@example.org and marks sponsor #14 as dispatched.` |
| Popup footer | `Save draft`, `Cancel`, `Send email`, `Sending…`, `Retry send`, `Check Gmail` |
| Success notice | `Sent packet email to Jane Doe (jane.doe@example.org). Sponsor #14 is now marked dispatched.` |
| Failure | `Could not send email for sponsor #14: <reason>` |
| Unknown outcome | `We do not know whether sponsor #14's email was accepted. Check the Gmail Sent folder for <message-id> before sending again.` |

### 6.9 Responsive behavior

- Wide: the generation card puts the bulk actions left and the "generate
  one" disclosure right; the table shows all seven columns; the popup is
  `min(52rem, 92vw)` with a `90vh` cap.
- Narrow: the filters wrap; the table scrolls horizontally inside its
  card, with the sponsor column and the actions column kept readable;
  the popup goes to `96vw`, the envelope stacks label-over-value, and the
  footer wraps with the send button on the right.

### 6.10 Components

| Component | Path | Modelled on | Props / emits |
|---|---|---|---|
| `DispatchView` | `frontend/src/views/DispatchView.vue` | `GenerateView.vue` + `ManualMatchView.vue` | route view; reads `session.season` |
| `DispatchGenerationBar` | `frontend/src/components/DispatchGenerationBar.vue` | `PipelineRunForm.vue` | props: `activeJob`, `pendingSheets`, `totalChildren`, `pendingPackets`, `eligibleSponsors`, `disabled`; emits: `enqueue`, `generateSponsor`, `generateChild` |
| `DispatchSponsorTable` | `frontend/src/components/DispatchSponsorTable.vue` | `SponsorCardList.vue` (its filter/sort helpers) | props: `sponsors`, `busySponsorId`, `disabled`; emits: `generateSponsor`, `openEmail`, `markDispatched`, `openDetails` |
| `SponsorEmailModal` | `frontend/src/components/SponsorEmailModal.vue` | `EntityModal.vue` + `HoldDialog.vue` | props: `open`, `season`, `sponsorId`; emits: `close`, `sent`, `saved`, `openSponsorDetails` |
| `ConfirmDialog` | existing | — | reused as is |
| `EntityModal` | existing | — | reused for the `↗` details button |
| `dispatch` API module | `frontend/src/api/dispatch.ts` | `api/clarify.ts` | `listDispatch`, `listDispatchChildren`, `getDraft`, `saveDraft`, `sendDraft`, `draftSponsors` |
| sponsor helpers | `frontend/src/utils/sponsor.ts` | extracted from `SponsorCardList.vue` | `personName`, `organization`, `remainingOf`, `dispatchBlock` — shared by the match and dispatch views |

---

## 7. Edge cases and failure modes

### 7.1 Data-shape edge cases

| Case | Behavior |
|---|---|
| Sponsor has no email | Blocker `no_email`. The row cannot be drafted or sent. Shown greyed out with the reason. |
| Sponsor email is not deliverable | `email_checks` already holds the ZeroBounce result. The list shows a "deliverability risk" marker; the send is not blocked (ZeroBounce is a point-in-time answer and re-checking costs credits). |
| Two sponsor rows share an email | `final_sponsor.email` is deliberately not unique (duplicate submissions keep their own row). Dispatch is per `sponsor_id`, so each row is drafted and sent separately, exactly as the packets are built — but the send needs an explicit confirmation naming the affected angels, because the two packets can hold different children for the same mailbox (§7.2, §8.1). |
| Sponsor assigned fewer children than `num_children` | Blocker `slots_not_filled` (D12). Same rule the manual-match dispatch applies. |
| Some of the sponsor's children have no sheet | In the combined run and in the per-sponsor run, the packet build fails validation for that sponsor (`SponsorPacketError`), so no packet is produced and the row shows `missing_sheets` with the count. In a **packets-only** run the sponsor is skipped instead, and the job reports it in `packets_blocked` (D14). |
| A sheet is missing but the packet exists from an earlier run | The packet PDF has fewer pages than angels. The row shows the mismatch (`sheets_generated` vs `assigned_count`) and the coordinator regenerates before sending. |
| Child has no `family_id` | Orphaned; excluded from generation and from packets (unchanged behavior). |
| Child has no assigned sponsor | A sheet exists to go into a sponsor's packet, so a child without an assigned sponsor cannot get one. "Generate child sheet" reports that the child has no sponsor yet, and the page links to `/match`. The eligibility rule is unchanged, not relaxed for this page. |
| `angel_id` with a path separator | Hard error at generation time, unchanged. The new `child` job option validates the id format before it reaches the renderer. |
| The packet was regenerated after the draft was written | The draft stores the attachment `sha256` values it was rendered from. A mismatch raises the blocker `packet_changed` and the coordinator re-drafts. |
| The packet was built before the manifest existed | It has no manifest, so it cannot be drafted from one. The sponsor carries the blocker `manifest_missing` and the next packet build rewrites it. |
| The sponsor's email changed after the draft was written | The draft keeps the recipient it was drawn for. The send is refused (`409`) until the draft is redrawn, so a mail never goes to an address the coordinator did not see in the popup. |
| The coordinator hand-edited the packet HTML | The old flow kept the edit in `<stem>.manual.html` (an artifact). The draft body is rendered from the manifest, so a hand edit is not carried into the draft. The popup's subject and plain-text note are the supported way to adjust a message (D6). |

### 7.2 Sending edge cases

| Case | Behavior |
|---|---|
| The provider refuses the message (bad credentials, no such recipient, quota refused) | Outcome `rejected` → the attempt is `failed` with a short sanitized error, the sponsor is **not** stamped, and the popup offers Retry with the same content. |
| A timeout, a dropped connection, an ambiguous 5xx | Outcome `ambiguous` → the attempt is `unknown`, the sponsor is **not** stamped, and **there is no retry button**: the popup says to check the Gmail Sent folder for the deterministic `Message-ID`. Retrying here is how a sponsor gets two packets. |
| The process dies between the transport call and the commit | The attempt stays `sending`. A recovery sweep marks attempts still `sending` after a few minutes as `unknown`, never as `failed`, so they are never auto-retried. |
| The token expired and cannot refresh | The provider rejects it → `failed`, with "re-authorize the mail account". The page surfaces it once, not per sponsor. |
| Attachments exceed the size limit | Rejected before the transport call, with the file sizes named. Nothing is sent. |
| The packet file is missing on disk, or its bytes changed since the draft | Rejected before the transport call (the manifest fingerprint check). Nothing is sent. |
| Two coordinators send the same sponsor | The attempt row is created with a conditional transition, so only one send starts; the second gets `409`. |
| The coordinator navigates away or reloads mid-send | The request continues server-side; the row ends up `sent`, `failed` or `unknown`, and the page shows that state on the next read. The popup itself cannot be closed while a send is in flight (§6.5). |
| Gmail daily quota exhausted | The provider rejects it → `failed` for that sponsor. **The remaining sponsors are affected too**: the quota is account-wide and BCC recipients count against it. The page shows the quota message once, and the work list marks the rest as blocked until the quota resets. |
| Two sponsor rows share one address | The work list flags both rows with the other sponsor ids; sending either one asks for an explicit confirmation naming the affected angels, because the packets can contain different children for the same mailbox. |

### 7.3 Interaction with the rest of the system

| Case | Behavior |
|---|---|
| A `generate` job is running while the page is open | The page shows the active job banner and the affected rows update as sheets are committed (the marker is written per item). Drafting during a run is allowed but the resulting draft may be stale (`packet_changed`). |
| A packets-only run skips a sponsor | The job succeeds, the blocked sponsor ids and their angel ids are in `packets_blocked`, and the row shows `Missing sheets` with the angel ids. The coordinator regenerates that sponsor's sheets and runs its packet alone. |
| A `match` job is running | The manual-match dispatch endpoint refuses while a match job runs; the dispatch page's send path refuses too (D18), because a sponsor's assignment can change under the coordinator. |
| A `generate` job is running | Sending is refused while it runs: the attachments it may replace are the ones being sent. |
| Another pipeline-class job runs (`acquire`, `screen`, `stats`, `clarify_generate`) | Generation is refused with `409` by the season's one-pipeline-job rule; the page's job strip names the job in the way. |
| The season changes | Every row is season-scoped (`season_id` on the table, on the artifacts and on the finals). A draft from last season is not visible or sendable. |
| The coordinator re-runs generation with `overwrite` | `generated_info_sheet` is rewritten per item and the packet files are replaced; the previous packet HTML is kept as `sponsor_packet_manual`. Drafts then report `packet_changed` until they are re-drafted. |

### 7.4 Operational edge cases

- Mail is not configured (`dispatch.enabled` false, or no credentials): the page renders the work list and the generation actions, but the send control is replaced by a "mail is not configured" notice naming the missing setting. The page never pretends a send can work.
- The `bcc_emails` list is empty: the send is blocked as
  `mail_not_configured` and names the setting. The archival copy of every
  sponsor mail is a program rule (D9), so a send without one is a
  configuration mistake, not a choice.
- No eligible sponsors at all: the page shows an empty state that points at matching, not at an error.
- A stale `sending` attempt: the recovery sweep turns it into `unknown`
  and the row shows `Check Gmail`. The sweep runs at worker start-up and
  before each dispatch list read, it only touches attempts older than
  the transport timeout plus a margin — so a send that is still in flight
  is never marked — and it writes the unknown-attempt **audit event in
  the same transaction** as the state change, so the sweep cannot bypass
  the audit rule. There is no automatic cleanup that would allow a blind
  resend.

---

## 8. Security, privacy, and audit

### 8.1 The send path is a data-egress boundary

The attachment carries a child's wish list, clothing and shoe sizes,
interests and notes. No other part of this system mails a child's
information sheet to a third party — the screening prompt, the
clarification prompt and the ZeroBounce checks transmit data, but not
the sheet — so this path is stricter than the rest:

- **Authorization**: `coordinator` or `admin` only, plus the CSRF header
  — the same bar as the existing dispatch endpoint
  (`routes/manual_match.py:372`). `auditor` and `read_only` see the work
  list but cannot draft or send.
- **Audit**: every send writes a `security_audit_events` row (actor,
  sponsor id, recipient, message id, attachment manifest, timestamp).
  Drafting and editing are audited too, at a lower level: the draft row
  keeps `created_by`/`edited_by`.
- **No PII in logs**: no address, subject line, body or attachment
  content in a log line. The request middleware already logs only method,
  path, status, user and roles; the send path keeps that property and
  the transport's exceptions are truncated to a short message before they
  are stored.
- **Attachments are read through the artifact registry**, whose
  `resolve_download` already refuses symlink escapes, and only `.pdf`
  content types are allowed (the same whitelist `SAFE_CONTENT_TYPES`
  uses).
- **Preview isolation**: the popup renders the body in an `<iframe
  sandbox="">` with `srcdoc`, so no script or stylesheet from the server
  HTML can run in the app's origin.
- **Irreversibility**: a send cannot be undone. The popup shows the full
  recipient address (not truncated) next to the Send button, and the
  button requires a deliberate click. There is no "send all" button.
- **Duplicate recipients**: two sponsor rows can carry the same address
  (the column is deliberately not unique). Both rows are flagged, and a
  send to a shared address names the affected angels in the confirmation,
  so a second, different packet never goes out to the same mailbox by
  accident.
- **Retention**: the draft body, the note and the attempt snapshots are
  new stored personal data. Their retention period is an owner decision
  (§14.9), and it has to be stated next to the existing retention gate in
  `docs/limitations.md`.

### 8.2 Credentials

- The Gmail **token** is a new file (`ccat_google_gmail_token.json`),
  added to `.gitignore` next to the Drive pair. The OAuth **client**
  file is the existing one and is reused unchanged.
- The container deployment gets **new** mounts: there are no Drive
  credential mounts in `docker-compose.yml` today to copy, so the Gmail
  client, the Gmail token and a writable, persistent directory for the
  refreshed token are introduced here (§13).
- The token file holds a refresh token: it is a long-lived credential and
  is chmod 600 on the host, as the Drive token is.
- The SMTP fallback reads its password from an environment variable
  named in the config (`smtp_password_env`), never from the JSON file.
- `dispatch.enabled` is false by default, so an unconfigured deployment
  cannot send mail by accident.

### 8.3 What the sponsor receives

The body contains the angel id, gender, age and drop-off location — the
same content the current template already puts in front of a coordinator
to copy by hand. The program addresses are BCC'd, never To'd, and the
coordinator cannot see the BCC list in the body the sponsor receives.

The attachment is unredacted by design (it is the point of the program).
What changes is that the copy is no longer manual, so the page shows
exactly which files are attached before the send, with their names and
sizes.

This is not the first time the system transmits a child's details — the
screening prompt, the clarification prompt and the ZeroBounce checks do
that, under approval gates recorded in `docs/limitations.md`. It is the
first time a child's **information sheet** is mailed to a third party,
and that is the difference the send path has to earn.

---

## 9. Performance and scale

- **Sheets**: the expensive part, unchanged. A LibreOffice conversion is
  ~1-3 s per child, `info_sheet.max_workers` (default 4) at a time. 200
  children is a few minutes in one job. The dispatch page never does this
  in a request.
- **Packets**: a merge and a template render per sponsor, fast. The
  packet phase stays inside the `generate` job.
- **Work list**: one row per sponsor with assigned angels. A season has
  tens to a few hundred sponsors. The list endpoint must not do N+1
  artifact lookups: the season's artifacts are fetched once and joined in
  memory, by `meta["sponsor_id"]` for the new rows and by the
  root-relative path prefix for the rows that predate the metadata.
- **Drafting**: a template render plus a row write per sponsor. 200
  drafts is well under a second, so `POST /dispatch/drafts` stays
  synchronous. If a season ever has thousands of sponsors, the same
  endpoint moves behind a job without changing the contract.
- **Sending**: one round trip per sponsor, plus reading ~1-3 MB of PDF.
  Fine for one request at a time; there is no bulk send. The transport
  runs in a worker thread (D18), so a slow provider does not stall the
  API's event loop, and the request has a timeout.
- **Known cost, not introduced here**: the sheet phase calls
  `repo.commit_finals("generate", "child", child_rows, …)` once per
  successful sheet, rewriting the whole `final_child` store each time
  (`db_generate.py:996`). That is O(n²) on a large season. The dispatch
  page makes generation runs more frequent, so this will be felt, but
  fixing it is a separate change (a per-row update) and is not in scope
  here.

## 10. Testing plan

### 10.1 Backend — new `test/test_db_dispatch.py`

- **Blockers**: a sponsor with no email, no packet, missing sheets, slots
  not filled, already sent, mail not configured — each produces the right
  blocker and the right HTTP status.
- **Drafting**: the rendered subject matches the existing
  `_SUBJECT_TEMPLATE`; the body contains the greeting, the angel table and
  the file names; the copy-paste header box is absent; the attachments
  list names the two PDFs and their sizes; the row is created once and
  re-drafting is idempotent unless `overwrite`.
- **Editing**: subject and note updates bump the version; a stale
  `If-Match` raises the version conflict; a note change re-renders the
  body from the manifest, so the angel table stays generated. The BCC
  list is not editable (D9).
- **Sending** (with a fake transport): the recipient, subject, body and
  attachment bytes are what the coordinator saw; on success the attempt
  is `sent` with the message id and `packets_dispatched_at` is stamped;
  on a rejection the attempt is `failed`, the error is short, and the
  sponsor is **not** stamped; a retry after a rejection sends again and
  succeeds.
- **Guards**: sending a sponsor that is already sent, that has no email,
  whose packet is missing, that is short of its quota, or while a `match`
  job is running — each is refused with its reason and nothing is sent.
- **Staleness**: a packet rebuilt after the draft makes the draft stale.

### 10.1b Backend — the failure windows, in `test/test_db_dispatch.py`

These are the tests that decide whether the design is safe. None of them
needs the network: the transport is reached through a
`_default_transport(cfg)` seam (the same shape as
`db_clarify._default_sender`), and the fake records every call.

| Test | What it proves |
|---|---|
| Two concurrent sends with the same draft version | Only one transport call happens; the other gets `409`. |
| The transport accepts, then the commit fails | The attempt stays `sending` (the write failed), the sponsor is not stamped, and the recovery sweep later marks it `unknown` — **no retry is offered**. |
| A timeout from the transport | The attempt is `unknown`, not `failed`. |
| A restart with a committed `sending` attempt | The recovery sweep turns it into `unknown`, never `failed`. |
| The packet is replaced while a send is in flight | The send uses the bytes it read and hashed, not the path reopened later. |
| The same artifact id with changed bytes | The per-attachment hash comparison refuses the send. The merged PDF and the instruction PDF are checked separately: changing either without changing the assignment or the source sheets still fails. |
| A pre-change artifact row with `meta` NULL or `{}` | The path fallback finds the sponsor's packet in both cases. |
| Assignments changed before the draft was written | The draft is refused as `packet_changed` (not only after drafting). |
| Edit the subject, send, it fails, retry | The retry uses the version the failure returned and sends the edited subject. |
| A historical-season URL | Every row, artifact and attempt stays in that season. |
| Two sponsor rows with one address | The send path returns the duplicate sponsor ids so the page can confirm. |
| The physical dispatch call | The sponsor's `row_version` from the list is the one submitted, and a stale one is refused. |
| An unknown or ambiguous child id | The job option is refused instead of generating nothing quietly. |
| A pipeline job enqueued while a send is in flight | `enqueue_job` refuses it; the reservation is the in-flight attempt. |
| An assignment or the sponsor's email changed between the first check and the reservation | The re-validation inside the reservation transaction aborts it: no attempt, nothing sent. |
| An audit save of the sponsor's email or ignore flag during a send | The audit write refuses while the attempt is in flight. |
| A reservation taken while a pipeline job is being enqueued | The send's transaction sees the active job and refuses. |
| A manual `assign` while a send is in flight | The manual matcher's commit refuses it, inside its own transaction. |
| A legacy packet with no manifest | The sponsor carries `manifest_missing` and cannot be drafted until the packet is rebuilt. |
| A packet built with the manifest | The manifest carries the angels with gender, age and drop-off, and the rendered body matches it. |
| A child that is already generated | The sheet is rebuilt (the marker is cleared before the pending filter). |

### 10.2 Backend — new `test/test_mail.py`

- The message builder sets From (with the display name), Reply-To, To,
  Bcc, Subject and the HTML body; each attachment is a part with the right
  filename and content type.
- The Gmail transport produces a base64url `raw` payload and sends it to
  `users.messages.send` with `userId="me"`; the body carries the From,
  To, every BCC, the Subject, the deterministic `Message-ID` header and
  one part per attachment; an expired
  token triggers a refresh and the refreshed token is written back
  atomically; a refresh failure surfaces a short error.
- The outcomes are classified: an authentication rejection is
  `rejected`, a timeout is `ambiguous`, and neither is reported as
  `sent`.

### 10.3 Backend — existing suites

- `test/test_db_generate.py`: the new `only` and `child` options — the
  sheets-only mode, the packets-only mode (a sponsor with a missing sheet
  is skipped and reported in `packets_blocked`), one child only, a bad
  `only` value raising `InfoSheetError`, `child` with `only="packets"`
  refused, an unknown child refused, and an ambiguous angel id refused.
  The combined run keeps its strict validation: `test=True` still skips
  the packet phase, and a missing sheet still fails the job.
- `test/test_api.py`: the six new endpoints; `read_only` and `auditor`
  are refused on the mutating ones; CSRF is enforced; a stale `If-Match`
  is a `412`; the existing guard that no clarify route sends still holds.
- `test/test_sponsor_packets.py`: unchanged — the packet file keeps its
  current shape.
- `test/test_config.py`: the new `dispatch` section, its defaults and its
  validation.
- `test/test_db_repo.py`: the `"dispatch"` write-rights entry — the module
  may write sponsors, may write only `packets_dispatched_at` on them, and
  may not write children or families.

### 10.4 Frontend — new `frontend/test/dispatch.test.ts`

- The work list renders one row per sponsor with the blocker text.
- "Generate sheets" and "Generate packets" enqueue a `generate` job with
  the right options and the page links to `/jobs/:id`.
- "Generate one" enqueues with `child` / `sponsor` / `only`.
- The draft popup opens with the draft's subject, recipient, BCC and
  attachments; editing the subject and saving issues a `PATCH` with the
  `If-Match` version.
- Send: clicking Send posts the typed subject and note with the version;
  success shows the sent state and the timestamp; a rejection shows the
  error and a Retry button; an unknown outcome shows `Check Gmail` and
  **no** retry; the send button is disabled while a send is in flight.
- A dirty popup sends the edited content, not the stored draft.
- The popup closes on Esc and on Cancel; a focus trap keeps Tab inside
  it; focus returns to the opener.

### 10.5 Frontend — existing suites

- `frontend/test/generate.test.ts` is replaced by the dispatch test.
- `frontend/test/helpers.ts` gains a `/dispatch` placeholder route.
- `frontend/test/run-forms.test.ts` and `run-menu.test.ts` keep passing
  unchanged (the new job options have defaults).

### 10.6 Gates

- `pre-commit run ruff-format --all-files`, `pre-commit run ruff
  --all-files`, `pre-commit run ty --all-files` — clean.
- `pytest` — the whole suite green, including the new files.
- `cd frontend && npx vitest run` — green.
- One manual rehearsal against a copy of the real database: generate
  sheets, build packets, draft, send one sponsor a real email with the two
  attachments, and confirm the sponsor row is stamped.

---

## 11. Implementation phases

Each phase ends in a state that is verifiable on its own.

**Phase 0 — decisions (no code).**
The owner answers §14: the sender, the transport and the OAuth project,
the resend rule, who may send, the physical-packet path, the BCC list,
the auto-matcher question, and the retention of the new stored bodies.
The account is chosen and its sending identity validated: a Gmail
account that can send as the program's From address, and an OAuth app
with the `gmail.send` scope. Gate: the decisions are written down and
the From address is one the account may send as. Nothing is authorized
yet — the consent needs the command from Phase 1.

**Phase 1 — the transport (no UI).**
The `MailTransport` protocol in `src/ccat/services/ports.py`, the Gmail
implementation in `src/ccat/mail.py` with the `sent` / `rejected` /
`ambiguous` outcomes, the thread-off-the-loop rule and the timeouts, and
the `ccat mail-authorize` command — plus `test/test_mail.py`.
Gate: the unit tests pass; `ccat mail-authorize` completes a consent on
a machine with a browser; a real send through the new module delivers a
message with two PDF attachments; and a forced timeout produces
`ambiguous`.

**Phase 2 — storage.**
The Alembic revision with `sponsor_drafts` and `sponsor_send_attempts`,
the `"dispatch"` write-rights entries, the dedicated `commit_dispatch`
repository method, the artifact roles and the packet build manifest
(D16). Gate: the migration applies to a copy of the production
database; a packet build writes a manifest, the manifest's fingerprint
changes when the assignment or a source sheet changes, and each
artifact's own hash is recorded separately so a changed merged PDF is
detected too.

**Phase 3 — the dispatch service and the routes.**
`db_dispatch.py` (list, children, draft, edit, send), the results, the
DTOs and the six routes, plus `test/test_db_dispatch.py`.
Gate: the backend suite is green; the endpoints work against a local
database with a fake transport, including every failure window in
§10.1b.

**Phase 4 — the API contract.**
`scripts/export_openapi.py` and `npm run gen:api`, so the frontend has
the types. Gate: `openapi.json` and `schema.d.ts` are current and
committed.

**Phase 5 — the generation options.**
`only` and `child` in the `generate` job, the packets-only skip (D14),
and the `child` selection rules.
Gate: `pytest` green; a job with `only="packets"` builds only packets
and skips a sponsor with a missing sheet; `child="<zite_record_id>"`
builds exactly one sheet and refuses an unknown or ambiguous id.

**Phase 6 — the page.**
The `/dispatch` route, the nav entry, the home card, the `/generate`
redirect, `DispatchView.vue`, `api/dispatch.ts`, the work list and the
generation actions.
Gate: `npx vitest run` green; the page lists sponsors and starts jobs.

**Phase 7 — the email popup.**
`SponsorEmailModal.vue`, the draft and send calls, the send button, the
three outcomes (`sent`, `failed`, `unknown`), the retry path.
Gate: the frontend suite is green; a coordinator can draft a sponsor and
send it with the attachments.

**Phase 8 — rehearsal and documentation.**
Run the whole flow against a copy of the real database, including a
forced timeout (to see the `unknown` path) and a duplicate recipient.
Update `pipeline_architecture.md`, `docs/limitations.md`,
`docs/deployment.md` and the README **in the same change** as the
behavior they describe, not afterwards.

---

## 12. Blast radius — what this change could break

| Change | What breaks | Mitigation |
|---|---|---|
| `/generate` route removed | Old bookmarks, the home card, `frontend/test/generate.test.ts`, `frontend/test/helpers.ts` (which has a `/generate` placeholder route) | Keep `/generate` as a redirect to `/dispatch` (D11); update the nav, the home card and the tests. |
| `GenerateView.vue` deleted | Nothing else imports it (only `router/index.ts` and its own test) | Delete the test with it. |
| `generate` job gains `only` and `child` | `ManualMatchView.vue:793-794` also enqueues a `generate` job; `frontend/test/run-forms.test.ts` and `run-menu.test.ts` assert its payload | Defaults keep today's behavior (`only="both"`, `child=None`); the existing payload assertions stay valid. |
| `sponsor_packets.email_template_html_file` template edited (the copy-paste header box becomes conditional) | The packet HTML artifact changes; `test/test_sponsor_packets.py` asserts on the rendered HTML | The box is dropped only in the draft rendering, so the packet file keeps its current shape and the existing assertions pass. |
| New `"dispatch"` module in the write-rights map | `db/repositories.py` `WRITABLE_ENTITIES` / `ALLOWED_CHANGES` | The new entry grants only `packets_dispatched_at` on sponsors; no existing module's rights change. |
| `register_file` gains `meta={"sponsor_id": ...}` for packet artifacts | Anything reading `artifacts.meta` expecting it to be empty | `meta` is already a free-form JSON column; only new rows carry the keys. |
| New `sponsor_drafts` and `sponsor_send_attempts` tables + Alembic revision | The alembic head moves; a deployed database needs `ccat db-upgrade` | A new revision after `20261121_0008`; the cutover checklist already covers `db-upgrade`. |
| New `"dispatch"` entry in `WRITABLE_ENTITIES` | A module absent from an entity's set may not write that entity at all | The entry grants sponsors only, and `ALLOWED_CHANGES` limits it to `packets_dispatched_at`. |
| `dispatch` config section added to `Config` | `Config` is `extra="forbid"`; a config file with an unknown key fails validation | The section has defaults; `config/ccat_2026.json` and `config/ccat_docker.json` each get a `dispatch` block with `enabled: false`. |
| A new outbound-mail capability | `docs/limitations.md` says "no automatic email sending"; `test/test_api.py:1800-1806` asserts no clarify route sends | The new routes live under `/dispatch`, so the clarify guard still holds; the limitations line is rewritten (§13). |
| `packets_dispatched_at` written by a second module | Anything that assumed only `manual_match` sets it | The column keeps one meaning ("went out"); the manual-match endpoint still refuses to dispatch an already-dispatched sponsor. |
| OAuth scope work for Gmail | The Drive token must not be touched | The existing client credentials are reused; only the **token** is separate; `gdrive.py` is not modified. |

---

## 13. Documentation updates

| Document | Change |
|---|---|
| `pipeline_architecture.md` | A new section for the dispatch phase: the packet email is now composed and sent by the app, the draft and attempt tables, the transport, the manifest, and what `packets_dispatched_at` does and does not freeze. §13b ("the email draft names both PDF attachments so the body always matches the files in the directory") is amended to say the app attaches them. Appendix A.3 gains the dispatch scope note. |
| `docs/limitations.md` | "No automatic email sending. Clarification drafts are generated and edited, never sent" becomes "Clarification drafts are never sent; sponsor packet emails are composed and sent from the dispatch page". The clarify half of the decision is unchanged, and the doc must say so explicitly. The retention gate gains the new stored email bodies and attempt records. |
| `README.md` | The command list and the web-application section mention the `/dispatch` page and the `ccat mail-authorize` command; the architecture summary's implication about email is updated. |
| `docs/deployment.md` | The Gmail credential and token mounts, the writable token path for the refreshed token, the OAuth consent step, and what happens when the refresh token expires. |
| `docs/limitations.md` (open decisions) | The Gmail OAuth verification and the sender identity as operator gates, alongside the existing ones. |
| `docker-compose.yml`, `Dockerfile` | New mounts for the Gmail client and token, and a writable, persistent path for the refreshed token, so a recreated container does not lose it. There are no Drive credential mounts to copy — these are new. |
| `config/ccat_2026.json`, `config/ccat_docker.json` | The `dispatch` block in both (the docker config is the deployed one). |
| `.gitignore` | The new Gmail **token** file name (the client file name is already ignored). |

---

## 14. Open questions for the human owner

These are policy, not engineering. A coder should not decide them.

1. **The sender.** Which Gmail account sends, and what is the display
   name and reply-to address the sponsors see? The plan reads
   `dispatch.sender_email` / `sender_name` / `reply_to` from the config
   and never infers them.
2. **The transport.** Gmail API with the existing OAuth client and a
   separate token (the plan's recommendation) or SMTP with an app
   password? The OAuth route needs one consent with the `gmail.send`
   scope; the SMTP route needs 2FA on the account and an app password.
   If the deployment host cannot run a desktop OAuth consent, that
   decides it. The Google verification state (Testing vs published, and
   the seven-day refresh tokens that come with Testing) is part of this
   decision.
3. **Resending.** If a sponsor says they never got the packet, can the
   coordinator resend from this page, or is that a manual action in
   Gmail? The plan blocks a second send; a resend is a policy call.
4. **Who may send.** The plan gives `coordinator` and `admin` the send
   button. If sending sponsor mail is too sharp for a coordinator, it
   could be admin-only, with the coordinator drafting and an admin
   releasing. Today the same role can place a family by hand, so
   coordinator is defensible.
5. **Physical packets.** Sponsors with `physical_packet` get no email.
   Confirm that the page's "Mark dispatched" button is the right way to
   close those out, and that the filled-slot precondition is right for
   them too.
6. **The BCC list.** `sponsor_packets.bcc_emails` has three program
   addresses today. Confirm they are the right ones to BCC on every
   sponsor email, and whether a sponsor should ever see them.
7. **The auto-send question.** The plan sends one email per click. If
   the season ever has hundreds of sponsors and the coordinator wants
   "draft all, review all, send all", that is a deliberate change to the
   no-bulk-send rule in §2 and needs the same sign-off.
8. **What "dispatched" freezes.** Today only the manual matcher refuses
   to move a dispatched sponsor's children. An automatic match run after
   a send can still move them, and then the emailed packet no longer
   matches the assignment. Either the automatic matcher learns to skip
   dispatched sponsors (a small change in the match path, with tests), or
   the coordinator accepts that risk and the page stops implying a lock.
   The plan does not build either until this is answered.
9. **Retention of the new personal data.** The draft body, the note and
   the attempt snapshots are stored copies of what was mailed to a
   sponsor. They need a retention period — the same gate that already
   covers generated packets and logs in `docs/limitations.md` — and a
   decision on whether an attempt row's body snapshot is kept at all
   after the season closes.

---

## Appendix A — the change list, file by file

### New files

| File | What |
|---|---|
| `src/ccat/mail.py` | The Gmail transport, the message building, the three outcomes, the timeouts. |
| `src/ccat/services/db_dispatch.py` | `list_dispatch_rows`, `list_dispatch_children`, `draft_sponsors`, `get_draft`, `update_draft`, `send_draft`. |
| `src/ccat/api/routes/dispatch.py` | The six routes. |
| `alembic/versions/<rev>_sponsor_dispatch.py` | The `sponsor_drafts` and `sponsor_send_attempts` tables. |
| `frontend/src/views/DispatchView.vue` | The page. |
| `frontend/src/api/dispatch.ts` | The API client for the dispatch routes. |
| `frontend/src/components/DispatchGenerationBar.vue` | The generation card. |
| `frontend/src/components/DispatchSponsorTable.vue` | The work list. |
| `frontend/src/components/SponsorEmailModal.vue` | The email popup. |
| `frontend/src/utils/sponsor.ts` | The sponsor helpers extracted from `SponsorCardList.vue`, shared by the match and dispatch views. |
| `test/test_db_dispatch.py` | Service tests. |
| `test/test_mail.py` | Transport tests. |
| `frontend/test/dispatch.test.ts` | Page tests. |

### Modified files

| File | Change |
|---|---|
| `src/ccat/config.py` | `DispatchSpec` and `Config.dispatch`. |
| `src/ccat/db/models.py` | The `SponsorDraft` and `SponsorSendAttempt` models. |
| `src/ccat/db/repositories.py` | `"dispatch"` write rights (both maps); `commit_dispatch`; the draft and attempt methods. |
| `src/ccat/services/ports.py` | The `MailTransport` protocol. |
| `src/ccat/services/results.py` | `DispatchResult`, `DispatchSendResult`; `GenerateResult.packets_blocked`. |
| `src/ccat/services/db_generate.py` | The `only` and `child` options; the packets-only skip; the packet build manifest (`manifest.json` beside the packet files) and the artifact roles. |
| `src/ccat/api/dto.py` | The dispatch DTOs. |
| `src/ccat/api/__init__.py` | Mount the dispatch router. |
| `src/ccat/__init__.py` | The `ccat mail-authorize` command. |
| `src/ccat/jobs/service.py`, `src/ccat/services/db_manual_match.py` | The write-time reservation checks: a pipeline job is refused while a send is in flight, and a manual assignment is refused the same way (D18). |
| `data/sponsor_packet_email_2026.html` | The header box becomes conditional; the note slot. |
| `config/ccat_2026.json`, `config/ccat_docker.json` | The `dispatch` block. |
| `frontend/src/router/index.ts` | The `/dispatch` route and the `/generate` redirect. |
| `frontend/src/App.vue` | The nav entry. |
| `frontend/src/views/HomeView.vue` | The home card. |
| `frontend/src/api/schema.d.ts`, `frontend/openapi.json` | Regenerated. |
| `frontend/test/helpers.ts` | The `/dispatch` placeholder route. |
| `.gitignore` | The new Gmail **token** file name (the client file name is already ignored). |
| `docker-compose.yml`, `Dockerfile` | The Gmail credential and token mounts; a persistent writable token path. |
| `docs/deployment.md`, `docs/limitations.md`, `pipeline_architecture.md`, `README.md` | The documentation updates (§13). |

### Deleted files

| File | Why |
|---|---|
| `frontend/src/views/GenerateView.vue` | Replaced by `DispatchView.vue` (D11). |
| `frontend/test/generate.test.ts` | Replaced by `frontend/test/dispatch.test.ts`. |

---

## Appendix B — terminology

| Term | Meaning |
|---|---|
| **Info sheet** | The per-child PDF rendered from the `.ott` template. One per child. |
| **Packet** | The per-sponsor directory holding the merged child PDF, the instruction PDF and the draft HTML. |
| **Draft** | The stored, editable email for one sponsor: subject, generated body, note, BCC, attachment list. Not sent. |
| **Send** | Handing one draft to the mail transport with its attachments. It creates an **attempt**. |
| **Attempt** | One append-only send attempt: what was sent, to whom, with which attachment hashes, and what came back. |
| **Rejected** | The provider refused the message. It was not accepted, and a retry is safe. |
| **Ambiguous / unknown** | The provider's answer does not say whether the message was accepted. No automatic retry, ever. |
| **Dispatched** | `final_sponsor.packets_dispatched_at` is set — the packet went out. The manual matcher then refuses to move that sponsor's children; the automatic matcher does not (see D3 and §14.8). |
| **Manifest** | The record of one packet build: which children, which sheets, which hashes. The draft is rendered from it, and the send checks it. |
| **Blocked** | A sponsor that cannot be drafted or sent yet, with the reason (no email, no packet, missing sheets, quota not filled, already sent, packet changed, manifest missing, mail not configured, a pipeline job running). |
| **Ready** | A sponsor with a packet, a usable recipient, a filled quota and no blockers. |
