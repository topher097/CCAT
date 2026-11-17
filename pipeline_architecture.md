# CCAT Pipeline Architecture

Architecture guide for the CCAT gift-giving matching system. This document is the
basis of all code in this codebase. No implementation exists yet; every section below
is the contract that the implementation must satisfy.

## 1. Purpose and Scope

CCAT matches families of children ("angels") with sponsors for a holiday gift
giving program. The pipeline has four modules, each runnable independently:

1. **Data Acquisition and Validation** — pull sponsor/family/child tables from
   Zite, validate, clean, assign IDs, persist raw sheets.
2. **AI Screening** — adjudicate each child row with an LLM, flag anomalies,
   persist `adj_child` (sparse, new rows only).
3. **Human Audit Tool** — lightweight local web tool; human reviews raw vs.
   adjudicated child data and commits audited rows to `final_child`.
4. **Matchmaking** — assign whole families to sponsors (special-needs
   sponsors first, then individuals, then groups/organizations),
   honoring exact child counts and preference priority; writes `final_family`
   and `final_sponsor`.

All inter-module data exchange happens through one Excel workbook. All
configuration lives in one JSON file.

## 2. System Overview

```
                 config/ccat_2026.json
                         |
                         v
  [Zite API] --> (1) Acquire ---> raw_sponsor / raw_family / raw_child sheets
                                        |
                                        v
                  (2) Screen ----------> adj_child sheet   (new zite_record_ids only)
                                        |
                                        v
                  (3) Audit  ----------> final_child sheet  (human in the loop)
                                        |
                                        v
                  (4) Match  ----------> final_family / final_sponsor / match_report
                                        |
                                        v
                  (5) Generate --------> output/child_info_sheets/AT<season>_<angel_id>.pdf
                                        |
                                        v
                  (5b) Sponsor packets > output/sponsor_packets/<sponsor_id>/
                                         (email.html draft + combined packet PDF)
```

- Modules run individually via CLI (`ccat acquire`, `ccat screen`, `ccat audit`,
  `ccat match`, `ccat generate`, `ccat sponsor_packets`, `ccat stats`), each
  taking an optional `--config` path (default `config/ccat_2026.json`);
  screen/audit/match/generate accept `--test` and `--overwrite` (§6.3, §7.2,
  §8.6, §13.4); match also `--skip-match` (§8.7); audit also `--restart`
  (§7.4); stats also `--raw` (§14); sponsor_packets `--overwrite` (§13b).
- `final_*` sheets are ground truth: only their owning module writes them, all
  other modules treat them read-only (see §9 Ownership Matrix).

## 3. Configuration File

Path: `config/ccat_2026.json` (checked into repo; contains no secrets — the Zite
API key stays in the `ZITE_API_KEY` environment variable).

Structure:

