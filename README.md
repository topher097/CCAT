# CCAT — Copper Country Angel Tree pipeline

Five-stage data pipeline for the CCAT gift-giving program (see
`pipeline_architecture.md` for the full design).

## Setup

```sh
direnv allow        # devbox + uv environment (.envrc)
uv sync             # create .venv and install dependencies
```

## Usage

Each module runs independently against one Excel workbook and config:

```sh
ccat acquire      [--config PATH]          # 1: pull/validate Zite data -> raw_* + submissions sheets
ccat screen       [--config PATH] [--test] # 2: LLM adjudication -> dense adj_child
ccat audit        [--config PATH] [--test] # 3: human audit NiceGUI app -> adj_* + final_child
ccat match        [--config PATH] [--test] # 4: matchmaking -> adj_* assignments + final_family/sponsor
ccat generate     [--config PATH] [--test] # 5: child info sheet PDFs
ccat audit-migrate --config config/ccat_2026_v2.json --from ./output/ccat_2026.xlsx
                                           # one-time v2 workbook bootstrap
```

Default config: `config/ccat_2026.json`. `--test` stops each module after
its first unit of work (first family, first sponsor, first PDF).

**v2 audit workbook** (`config/ccat_2026_v2.json` →
`output/ccat_2026_v2.xlsx`): the multi-entity human audit (sponsors,
families, children — dense `adj_*` sheets, derived per-row `status`,
`final_*` projections) runs against the v2 workbook only.  The old
workbook is never written by the v2 system (its `ccat audit` refuses a
pre-v2 `adj_child` schema).  Bootstrap the v2 workbook once with
`ccat audit-migrate --config config/ccat_2026_v2.json --from
./output/ccat_2026.xlsx` (the old workbook is opened read-only), then
run `ccat audit --config config/ccat_2026_v2.json`.

**Cutover**: the `DEFAULT_CONFIG` environment variable (exported in
`.envrc`) now points at `config/ccat_2026_v2.json`, so every `ccat`
command without an explicit `--config` uses the v2 workbook.  The
repo-side fallback in `ccat.__init__` stays `config/ccat_2026.json`
until the switch is committed; tests pin the fallback regardless of
the environment.

`--overwrite` (screen/match/generate) ignores previously generated
output: screening re-screens every child (preserving human edits and
controls), matchmaking replaces its outputs in place instead of
archiving, and info sheet generation re-renders every pending audited
PDF regardless of the `generated_info_sheet` column.  The audit tool's
`audited` checkbox is always revocable — there are no frozen rows, and
completed-hiding is a list filter in the UI.

Module 5 renders one `<angel_id>.pdf` per pending child from
`data/child_information_sheet_2026.ott` into `output/child_info_sheets/`,
requires headless LibreOffice (`soffice`), and tracks progress in the
`generated_info_sheet` flag of `adj_child` (only audited children whose
family has an assigned sponsor and a `FALSE` flag are processed; the
`final_child` projection refreshes in the same atomic write).  Sheets
render and convert on a thread pool
(`--max-workers N`, default `info_sheet.max_workers` = 4) with a `tqdm`
progress bar; Ctrl+C cancels cleanly — finished sheets are recorded, the
rest stay pending for the next run.  The template's sponsor section is
filled from the child's assigned sponsor in `final_sponsor` when one
exists, and left blank for hand-filling otherwise.
