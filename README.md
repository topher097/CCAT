# CCAT — Copper Country Angel Tree pipeline

Workbook pipeline for the CCAT gift-giving program. Full behavior and writer
ownership are documented in `pipeline_architecture.md`.

## Setup

```sh
direnv allow        # devbox + uv environment (.envrc)
uv sync             # create .venv and install dependencies
```

## Storage model

One Excel workbook holds three layers:

- `raw_sponsor`, `raw_family`, `raw_child`: replaceable verbatim Zite source.
- `final_sponsor`, `final_family`, `final_child`: complete persistent,
  editable entity stores. Ignored, orphaned, duplicate, raw-missing, and
  pending entities remain stored.
- Operational logs plus `match_report`: process history and match reporting.

There are no active `*_submissions` or `adj_*` sheets. **Raw is source; final
is truth; logs are history.**

## Usage

```sh
ccat acquire       [--config PATH]  # fetch Zite tables verbatim -> raw_*
ccat screen        [--config PATH] [--redo ID] [--test] [--overwrite]
                   [--skip-email-check] [--skip-address-check]
                   # prepare raw in memory, append/merge full finals,
                   # refresh duplicate/DOB diagnostics and validation logs,
                   # then screen only children with screened=false
ccat audit         [--config PATH] [--test] [--restart]
                   # NiceGUI editor for complete final stores
ccat match         [--config PATH] [--test] [--overwrite] [--skip-match]
                   # patch assignment fields; archive/report as configured
ccat generate      [--config PATH] [--test] [--overwrite]
                   [--sponsor ID] [--max-workers N]
                   # child information PDFs, generation markers, packets
ccat stats         [--config PATH] [--raw]
ccat clarify       [--config PATH] [--generate] [--overwrite]
ccat clarify-export [--config PATH] [--output PATH]
```

`--test` means the first unit of work: first family for screening, first
sponsor for match, first PDF for generate. `--overwrite` is an explicit
rework control, not blanket data replacement: human edits, controls, and
module-owned fields retain their protections. Failed or interrupted AI/PDF
work remains retryable; completed work remains complete.

Duplicates are flagged, never merged. Users resolve extras in audit.
ZeroBounce and Nominatim reuse their logs, so reruns do not repeat successful
external checks; complete verbatim responses remain in
`email_validation_log` and `address_validation_log`.

## Migration

- `ccat audit-migrate --config CONFIG --from OLD [--force]` bootstraps an
  old v1 workbook directly into persistent finals.
- `ccat consolidate-migrate --config CONFIG --from OLD --output NEW
  [--force --backup BACKUP] [--report REPORT]` converts a full v2 `adj_*`
  workbook into a separate persistent workbook. It never mutates the input,
  refuses output replacement without force plus a readable backup, and
  writes a JSON validation report.

For production cutover: back up workbook/config outside the live path, run
conversion, inspect the report, update `excel_file` in a validated config or
`DEFAULT_CONFIG`, and retain the old workbook read-only. Rollback restores
the original config/workbook; never feed full finals to old projection code.