```jsonc
{
  // Season identifier, used for file naming.
  "season": "2026",

  // Christmas date, MM/DD/YYYY. Used to compute age_at_xmas.
  "xmas_date": "12/25/2026",

  // Excel workbook location.
  "excel_file": "../IO/ccat_2026.xlsx",

  // Zite table references (dashboard URLs or DATABASE_ID::TABLE_ID/NAME),
  // consumed by src/ccat/zite.py get_table_data().
  "zite": {
    "sponsor": "TODO",
    "family": "TODO",
    "child": "TODO"
  },

  // Column schema per table. Keys are the lower snake_case names used
  // downstream (code, Excel headers, adj/final sheets).
  // Import rules:
  //   - Zite column names are auto-normalized to lower snake_case.
  //   - "rename": maps the source column to this key instead of its
  //     auto-normalized name (semantic rename or disambiguation).
  //   - "type": pandera check applied after cleaning.
  //   - "default": value substituted for null before validation.
  // Only listed columns are imported (allow-list); all others are dropped.
  "tables": {
    "sponsor": {
      "columns": {
        "zite_record_id":           { "type": "string" },                    // Zite system `id`
        "created":                   { "type": "datetime" },                  // Zite system `createdAt`; confirmed for sponsor_id ordering (§12 item 2 resolved)
        "updated":                   { "type": "datetime", "rename": "Updated time" },
        "email":                     { "type": "email" },
        "accepts_disclaimer":       { "type": "bool",    "rename": "I Accept Data Disclaimer" },
        "sponsor_type":              { "type": "enum",   "rename": "Affiliation", "values": "sponsor_affiliation" },
        "organization_name":         { "type": "string", "default": null },
        "prior_assigned_angel_ids":  { "type": "string", "rename": "Assigned Angel ID Code", "default": null },
        "first_name":               { "type": "name" },
        "last_name":                { "type": "name" },
        "phone":                     { "type": "phone_us", "rename": "Phone Number" },
        "num_children":              { "type": "int",    "rename": "Number of Angels to Sponsor", "default": 1, "values": "sponsor_num_children" }, // single_select: string "1".."25", cast to int
        "age_preference":            { "type": "enum",   "rename": "Age Preference", "values": "sponsor_age_preference", "default": "Any Age" },
        "gender_preference":         { "type": "enum",   "rename": "Gender Preference", "values": "sponsor_gender_preference", "default": "Any Gender" },
        "dropoff_location":          { "type": "enum",   "rename": "Gift Drop-Off Location Preference", "values": "sponsor_dropoff_location", "default": null },
        "source":                    { "type": "string" }
      }
    },
    "family": {
      "columns": {
        "zite_record_id":        { "type": "string" },                        // Zite system `id`
        "zite_created_at":       { "type": "datetime" },                      // Zite system `createdAt`; cross-checked vs `created`
        "created":               { "type": "datetime", "rename": "Created time" },
        "primary_email":         { "type": "email",  "rename": "Email" },      // link key for children + dedup key
        "accepts_disclaimer":    { "type": "bool",    "rename": "I Accept Data Disclaimer" },
        "first_name":            { "type": "name" },
        "last_name":             { "type": "name" },
        "phone":                 { "type": "phone_us", "rename": "Phone Number" },
        "street_address":        { "type": "string" },
        "apt_floor":             { "type": "string", "rename": "Apt / Floor", "default": null },
        "city":                  { "type": "string" },
        "zip_code":              { "type": "string" },                         // string, not int: preserves leading zeros
        "state":                 { "type": "enum",   "values": "family_state" }, // full state list; Michigan expected in practice
        "secondary_first_name":  { "type": "name",     "rename": "First Name (secondary)", "default": null },
        "secondary_last_name":   { "type": "name",     "rename": "Last Name (secondary)", "default": null },
        "secondary_email":       { "type": "email",    "rename": "Email (secondary)", "default": null },
        "secondary_phone":       { "type": "phone_us", "rename": "Phone Number (secondary)", "default": null },
        "gift_pickup_location":  { "type": "enum",   "rename": "Gift Pickup Location Preference", "values": "family_gift_pickup_location" },
        "source":                { "type": "string" }
      }
    },
    "child": {
      "columns": {
        "zite_record_id":      { "type": "string" },                           // Zite system `id`
        "zite_created_at":     { "type": "datetime" },                          // Zite system `createdAt`; cross-checked vs `created`
        "created":             { "type": "datetime", "rename": "Creation Time" },
        "first_name":          { "type": "name" },
        "primary_email":       { "type": "email",  "rename": "Primary Email" }, // source is single_line_text, still validated as email; link to family
        "dob":                 { "type": "date",   "rename": "Date of Birth" },
        "age_reported":        { "type": "int",    "rename": "Age at Christmas 2026", "default": null }, // source is free single_line_text; cross-checked vs computed age_at_xmas
        "gender":              { "type": "enum",   "values": "child_gender" },
        "need_1":              { "type": "string", "rename": "Need #1" },
        "need_2":              { "type": "string", "rename": "Need #2", "default": null },
        "desire_1":            { "type": "string", "rename": "Desire #1" },
        "desire_2":            { "type": "string", "rename": "Desire #2", "default": null },
        "shoe_size":           { "type": "enum",   "rename": "Shoe Size", "values": "child_shoe_size" },
        "shoe_size_type":      { "type": "enum",   "rename": "Shoe Size Type", "values": "child_shoe_size_type" },
        "pant_waist_size":     { "type": "enum",   "rename": "Pant Waist Size", "values": "child_pant_waist_size" },
        "pant_inseam":         { "type": "enum",   "rename": "Pant Inseam", "values": "child_pant_inseam" },
        "pant_size_letter":    { "type": "enum",   "rename": "Pant Size (Letter)", "values": "child_pant_size_letter" },
        "pant_size_type":      { "type": "enum",   "rename": "Pant Size Type", "values": "child_pant_size_type" },
        "jean_size":           { "type": "string", "rename": "Jean Size" },     // free text (no fixed vocabulary)
        "shirt_size_letter":   { "type": "enum",   "rename": "Shirt Size (Letter)", "values": "child_shirt_size_letter" },
        "shirt_size_type":     { "type": "enum",   "rename": "Shirt Size Type", "values": "child_shirt_size_type" },
        "favorite_colors":     { "type": "string", "rename": "Favorite Color(s)" },
        "interests":           { "type": "string" },
        "other_notes":         { "type": "string", "rename": "Other Notes", "default": null },
        "source":              { "type": "string" }
      }
    }
  },

  // Derived columns computed at acquisition (not stored in Zite):
  //   child.age_at_xmas                 <- dob + xmas_date (the source column
  //                                       "Age at Christmas 2026" is imported
  //                                       as age_reported for cross-check only)
  //   sponsor.pref_age_min / pref_age_max <- mapped from age_preference (§5.5)
  //   family.family_id, sponsor.sponsor_id, child.angel_id  (see §5.5)

  // Canonical vocabularies for every single_select field, transcribed from
  // the Zite form schema export ("Copper Country Angel Tree - 2026.json").
  // Enum-typed columns above reference these by name; see Appendix A.
  "vocabularies": {
    "sponsor_affiliation":   ["Individual", "Organization", "Walmart"],
    "sponsor_num_children":  ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "15", "20", "25"],
    "sponsor_age_preference": [
      "Any Age", "Newborn through Toddler (0-3 yrs)",
      "Pre-K through Grade 5 (4-10 yrs)", "Middle School (11-13 yrs)",
      "High School (14-18+ yrs)", "Special Needs (any age)"
    ],
    "sponsor_gender_preference": ["Any Gender", "Male", "Female"],
    "sponsor_dropoff_location": ["Hancock", "Any Location", "Calumet"],
    "family_gift_pickup_location": ["TSA (Hancock)", "CCAM (Calumet)", "Hancock", "Calumet"],
    "child_gender": ["Male", "Female", "Non-binary"],
    "child_shoe_size": "0.5 through 16 in 0.5 steps, plus 'not applicable' (Appendix A)",
    "child_shoe_size_type": ["Baby/Infant", "Youth", "Men's", "Women's", "Not Applicable"],
    "child_pant_waist_size": "Newborn (NB) through 18-24 Months, 2T-5T, 5\" through 44\" (Appendix A)",
    "child_pant_inseam": "N/A (Baby/Infant), 14\" through 36\" (Appendix A)",
    "child_pant_size_letter": "Newborn (NB) through 18-24 Months, XXS through 3X, 'not applicable' (Appendix A)",
    "child_pant_size_type": ["Baby/Infant", "Youth", "Adult", "not applicable"],
    "child_shirt_size_letter": "Newborn (NB) through 18-24 Months, XXS through 3X, 'not applicable' (Appendix A)",
    "child_shirt_size_type": ["Baby/Infant", "Youth", "Adult", "not applicable"]
    // family_state: full 50-state + DC list; all values accepted (see Appendix A)
  },

  // Location canonicalization for matchmaking (§8.3): both sides of a match
  // are reduced to one of these buckets before comparison.
  "location_groups": {
    "hancock": ["Hancock", "TSA (Hancock)"],
    "calumet": ["Calumet", "CCAM (Calumet)"]
  },

  // AI screening (module 2), consumed by the opencode python SDK.
  // system_prompt: full text in Appendix B; stored verbatim here.
  "ai_screening": {
    "model": "TODO",
    "system_prompt": "<Appendix B text>",
    "temperature": 0.0
  },

  // Screening pre-audit (module 2). Same-family children born at most
  // this many days apart are recorded in dob_conflict_log (§6.6).
  "screening": {
    "sibling_dob_max_gap_days": 274
  },

  // ZeroBounce email deliverability check (module 2, §6.7).
  "email_check": {
    "enabled": true,
    "delay": 0.25,
    "api_key_env": "ZEROBOUNCE_API_KEY"
  },

  // Nominatim family-address validation (module 2, §6.8).
  "address_check": {
    "enabled": true,
    "base_url": "https://nominatim.openstreetmap.org",
    "delay": 1.1,
    "user_agent": "ccat-angel-tree/1.0 (family address validation)",
    "timeout": 30.0
  },

  // Deprecated (module 1 only fetches now); kept so old configs load.
  "acquire": {},

  // Human audit tool (module 3).
  "audit": {
    "host": "127.0.0.1",
    "port": 8642
  },

  // Matchmaking (module 4). Weights are lexicographic tiers, not additive.
  "matchmaking": {
    "location_weight": 1000,
    "age_weight": 100,
    "gender_weight": 1
  },

  // Child info sheet generation (module 5). `mappings` maps each py3o
  // template variable in the .ott onto a column key of the enriched
  // final_child row (§13); template_file is the LibreOffice template;
  // max_workers is the thread-pool size for render+convert (§13.4).
  "info_sheet": {
    "template_file": "./data/child_information_sheet_2026.ott",
    "output_dir": "./output/child_info_sheets",
    "soffice_binary": "soffice",
    "max_workers": 4,
    "mappings": {
      "info.angel_id": "angel_id",
      "info.age": "age_at_xmas",
      "info.gender": "gender",
      "info.family_size": "family_size",
      "info.drop_off_location": "gift_pickup_location",
      "info.sponsor_id": "assigned_sponsor_id",
      "entry.colors": "favorite_colors",
      "entry.interests": "interests",
      "entry.other_info": "other_notes",
      "entry.need_1": "need_1",
      "entry.need_2": "need_2",
      "entry.desire_1": "desire_1",
      "entry.desire_2": "desire_2",
      "entry.footwear.size": "shoe_size",
      "entry.footwear.size_type": "shoe_size_type",
      "entry.pants.waist_in": "pant_waist_size",
      "entry.pants.inseam_in": "pant_inseam",
      "entry.pants.size_letter": "pant_size_letter",
      "entry.pants.size_type": "pant_size_type",
      "entry.pants.jean_size": "jean_size",
      "entry.tops.size_letter": "shirt_size_letter",
      "entry.tops.size_type": "shirt_size_type",

      // sponsor.* fills from the child's assigned final_sponsor row (§13);
      // children without an assigned sponsor render these blank.
      "sponsor.full_name": "sponsor_name",
      "sponsor.email": "sponsor_email",
      "sponsor.phone": "sponsor_phone",
      "sponsor.sponsor_id": "assigned_sponsor_id",
      "sponsor.angel_id": "angel_id",
      "sponsor.family_size": "family_size"
    }
  },

  // Sponsor packets (module 5b). One <sponsor_id>/ directory per
  // assigned sponsor under output_dir, holding the rendered email
  // draft HTML and the combined child information PDF (angel sheets
  // only) plus a copy of the instructions PDF (instructions file first,
  // then each angel's child information sheet).
  "sponsor_packets": {
    "email_template_html_file": "data/sponsor_packet_email_2026.html",
    "instructions_file": "data/Sponsor Instruction 2026.pdf",
    "output_dir": "./output/sponsor_packets",
    "filename_prefix": "sponsor_packet"
  },

  // Logging / reports. match_report is also exported as CSV for the
  // program coordinator.
  "log_file": "../IO/ccat_2026.log",
  "match_report_csv": "../IO/ccat_2026_match_report.csv"
}
```

