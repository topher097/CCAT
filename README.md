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
ccat acquire  [--config PATH]                 # 1: pull/validate Zite data -> raw_* sheets
ccat screen   [--config PATH] [--test]        # 2: LLM adjudication -> adj_child
ccat audit    [--config PATH] [--test]        # 3: human audit web tool -> final_child
ccat match    [--config PATH] [--test]        # 4: matchmaking -> final_family/sponsor
ccat generate [--config PATH] [--test]        # 5: child info sheet PDFs
```

Default config: `config/ccat_2026.json`. `--test` stops each module after
its first unit of work (first family, first sponsor, first PDF).
`--overwrite` (screen/audit/match/generate) ignores previously generated
output: screening replaces every `adj_child` row; the audit tool
re-queues audited children and allows re-saving frozen rows; matchmaking
ignores the `audited` column and replaces its outputs in place instead
of archiving; info sheet generation re-renders every PDF regardless of
the `generated_info_sheet` column.

Module 5 renders one `<angel_id>.pdf` per pending child from
`data/child_information_sheet_2026.ott` into `output/child_info_sheets/`,
requires headless LibreOffice (`soffice`), and tracks progress in the
`generated_info_sheet` column of `final_child` (only `FALSE` rows are
processed).  The template's sponsor section is filled from the child's
assigned sponsor in `final_sponsor` when one exists, and left blank for
hand-filling otherwise.