Rules:

- Config is loaded and validated once per module run (pydantic model in
  `src/ccat/config.py`); unknown keys or missing required keys fail fast.
- `type` values: `string`, `name`, `email`, `phone_us`, `int`, `float`, `bool`,
  `date`, `datetime`, `enum`.
- Column names are auto-normalized to lower snake_case; `rename` overrides
  with a semantic mapping (e.g. "Number of Angels to Sponsor" becomes
  `num_children`; family "Email" and child "Primary Email" both become
  `primary_email`).
- `enum` columns declare `values` as either an inline list or the name of a
  vocabulary in `config.vocabularies` (transcribed from the Zite form schema
  export "Copper Country Angel Tree - 2026.json"; full lists in Appendix A).
  Enum values are matched case-sensitively against the exact Zite labels.
- Column keys are lower snake_case everywhere: config, code, Excel headers.
- The child column "Age at Christmas 2026" is season-specific and imported as
  `age_reported` for cross-checking only; the computed `age_at_xmas` (from
  `xmas_date`) is the value used downstream.

## 4. Excel Workbook Contract

One workbook (`excel_file` from config). Sheets:

| Sheet           | Contents                                                        | Written by |
|-----------------|-----------------------------------------------------------------|------------|
| `raw_sponsor`   | **Verbatim** Zite sponsor records (config columns, no cleaning)  | module 1   |
| `raw_family`    | **Verbatim** Zite family records (every submission, duplicates included) | module 1   |
| `raw_child`     | **Verbatim** Zite child records (every submission, duplicates included) | module 1   |
| `sponsor_submissions` | Cleaned, ID-assigned sponsor rows (§6.3), one per submission | module 2 |
| `family_submissions` | Cleaned, ID-assigned family rows (§6.3), duplicates included | module 2 |
| `child_submissions` | Cleaned, ID-assigned child rows (§6.3) with `family_id`, `angel_id`, `age_at_xmas` | module 2 |
| `duplicate_log` | One row per flagged duplicate submission — nothing merged (§6.4) | module 2   |
| `dob_conflict_log` | One row per same-family pair born <=9 months apart (§6.6)    | module 2   |
| `email_validation_log` | One row per (record, field) email checked via ZeroBounce, **full verbatim API response** in `response_json` (§6.7) | module 2 |
| `address_validation_log` | One row per family address checked via Nominatim, **full verbatim response** in `response_json` (§6.8) | module 2 |
| `adj_child`     | **Dense** adjudicated child record + controls + `potential_duplicate` (audit arch §4.3); screening pre-fills | modules 2, 3, 5 |
| `adj_family`    | **Dense** adjudicated family record + controls + `potential_duplicate` + assignment columns | modules 2, 3, 4 |
| `adj_sponsor`   | **Dense** adjudicated sponsor record + controls + `potential_duplicate` + assignment columns | modules 2, 3, 4 |
| `final_child`   | Projection of `adj_child` (audit arch §7.1): ignores dropped; **orphaned children (family_id null) excluded** | modules 3, 5 |
| `final_family`  | Projection of `adj_family`                                      | module 4   |
| `final_sponsor` | Projection of `adj_sponsor`                                     | module 4   |
| `match_report`  | Unmatched families/sponsors, unresolved duplicates, and reasons | module 4   |

The `adj_*` sheets are dense — each data column holds the adjudicated
value that flows downstream; a null cell means the value is genuinely
absent, never "same as raw".  The `final_*` sheets are pure projections
of the `adj_*` sheets: drop ignored rows, drop the pure control columns
(`audited`, `ignore`, `screened`), keep everything else (including
`status`, notes, and `audited_at`).  `final_child` additionally drops
orphaned children (rows whose `family_id` is null — they stay in
`adj_child` for the audit tool to fix).  Nothing builds finals from raw.

I/O implementation lives in `src/ccat/io.py`: thin polars-based
read/write helpers over the workbook. Writes are atomic (write temp file,
replace) to avoid corruption if a run is interrupted.

`final_child.age_at_xmas` is stored as a live spreadsheet formula:
`=DATEDIF(<dob cell>, DATE(Y,M,D), "y")` with the age (recomputed from the
row's `dob` and `xmas_date`) embedded as the cached value. openpyxl cannot
write a formula and its cached result in one cell, so `write_sheets` saves
the ages as numbers and then patches the sheet XML to add the `<f>`
element; fastexcel reads (the pipeline's reader) see the cached number,
while LibreOffice/Excel show a formula that recalculates when a `dob` cell
is edited. Every `write_sheets` call that includes `final_child` must pass
the configured Christmas date (`xmas=cfg.xmas`) and re-derives the ages;
calls that write other sheets restore the existing cached values, so the
formulas and their caches survive every pipeline run.

## 5. Module 1 — Verbatim Data Acquisition

Entry point: `src/ccat/acquire.py`, CLI `ccat acquire [--config PATH]`.

Module 1 **only fetches**. It pulls the three Zite tables and writes
them verbatim to the `raw_*` sheets: no cleaning, no validation, no
deduplication, no ID assignment, no duplicate removal. Every downstream
preparation belongs to module 2 (`ccat screen`, §6).

### 5.1 Pull and import

- Call `ccat.zite.get_table_data()` for each of the three table references in
  `config.zite`.
- Import columns per `config.tables.*.columns`; apply `rename` first, then
  cast to snake_case keys. Values are copied verbatim.
- Zite system columns map to `zite_record_id` and `zite_created_at`; the
  system `updatedAt` is not imported (sponsor's user "Updated time" column
  provides `updated`).
- User-facing "Created time" (family) and "Creation Time" (child) map to
  `created`. The sponsor table has no user-created column, so its `created`
  is the Zite system `zite_created_at` (confirmed correct for ID ordering).
- Columns not listed in the config are dropped (explicit allow-list).

### 5.2 Persistence and re-run semantics

- `raw_sponsor`, `raw_family`, `raw_child` are fully rebuilt on every run,
  verbatim (sheet dtypes follow the declared column types; values are not
  transformed).
- Duplicates survive: nothing is merged, coalesced, or dropped.
- `*_submissions`, `adj_*`, `final_*`, and the log sheets are never touched
  by this module.
- Validation: after write, read back and confirm the `final_*` sheets are
  byte-identical to before the run (guard against accidental corruption).

## 6. Module 2 — Pre-audit Screening

Entry point: `src/ccat/screening.py`, CLI
`ccat screen [--config PATH] [--redo ZITE_RECORD_ID] [--test]
[--overwrite] [--skip-email-check] [--skip-address-check]`.

Module 2 prepares everything module 1 no longer does, then AI-screens
the children.  Order:

1. **Local pre-audit (§6.2)** of all three tables — never the AI.
2. **Derived IDs** (§6.5) on the cleaned rows.
3. **Duplicate detection (§6.4)** — flag only, never merge.
4. **Sibling DOB conflicts (§6.6)**.
5. **Submissions sheets + logs written** (§6.3).
6. **Adj seeding** for sponsors, families, children (§6.9).
7. **ZeroBounce email check (§6.7)** and **Nominatim address check
   (§6.8)**.
8. **AI screening of the children only (§6.10)**.

### 6.2 Local pre-audit: clean and validate-as-flags

The verbatim `raw_*` records are cleaned with the local trivial fixes
(these never go near the AI):

- **Names** (`type: name`): title case, preserving apostrophes/hyphens
  (e.g. `o'brien` -> `O'Brien`, `smith-jones` -> `Smith-Jones`).
- **Emails** (`type: email`): lowercased; must match an RFC 5322 subset regex.
- **US phones** (`type: phone_us`): strip non-digits; accept 10 digits, or 11
  digits starting with `1`; normalize to `(XXX) XXX-XXXX`.
- **Dates** (`type: date`): accept `MM/DD/YYYY` and ISO `YYYY-MM-DD`, normalize
  to ISO. **Datetime** (`type: datetime`): ISO 8601 with timezone.
- **Enums**: value must be in the declared `values` (inline list or
  `config.vocabularies` entry, Appendix A).
- Blank strings become nulls; declared defaults fill nulls; free-text
  `age_reported` ("4 turning 5", "2.5") coerces to an int or null.

Failure handling changed: **rows are never dropped**. Every cleaning or
enum failure becomes a note (`<column>: <reason> (<value>)`) keyed by
`zite_record_id`; the notes land in the seeded adj row's
`require_clarification`, so the row shows as `needs_clarification` in
the audit tool instead of silently disappearing.

### 6.3 Prepared submissions sheets

The cleaned, ID-assigned rows are written to
`sponsor_submissions` / `family_submissions` / `child_submissions` —
config columns plus derived columns (`sponsor_id` + age bounds,
`family_id`, child `family_id`/`angel_id`/`age_at_xmas`), sorted by
(`created`, `zite_record_id`). Every submission is present, duplicates
included. These sheets are what the audit tool, match, generate, and
stats (`--raw`) read as their working data.

### 6.4 Duplicate detection — flag only, never merge

Group keys: families by `primary_email`, children by
(`primary_email`, `first_name`), sponsors by `email` (sponsors were
never deduplicated before). Rows missing their key are never grouped.
The survivor is the earliest submission for families and sponsors, the
most recent for children (matching the old dedup semantics) — but
nothing is dropped or merged:

- Every group member keeps its prepared row (and its adj row).
- Every member's adj row gets `potential_duplicate = true` and a
  `require_clarification` note naming the survivor submission.
- One `duplicate_log` row per group member records: `sheet`,
  `group_key`, `zite_record_id`, `created`, `survivor` (bool),
  `conflict_columns` (columns where group members' non-null values
  disagree; identity metadata excluded), `conflict_details`, and the
  full record JSON.

The user resolves duplicates in the audit tool by **ignoring** the
extras (match pools exclude ignored rows; §8). Match additionally
reports unresolved duplicate families/sponsors still sharing an email
in the pool (§8.7).

### 6.5 Derived columns and IDs

- `age_at_xmas` (child): integer years between `dob` and `xmas_date`;
  an unparsable dob leaves null (the row is flagged instead of
  dropped). An `age_reported` mismatch is logged and passed to AI
  screening.
- `pref_age_min` / `pref_age_max` (sponsor): exact mapping from the
  `age_preference` vocabulary:

  | `age_preference` label                   | min | max |
  |------------------------------------------|-----|-----|
  | Any Age                                  | 0   | 18  |
  | Newborn through Toddler (0-3 yrs)        | 0   | 3   |
  | Pre-K through Grade 5 (4-10 yrs)         | 4   | 10  |
  | Middle School (11-13 yrs)                | 11  | 13  |
  | High School (14-18+ yrs)                 | 14  | 18  |
  | Special Needs (any age)                  | 0   | 18  |

  "Special Needs (any age)" additionally marks the sponsor as a
  special-needs sponsor (§8.3).
- `family_id`: one id per `primary_email` group — duplicate family
  submissions **share** the family's id; groups ordered by earliest
  `created` then email. Email-less rows are singleton groups.
- `sponsor_id`: sequential per sponsor submission, ordered by earliest
  `created` then `email`.
- `angel_id` (child): `<family_id><letter>`. Children link via
  `primary_email`. Within a family, letters go to the **survivor of
  each duplicate-child group only** (extras stay letterless and
  flagged), ordered by descending `age_at_xmas` (oldest gets `A`),
  tie-break earliest `created` then `dob`.
- **Orphaned children** (email matches no family): `family_id` and
  `angel_id` null, row kept and logged; the audit tool flags them for
  clarification automatically.

### 6.6 Sibling DOB proximity check

Every pair of children sharing a `family_id` whose birth dates are at
most `screening.sibling_dob_max_gap_days` days apart (default 274, ~9
months; twins included) is recorded in `dob_conflict_log` — one row per
pair (`family_id`, `primary_email`, `child_a_*` earlier birth,
`child_b_*`, `gap_months`, `gap_days`). Duplicate submissions of the
same child (same email + name) never form a pair. Orphans and
null-dob children are skipped. Notes are merged into the affected
children's `potential_anomoly`.

### 6.7 Email deliverability — ZeroBounce

Every new email from sponsors (`email`) and families
(`primary_email`, `secondary_email`) is checked via the ZeroBounce API
(engine: `scripts/zerobounce_email_check.py`, reused in-process).
Targets are deduplicated by normalized address; emails already recorded
in `email_validation_log` are skipped, so re-runs never burn credits
twice. Each (record, field) occurrence gets one log row:
`source_sheet`, `zite_record_id`, `field`, `email`, `status`,
`sub_status`, `did_you_mean`, **`response_json` (the ENTIRE verbatim
API response, every field)**, `checked_at`. API failures are
non-fatal — the affected emails stay unchecked for the next run. The
key comes from the `ZEROBOUNCE_API_KEY` env var (`email_check.api_key_env`).

### 6.8 Address validation — Nominatim

Every family address (street / apt / city / state / ZIP) is checked
against OSM Nominatim (engine: `scripts/nominatim_address_check.py`;
no API key, `address_check.base_url` configurable). Targets are
deduplicated by normalized query text; queries already recorded in
`address_validation_log` are skipped. Each log row:
`source_sheet`, `zite_record_id`, `query`, `status` (`found` /
`ambiguous` / `not_found` / `error`), `display_name`, `lat`, `lon`,
**`response_json` (the ENTIRE verbatim Nominatim response)**,
`checked_at`. The OSM usage policy (1 request/second, identifying
User-Agent) is honored via `address_check.delay` and
`address_check.user_agent`. Failures are non-fatal.

### 6.9 Adj seeding

Screening seeds all three adj sheets itself (audit's
`ensure_adj_sheets` remains as an idempotent backstop):

- `adj_sponsor`: one dense row per sponsor submission.
- `adj_family`: one dense row per `family_id`, seeded from the group's
  survivor submission.
- `adj_child`: one dense row per child submission.

Seeded rows get default controls, the `potential_duplicate` flag, and
the validation/duplicate notes. **Existing adj rows are never
re-seeded** (human edits are protected); they only receive idempotent
flag/note updates, so a newly appearing duplicate still surfaces.

### 6.10 AI screening (children only)

For each child row not already present in `adj_child` (or with
`screened != true`):

- Build a prompt from `config.ai_screening.system_prompt` (full text in
  Appendix B) populated with the child's `zite_record_id`, `dob`, computed
  `age_at_xmas`, reported `age_reported`, `gender`, every size field with its
  type, and the needs/desires.
- **Privacy: no personal data is ever sent.** The prompt contains only
  the pseudonymous `zite_record_id` and the screening-relevant fields —
  no `first_name`, no contact details, no family information.
  **Sponsors and families are never AI-screened**: every one of their
  fields is personal data, so their pre-audit is entirely local (§6.2).
- Trivial capitalization/format fixes already happened locally (§6.2),
  so the model only fixes meaning-level issues.
- The model must adjudicate (rules and infant threshold encoded in the
  prompt, Appendix B):
  1. **DOB sanity**: cross-check computed `age_at_xmas` against
     `age_reported` and against the size values/types.
  2. **Needs/desires clarity**: spellcheck and reword unclear text.
  3. **Clothing sizes**: only touch a size field on an obvious
     contradiction; corrections must use exact Zite vocabulary labels.
- **Output contract**: one JSON object keyed exactly by
  `RESPONSE_COLUMNS` (a key is null when there is no change;
  `potential_anomoly` describes discrepancies; all-null means the model
  returned the literal `null`, which still records the screening).
  Validated against a pydantic schema (exact key set, vocabularies, dob
  format) before acceptance; one retry on a malformed response, then
  the child is left unscreened (picked up next run).
- **Dense output** (§6.9): the cleaned values overlaid with the
  model's non-null corrections, `screened = true`, default controls.
- **Human-edit protection**: a correction lands on a model-owned field
  only when that field still equals the cleaned baseline; fields the
  human already changed are left alone; controls and module-owned
  columns are always preserved.
- Regeneration: `--redo ZITE_RECORD_ID` re-screens one child;
  `--overwrite` re-screens every child under the same protection rules;
  `--test` screens only the first family's children.
- Batch runs use a thread pool (one opencode session per worker
  thread); every completed child is persisted atomically as it
  arrives, so Ctrl-C keeps completed rows.

## 7. Module 3 — Human Audit Tool (v2 — NiceGUI)

**Authoritative spec: `audit_architecture.md` (supersedes this section's
pre-v2 text).**  Entry point `src/ccat/audit/`, CLI `ccat audit
[--config PATH] [--test] [--restart]` plus the one-time bootstrap
`ccat audit-migrate --config config/ccat_2026_v2.json --from
./output/ccat_2026.xlsx`.

A multi-page NiceGUI app (same pattern as `ccat clarify`): a dashboard
(`/`) with sponsor/family per-status stats, sponsor and family lists
with text + status filters, family detail pages showing one raw column
per duplicate submission plus the adjudicated column, and sponsor,
family, and child detail pages where every adjudicable field is edited
against the raw baseline with diff highlighting.  `audited` is a
revocable checkbox; the derived `status` (new / in_progress /
needs_clarification / ignored / audited) recomputes live in the UI and
is persisted on every save, so the workbook is filterable in Excel and
downstream modules gate on it.  Ignoring a row is destructive: the adj
row is reduced to its identity skeleton, its projection is excluded
from the final sheet, and un-ignoring re-seeds from the baseline (edits
lost, confirmed in the UI).  The data layer (`store.py`) is pure and
unit-testable; every save preserves the module-owned columns
(assignments, `generated_info_sheet`, `screened`).

Startup (`ensure_adj_sheets`) seeds a dense adj row for every raw
entity missing one, then re-projects `final_child`.  Every
`final_child` write passes `xmas=cfg.xmas` (the live `DATEDIF` age
formulas, §4); writes that skip `final_child` restore the formula
caches automatically.  A legacy workbook whose `adj_child` still has
the pre-v2 sparse schema (no `screened` column) is refused before any
write — the v2 audit tool must never write the old workbook; create the
v2 workbook with `audit-migrate` instead.

The old http.server + `static/index.html` implementation is deleted;
the process-discovery helpers (`find_audit_processes`,
`stop_audit_processes`, `--restart`) live in the new `server.py`.

## 8. Module 4 — Matchmaking

Entry point: `src/ccat/match.py`, CLI `ccat match [--config PATH]`.

### 8.1 Input

Dense adj sheets only — never `raw_*` (audit arch §7.5):

- `adj_child`: only rows with `status == "audited"` **and** a non-null
  `angel_id` **and** `ignore != true` participate — orphaned children
  (§5.5) are excluded;
- `adj_family` / `adj_sponsor` (non-ignored rows): all sponsor/family
  data — `num_children`, `age_preference`, `prior_assigned_angel_ids`,
  locations, `pref_age_min/max` — comes straight from the dense adj
  rows.

### 8.2 Constraints

- **Family indivisibility**: all children of a family go to exactly one
  sponsor. Never split a family. A sponsor may receive multiple families.
- **Exact count**: the sum of children across a sponsor's assigned families
  must equal the sponsor's `num_children` exactly. Families that cannot be
  placed in any exact combination remain unmatched (reported in
  `match_report`).
- **Sponsor class ordering**: sponsors whose `age_preference` is "Special
  Needs (any age)" are matched first (their special-needs preference is the
  highest preference, §8.3). Then `Individual` sponsors, then
  `Organization` and `Walmart` sponsors together. Within each phase,
  sponsors are ordered by `sponsor_id` ascending.
- **Pre-assigned sponsors are excluded from matchmaking**: sponsors whose
  `prior_assigned_angel_ids` is non-null keep their manual assignment and
  never draw from the pool (§8.5).

### 8.3 Preference priority (lexicographic, per user spec)

For a sponsor–family pairing, evaluate in order; earlier tiers dominate later
ones (implemented as weighted score with weights from
`config.matchmaking` sized so no lower tier can outweigh a higher one):

0. **Special needs** (only for special-needs sponsors, absolute highest):
   fraction of the family's children with `special_needs == true` in
   `final_child`. Applies only to sponsors whose `age_preference` is
   "Special Needs (any age)"; they are also processed first (§8.2) so they
   draw special-needs children before the general pool.
1. **Location**: sponsor `dropoff_location` and family
   `gift_pickup_location` reduce to the same canonical bucket via
   `config.location_groups`: `{Hancock, TSA (Hancock)} -> hancock`,
   `{Calumet, CCAM (Calumet)} -> calumet`. Sponsor `Any Location` (or null)
   is indifferent (no penalty).
2. **Child age**: fraction of the family's children whose `age_at_xmas` falls
   within the sponsor's `[pref_age_min, pref_age_max]` (mapped from
   `age_preference` at acquisition, §5.5).
3. **Child gender**: fraction of the family's children whose `gender` matches
   the sponsor's `gender_preference` (`Any Gender` = full score).
   **Hard rule**: a `Non-binary` child can only be matched to a sponsor with
   `Any Gender` preference — Male/Female-preference sponsors are never
   assigned families containing `Non-binary` children, regardless of other
   tiers.

### 8.4 Algorithm

Deterministic, exhaustive for the expected data scale (community program,
tens of sponsors/families):

- Build the family pool from audited children, grouped by `family_id`, each
  with `child_count`, aggregated age/gender/location signals — **minus every
  child pre-assigned via `prior_assigned_angel_ids`** (§8.5).
- Process sponsors in phases: special-needs sponsors first, then
  `Individual`, then `Organization` and `Walmart` together, each ordered
  by `sponsor_id` ascending (creation order).
  Pre-assigned sponsors (§8.5) are skipped entirely.
- For each sponsor, search subsets of remaining families whose child counts
  sum exactly to the sponsor's `num_children`; score each subset by the sum
  of per-family preference scores (§8.3); pick the maximum, tie-break by
  lowest sorted family_id tuple. Subset search is bounded
  (prune by child count); if the pool is too large for exhaustive search,
  fall back to deterministic greedy with backtracking and log the fallback.
- Families remaining after both phases and sponsors that could not be filled
  exactly are listed in `match_report` with reasons (e.g. "no exact count
  combination", "location mismatch only").

### 8.5 Pre-assigned sponsors (Assigned Angel ID Code)

Some sponsors already have manually assigned angels recorded in the
`Assigned Angel ID Code` column (`prior_assigned_angel_ids` in
`raw_sponsor`). These assignments are honored verbatim:

- Parsing: split the free-text value on commas/whitespace, strip, uppercase
  (angel ids like `3b` normalize to `3B`).
- **Existence check**: every entered angel_id must exist in `final_child`.
  Unknown ids are dropped and logged in `match_report` with the sponsor.
- **Duplicate check**: no angel_id may appear in more than one sponsor's
  assignment (across pre-assigned sponsors, and versus pool-matched
  sponsors). A duplicate is a conflict: the sponsor with the **earlier
  `sponsor_id`** keeps the angel; the later sponsor's duplicate is dropped and
  both sponsors are flagged in `match_report` for manual resolution.
- **Family integrity**: if a pre-assigned angel has siblings (same
  `family_id`) not covered by that sponsor's pre-assigned list, the siblings
  are added to the same sponsor when the sponsor's `num_children` allows;
  otherwise the family is flagged in `match_report` (family would otherwise
  be split between a pre-assignment and the pool).  A family the §8.5
  reconciliation leaves unassigned (e.g. partially covered, siblings
  exceeding `num_children`) is still pointed at the pre-assigned sponsor
  in `final_family`, with `assigned_angel_ids` limited to that sponsor's
  prior angels of the family; the `match_report` split flag remains for
  manual follow-up.
- Pre-assigned sponsors do **not** participate in pool matchmaking, and
  their pre-assigned children are removed from the pool available to
  everyone else.
- Their `final_sponsor` row carries the reconciled `assigned_angel_ids`.

### 8.6 Output

- `adj_family` / `adj_sponsor`: assignments are written into the
  **assignment columns only** (`assigned_sponsor_id`,
  `assigned_angel_ids`, `num_angels_in_family`;
  `assigned_angel_ids`, `num_angels_assigned`) — every other dense
  column (data + audit controls) is preserved on every run.
  `num_angels_in_family` counts the family's non-ignored `adj_child`
  rows.
- `final_family` / `final_sponsor`: projections of the adj sheets
  (audit arch §7.1); the previous contents are archived to timestamped
  sheets (`final_family_YYYYMMDD_HHMMSS`, etc.) before replacement, so
  prior matchings are never silently lost.  `ccat match --overwrite`
  skips the archiving and replaces the sheets in place.  A child's
  special needs is the non-empty `special_needs` **string** (audit arch
  D4).
- `match_report`: unmatched families and sponsors with reasons, plus
  pre-assignment problems (unknown ids, duplicates, split families).
  Also exported as CSV to `config.match_report_csv` for the program
  coordinator.
- `final_child` is read-only here (never rewritten); its formula caches
  survive every match write (io's automatic restore, §4).

### 8.7 `--skip-match` reconciliation

`ccat match --skip-match` never runs the matching algorithm or touches
`match_report`.  Instead it reconciles the assignment columns inside the
adj sheets (§8.6 ownership still applies; audit arch §7.2):

- `adj_sponsor`: a non-empty `prior_assigned_angel_ids` is propagated to
  `assigned_angel_ids` (parsed per §8.5, uppercased, sorted); otherwise
  the existing assignment carries over unchanged; `num_angels_assigned`
  refreshes from the resulting list.  If the same angel_id ends up
  assigned to more than one sponsor, a `MatchError` is raised before
  anything is written.
- `adj_family`: `num_angels_in_family` refreshes from the non-ignored
  `adj_child` rows; existing assignments carry over untouched.
- Follow-through: each propagated prior angel's family gets
  `assigned_sponsor_id` set to that sponsor and `assigned_angel_ids`
  set to that sponsor's prior angels of the family.  Prior angels not
  in `adj_child`, or whose family is not in `adj_family`, warn and are
  skipped.  A family already assigned to a different current sponsor
  keeps that assignment and warns (split family; earlier `sponsor_id`
  wins deterministically).  A family assigned to a sponsor no longer
  in `adj_sponsor` is stale: the prior reassigns it.
- `final_family`/`final_sponsor` are re-projected from the adj sheets;
  previous finals are archived (timestamped) unless `--overwrite`.

## 9. Idempotency and Ground-Truth Protection

Ownership matrix — a module may only write cells in its own column:

Ownership matrix (audit_architecture.md §4.4 amends this; sheet-level —
column scoping is enforced by the modules through the shared store
helpers and by tests).  The one-time `migrate` writer exists only for
the `audit-migrate` bootstrap of a fresh workbook and never writes the
`--from` workbook:

| Sheet             | acquire | screen | write (data+controls) | match | generate | migrate |
|-------------------|---------|--------|-------|-------|----------|---------|
| `raw_*`           | write (verbatim) | read | read | - | - | write (bootstrap copy) |
| `*_submissions`   | -       | write (cleaned + IDs) | read | - | - | write (bootstrap copy) |
| `duplicate_log`, `dob_conflict_log` | - | write | read | - | - | write (bootstrap) |
| `email_validation_log`, `address_validation_log` | - | write | read | - | - | - |
| `adj_child`       | -       | write (data cells + `screened` + flags) | write (data + controls) | read | write (`generated_info_sheet` only) | write (bootstrap) |
| `adj_sponsor`     | -       | write (seed + flags) | write (data + controls) | write (assignment columns only) + read | read | write (bootstrap) |
| `adj_family`      | -       | write (seed + flags) | write (data + controls) | write (assignment columns only) + read | read | write (bootstrap) |
| `final_child`     | -       | -      | write (projection) | read | write (projection refresh) | write (bootstrap) |
| `final_family`    | -       | -      | -     | write (projection) | read | write (bootstrap) |
| `final_sponsor`   | -       | -      | -     | write (projection) | read | write (bootstrap) |
| `match_report`    | -       | -      | -     | write | -         | -        |

The v2 audit tool refuses a workbook whose `adj_child` still has the
pre-v2 sparse schema (no `screened` column) — the old system-of-record
workbook is never written by the v2 system (audit arch §10.1).

Additional guarantees:

- Every module, on start, opens the workbook read-only first; any planned
  write to a sheet it does not own is a hard error.
- New season = new config + new workbook; nothing in the pipeline carries
  state across seasons except files named in the config.
- Excel writes are atomic (temp file + replace) so an interrupted run never
  leaves a half-written workbook.

## 10. Code Layout

```
src/ccat/
  __init__.py          # main() dispatch for `ccat` CLI (fire)
  config.py            # pydantic config model + loader
  io.py                # Excel + logging helpers
  zite.py              # existing Zite REST client (unchanged)
  gdrive.py            # existing Google Drive client (unchanged)
  acquire.py           # module 1: verbatim Zite fetch (raw_* sheets only)
  screening.py         # module 2: pre-audit (clean, IDs, dup flags, email/address checks) + AI child screening
  match.py             # module 4
  child_info_generate.py  # module 5
  sponsor_packets.py   # module 5b: sponsor email draft + combined packet PDF
  stats.py             # module 6
  audit/
    __init__.py        # AuditError
    store.py           # pure data layer: dense adj schemas, status machine, groups, saves, projections
    migrate.py         # one-time v2 workbook bootstrap (`ccat audit-migrate`)
    pages.py           # NiceGUI pages + shared components (§8 of the audit arch)
    server.py          # run(): seed sheets, load state, ui.run (+ --restart helpers)
test/
  test_config.py
  test_acquire.py      # verbatim fetch, import mapping, IDs, angel rules, sibling DOB conflicts
  test_screening.py     # local pre-audit, duplicate flagging, ZeroBounce/Nominatim log wiring, prompt build (no personal data), dense writes, human-edit protection
  test_audit_store.py   # status machine, seeding, groups, dense saves, ignore, projections, formula integration
  test_audit_migrate.py # v2 workbook bootstrap
  test_audit_server.py  # process discovery, restart, run() wiring
  test_match.py         # adj pools, status gate, indivisibility, exact counts, phase order, scoring
  test_child_info_generate.py  # flag handling, enrichment, render/convert wiring
  test_sponsor_packets.py  # per-sponsor email draft + combined packet PDF
  test_stats.py         # report figures, tables, raw mode
```

Style: ruff format/check, `ty` type checking, google docstrings, snake_case
functions/variables, CamelCase classes, `test_` prefixed tests in `test/`.
Run via `just ready-py` for the venv.

## 11. Error Handling and Logging

- All modules log to `config.log_file` (plain text, timestamped, level INFO;
  DEBUG for per-row detail). Failures raise typed exceptions
  (`ConfigError`, `AcquisitionError`, `ScreeningError`, `AuditError`,
  `MatchError`, `InfoSheetError`, `SponsorPacketError`, `StatsError`) so
  the CLI can print actionable messages.
- Zite/network errors surface from `zite.py` unchanged; acquire retries are
  already handled inside the Zite client (429 with backoff).
- **Zite is strictly read-only**: the pipeline only ever pulls data from
  Zite. No module writes anything back to Zite under any circumstances,
  including final match assignments (they live only in the Excel workbook
  and CSV export).
- No module ever partially writes on failure: validation/processing completes
  before any sheet is touched; sheet writes are one atomic operation per
  sheet.

## 12. Open Items (TODO)

All design questions are resolved. Remaining inputs before implementation:

1. Fill `config.zite.*` table references (Zite database URLs, entered
   manually by the user) and `ai_screening.model` (§3). The
   `system_prompt` text is finalized in Appendix B.

Resolved decisions (for the record):

- ~~Sponsor created ordering~~ — Zite system `createdAt` confirmed for
  `sponsor_id` / `family_id` ordering.
- ~~`age_preference` format~~ — fixed vocabulary, mapped in §5.5.
- ~~Special needs~~ — human audit tool checkbox sets `special_needs` on
  `final_child` (§7); it is the highest preference tier for "Special Needs
  (any age)" sponsors, who are also processed first (§8.2, §8.3).
- ~~Gender vocabulary~~ — `Non-binary` children pair only with `Any Gender`
  sponsors, as a hard rule (§8.3).
- ~~Location vocabulary~~ — canonicalized via `location_groups` (§3, §8.3).
- ~~Affiliation ordering~~ — phases are special-needs, Individual, then
  Organization + Walmart (§8.2).
- ~~Infant threshold~~ — `age_at_xmas < 2` (Appendix B prompt).
- ~~`prior_assigned_angel_ids`~~ — reconciliation rules in §8.5.
- ~~Zite write-back~~ — never; Zite is read-only (§11).
- ~~`match_report` CSV export~~ — yes, via `config.match_report_csv` (§3, §8.6).
- ~~Sponsor count options~~ — 1-10, 15, 20, 25 all valid (Appendix A).

## 13. Module 5 — Child Info Sheet Generation

Renders one per-child information sheet PDF for the sponsor packet from the
LibreOffice template `data/child_information_sheet_2026.ott` via the
`py3o.template` package, converted to PDF by headless LibreOffice
(`soffice --convert-to pdf`, isolated `-env:UserInstallation` profile so a
running GUI cannot block conversion).

### 13.1 Input

- `final_child` (module 5 reads; also owns `generated_info_sheet`).
  Rows without a `family_id` (orphaned children — no matching family
  registration) are **ignored and logged**, never an error; they need
  fixing later and can never carry a sponsor assignment.  When every
  row is orphaned, the run fails with "nothing to generate".
- `final_family` (read-only), joined by `family_id` for
  `gift_pickup_location` and `assigned_sponsor_id`.
- `final_sponsor` (read-only), joined by `assigned_sponsor_id ==
  sponsor_id` for the sponsor's name, email, phone, and
  `assigned_angel_ids`.
- `config.info_sheet`: `template_file`, `output_dir`, `soffice_binary`,
  and `mappings` (py3o variable -> enriched column, §3).

### 13.2 Template variables

Three namespaces: `info.*` (angel id, age, gender, family size, drop-off
location, sponsor id), `entry.*` (gifts, sizes, interests), and
`sponsor.*` (assigned sponsor name, id, email, phone, angel ids, and
angel count).

Mapped sources resolve against an *enriched* row: the `final_child`
columns plus the family join plus computed `family_size` (number of
`final_child` rows sharing the child's `family_id`), plus the sponsor
join (aliased `sponsor_*` columns) plus computed `sponsor_name`
(`organization_name` when set, else first + last name).
`info.age` maps to `age_at_xmas`, and `sponsor.family_size` maps to
`family_size` (the angels in the child's own family).

Children without an assigned sponsor (null `assigned_sponsor_id`) render
the `sponsor.*` section blank for hand-filling, as do unmapped
`sponsor.*` placeholders.  A sponsor **with** assigned angels whose
contact info is missing (null, empty, or whitespace-only name, email,
or phone) renders that variable as an underscore placeholder
(`sponsor_contact_placeholder_length`, config-driven, default 30 `_`
characters) when the sponsor's affiliation is listed in
`contact_placeholder_affiliations` (season config: Organization,
Walmart — every affiliation except Individual).  All-or-nothing: when
any of the three is missing, all three render the placeholder
("Assigned Sponsor Name", "Sponsor Email", "Sponsor Phone") so the
coordinator hand-fills the whole block; Individual sponsors
and unassigned children render blank.  The placeholder is plain text,
so it survives the ODT -> PDF conversion unchanged.

### 13.3 `generated_info_sheet` flag

- The flag lives in `adj_child.generated_info_sheet` (Boolean) — the
  final is a projection, so the flag must live in the source of truth
  (audit arch D2).  The `final_child` projection carries it along.
- Only module 5 writes it (ownership matrix, §9).  Module 3 preserves
  the existing value on re-saves of the same child; ignoring a child
  nulls it with the rest of the skeleton (audit arch §5.5).

### 13.4 Run semantics

- Only rows that are **audited** (`final_child.status == "audited"`,
  the readiness gate replacing the old `audited == true` check), whose
  family has an **assigned sponsor** (`assigned_sponsor_id`
  non-null), and whose `generated_info_sheet == FALSE` are processed
  (every sponsored audited row with `ccat generate --overwrite`,
  ignoring the flag column); unsponsored or unaudited children are
  skipped (the sheet's purpose is the sponsor packet).  The output is
  `output/child_info_sheets/<location>/AT<season>_<angel_id>.pdf`, where
  `<location>` is the angel's family pickup location reduced to its
  canonical bucket via `config.location_groups` (`hancock`/`calumet`;
  unmapped labels go to `unknown`) — existing files are overwritten, so
  generation is idempotent.
- Sheets are generated on a thread pool of `info_sheet.max_workers` workers
  (default 4; `--max-workers` overrides; clamped to the pending row count):
  each worker renders with py3o and converts with headless LibreOffice,
  using its own scratch directory and LibreOffice user profile so
  concurrent conversions never share a profile lock.  A `tqdm` progress
  bar tracks completions (one tick per finished sheet, silenced when
  stderr is not a terminal).
- Ctrl+C cancels cleanly: queued children are dropped, in-flight
  LibreOffice conversions are killed as a process group within half a
  second, the flags of every child whose PDF was already written are
  committed in the same atomic sheet write, and the interrupt is
  re-raised (exit code 130).  Interrupted children stay pending — the
  next run picks them up.
- Per-row failures are logged and skipped; after the loop the flags of
  successful rows flip to `TRUE` inside `adj_child` and the
  `final_child` projection is refreshed in the same atomic sheet write
  (with `xmas`), then a typed `InfoSheetError` reports any failed angel
  ids.  Reruns pick up exactly the failed rows.
- `--test` stops after the first successfully generated sheet.
- `--sponsor <id>` scopes the run to one sponsor: the sheet phase only
  renders that sponsor's pending children (respecting the flag, unless
  `--overwrite`), and the packet phase builds only that sponsor's packet
  (its sheets must all exist; an unknown or angel-less sponsor id is a
  typed `SponsorPacketError`).
- Pending rows need a non-null `zite_record_id` and a safe `angel_id`
  filename (no separators); missing family/sponsor rows or duplicate
  `family_id`/`sponsor_id`s in `final_family`/`final_sponsor` are hard
  errors.
- The enrichment columns (`gift_pickup_location`, `assigned_sponsor_id`,
  `family_size`, and the `sponsor_*` join/computed columns) are never
  written back to the workbook; only `adj_child.generated_info_sheet`
  and the refreshed `final_child` projection change.

## 13b. Module 5b — Sponsor Packets

Entry point: `src/ccat/sponsor_packets.py`, CLI
`ccat sponsor_packets [--config PATH] [--overwrite]`.

For every `final_sponsor` row with assigned angels (`assigned_angel_ids`
non-empty), builds one packet directory:
`output/sponsor_packets/<sponsor_id>/` containing:

- `sponsor_packet_<sponsor_id>.html` — the email draft, rendered from
  the Jinja2 template `sponsor_packets.email_template_html_file`
  (default `data/sponsor_packet_email_2026.html`) with: a subject
  line, a personalized greeting ("Dear {first_name},"), and a table
  of the sponsor's assigned angels — angel ID, gender, age at
  Christmas, and per-angel drop-off location (the angel's family
  `gift_pickup_location`).  All styling is inline CSS so the draft
  survives copy-paste into an email client (manual sending; the
  subject, To (sponsor email), and BCC lines are shown in a header
  box above the body).
- `Sponsor <sponsor_id> - Child Information <season>.pdf` — the
  sponsor's angel child information sheets combined into one PDF
  (`output/child_info_sheets/<location>/AT<season>_<angel_id>.pdf`,
  module 5 — sheets nest under their canonical pickup location),
  ordered by family ID then angel letter (natural letter order:
  `1A < 1B < 1Z < 1AA`).  PDF outline bookmarks mark each angel.
- a copy of the sponsor instruction file
  (`sponsor_packets.instructions_file`, default `data/Sponsor
  Instruction 2026.pdf`), copied as-is into the directory.

The email draft names both PDF attachments (`instructions_filename`
and `child_information_filename` template variables) so the body
always matches the files in the directory.

Run semantics:

- Reruns skip sponsors whose packet files already exist;
  `--overwrite` regenerates (manual email edits are lost only then).
  `ccat generate --sponsor <id>` (module 5, §13.4) builds only that
  sponsor's packet.
- Sponsors without assigned angels produce no directory; unknown angel
  ids in `assigned_angel_ids` are dropped with a warning.
- Angels whose child information sheet PDF has not been generated yet
  contribute no pages (warning logged); the packet PDF is still
  written.
- Missing template file, missing `final_sponsor`/`final_child`, or a
  workbook where no sponsor has assigned angels are typed errors
  (`SponsorPacketError`, §11).
- `final_sponsor`/`final_family`/`final_child` are read-only here;
  the module writes only files under `sponsor_packets.output_dir`.


## Appendix A — Form Vocabularies

Transcribed from the Zite form schema export "Copper Country Angel Tree -
2026.json". Enum validation (§5.3) and the screening prompt (Appendix B)
use these exact labels.

**Sponsor**

| Column | Options |
|--------|---------|
| Affiliation | Individual, Organization, Walmart |
| Number of Angels to Sponsor | 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20, 25 |
| Age Preference | Any Age; Newborn through Toddler (0-3 yrs); Pre-K through Grade 5 (4-10 yrs); Middle School (11-13 yrs); High School (14-18+ yrs); Special Needs (any age) |
| Gender Preference | Any Gender, Male, Female |
| Gift Drop-Off Location Preference | Hancock, Any Location, Calumet |

**Family**

| Column | Options |
|--------|---------|
| Gift Pickup Location Preference | TSA (Hancock), CCAM (Calumet), Hancock, Calumet |
| State | full 50-state + District of Columbia list |

**Child**

| Column | Options |
|--------|---------|
| Gender | Male, Female, Non-binary |
| Shoe Size | 0.5, 1, 1.5, ..., 15.5, 16 (0.5 steps), not applicable |
| Shoe Size Type | Baby/Infant, Youth, Men's, Women's, Not Applicable |
| Pant Waist Size | Newborn (NB), 0-3 Months, 3-6 Months, 6-9 Months, 9-12 Months, 12-18 Months, 18-24 Months, 2T, 3T, 4T, 5T, 5" through 44" (integer inches) |
| Pant Inseam | N/A (Baby/Infant), 14" through 36" (integer inches) |
| Pant Size (Letter) | Newborn (NB), 0-3 Months, 3-6 Months, 6-9 Months, 9-12 Months, 12-18 Months, 18-24 Months, XXS, XS, S, M, L, XL, XXL, 2X, 3X, not applicable |
| Pant Size Type | Baby/Infant, Youth, Adult, not applicable |
| Shirt Size (Letter) | Newborn (NB), 0-3 Months, 3-6 Months, 6-9 Months, 9-12 Months, 12-18 Months, 18-24 Months, XXS, XS, S, M, L, XL, XXL, 2X, 3X, not applicable |
| Shirt Size Type | Baby/Infant, Youth, Adult, not applicable |
| Jean Size | free text (no fixed vocabulary) |

Infant-sizing indicators (used by screening, Appendix B): size types of
`Baby/Infant`, month-based sizes (Newborn through 18-24 Months), `2T`-`5T`
toddler sizes, `N/A (Baby/Infant)` inseam, and shoe sizes below ~5 with a
`Baby/Infant` type.

## Appendix B — AI Screening System Prompt

Stored verbatim as `config.ai_screening.system_prompt`. The child's record
is appended as a JSON object after the prompt. `{{xmas_date}}` is substituted
from config at run time.

```text
You are a meticulous data-quality adjudicator for a children's holiday gift
program (Copper Country Angel Tree). You will be given one child's
registration record as a JSON object. Your job is to check it for internal
consistency and produce a corrected (adjudicated) version of the fields you
are confident are wrong or unclear. A human reviewer will see your output
side by side with the original, so be conservative: only change what the
evidence supports.

You will receive these fields:
- zite_record_id (opaque system identifier, copied back unchanged), dob
  (YYYY-MM-DD), age_at_xmas (integer, computed from dob and Christmas
  {{xmas_date}}), age_reported (integer the family typed in, may differ or
  be null), gender
- need_1, need_2, desire_1, desire_2 (free text from the family)
- shoe_size, shoe_size_type, pant_waist_size, pant_inseam,
  pant_size_letter, pant_size_type, jean_size (free text),
  shirt_size_letter, shirt_size_type

The child's name and any family contact information are intentionally NOT
provided to you. Do not speculate about the child's identity, and never
include a name in your output.

CHECK 1 — Date of birth sanity:
- Treat age_at_xmas as the authoritative age (it is computed from dob).
- If age_reported is present and differs from age_at_xmas, describe the
  mismatch in potential_anomoly.
- Infant sizing is expected when age_at_xmas < 2. Infant indicators are:
  size types "Baby/Infant", month-based sizes ("Newborn (NB)" through
  "18-24 Months"), toddler sizes 2T-5T, inseam "N/A (Baby/Infant)", and
  "Baby/Infant"-typed shoe sizes.
- If the child is age_at_xmas < 2 but has non-infant sizing (Youth or Adult
  types, XXS-3X letters, numeric inch waist/inseam), the reported dob may be
  wrong. Likewise, if the child is older but has infant sizing, dob may be
  wrong.
- If a single corrected dob would reconcile age_reported, age_at_xmas, and
  the sizes, and you are confident, propose it in the dob field (YYYY-MM-DD).
  Otherwise leave dob null and describe the discrepancy in
  potential_anomoly.

CHECK 2 — Needs and desires clarity:
- For need_1, need_2, desire_1, desire_2: fix spelling errors, expand
  unclear abbreviations, and lightly reword garbled phrasing so a volunteer
  shopper understands what to buy. Never change the meaning, never invent
  items, never remove items. Keep each field short (one sentence). If the
  text is already clear, leave the field null.

CHECK 3 — Clothing sizes:
- Only touch a size field if the sizes contradict each other or contradict
  the age in an obvious way (e.g. shoe_size 13 with type "Baby/Infant" for a
  10-year-old). If you correct a size, it must be one of the exact allowed
  option labels from the registration form. When in doubt, leave the size
  null and describe the issue in potential_anomoly.

OUTPUT — return exactly one JSON object with these keys:
{"zite_record_id": string, "dob": string|null, "need_1": string|null,
"need_2": string|null, "desire_1": string|null, "desire_2": string|null,
"shoe_size": string|null, "shoe_size_type": string|null,
"pant_waist_size": string|null, "pant_inseam": string|null,
"pant_size_letter": string|null, "pant_size_type": string|null,
"jean_size": string|null, "shirt_size_letter": string|null,
"shirt_size_type": string|null, "potential_anomoly": string|null}

Rules for the output:
- Copy zite_record_id from the input unchanged.
- Every other key is null when you have no change for that field (value
  identical to the input or no correction to make).
- potential_anomoly: a short plain-English description of any age, dob, or
  size discrepancy you found (including age_reported mismatches); null when
  everything is consistent.
- If you have no changes AND no anomaly, return the literal null instead of
  an object. (The pipeline records the screening either way.)
- Return only the JSON object (or null). No explanations, no markdown
  fences.
```
