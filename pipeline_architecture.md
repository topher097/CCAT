# CCAT Pipeline Architecture

> **Historical note.** The NiceGUI audit and clarification applications and
> their `ccat audit` / `ccat clarify` commands were removed. The interactive
> editors now live in the Vue SPA served by `ccat serve` (FastAPI). The data
> logic in `src/ccat/audit/store.py`, `src/ccat/audit/migrate.py`, and the
> `ccat.clarify` modules survives.

Architecture guide for the CCAT gift-giving matching system. This document is the
basis of all code in this codebase. No implementation exists yet; every section below
is the contract that the implementation must satisfy.

## 1. Purpose and Scope

CCAT matches families of children ("angels") with sponsors for a holiday gift
giving program. The pipeline has independently runnable modules:

1. **Data Acquisition** — pull sponsor/family/child tables from Zite and
   replace the three verbatim `raw_*` sheets.
2. **Local Preparation and AI Screening** — clean, validate, link, assign
   stable IDs, refresh diagnostics, validate emails/addresses, append new
   persistent entities, and adjudicate only children still needing AI work.
3. **Human Audit** — the audit editor in the Vue SPA served by `ccat serve`;
   humans edit and commit rows directly in the full persistent `final_*`
   stores through the FastAPI resources.
4. **Matchmaking** — assign whole families to sponsors, honoring special-
   needs processing, exact child counts, and preference priority; it patches
   only assignment-owned columns in `final_family`/`final_sponsor`.
5. **Generation and Packets** — render child information PDFs, set child
   generation markers after real PDF completion, and assemble sponsor packet
   directories.

All workbook state lives in one Excel file. All configuration lives in one
JSON file. Raw is source; final is truth; logs are process history.

## 2. System Overview

```
                 config/ccat_2026.json
                         |
                         v
  [Zite API] --> (1) Acquire -----> raw_sponsor / raw_family / raw_child
                                        |
                                        | in-memory preparation
                                        v
                  (2) Screen ----------> final_sponsor / final_family / final_child
                                        +  duplicate_log / dob_conflict_log
                                        +  email_validation_log / address_validation_log
                                        |
                                        v
                  (3) Audit -----------> full final_* rows (human in the loop)
                                        |
                                        v
                  (4) Match -----------> assignment fields in final_family/final_sponsor
                                        + match_report (+ timestamped archives)
                                        |
                                        v
                  (5) Generate --------> output/child_info_sheets/AT<season>_<angel_id>.pdf
                                        + generated_info_sheet in final_child
                                        |
                                        v
                  (5b) Sponsor packets > output/sponsor_packets/<sponsor_id>/
                                         (email.html draft + combined packet PDF)
```

Modules run individually via CLI (`ccat acquire`, `ccat screen`,
`ccat match`, `ccat generate`, `ccat stats`, `ccat clarify-export`), each
taking an optional `--config` path (default `config/ccat_2026.json`).
Interactive audit and clarification editing run through `ccat serve` and its
Vue views; the legacy `ccat audit` and `ccat clarify` commands are removed.
Packets run as the second phase of `ccat generate`; there is no standalone
`sponsor_packets` command.

Supported migration commands are `ccat audit-migrate` (old v1 directly to
persistent finals) and `ccat consolidate-migrate` (full v2 `adj_*` to a
separate persistent workbook). Migration commands have separate writer
permissions and never turn a final sheet back into a projection.

Each final sheet is the complete editable store. Ignored, orphaned,
duplicate, failed-AI, and raw-missing entities remain stored. Downstream
output readers apply explicit eligibility filters; correction workflows do
not.

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
  // downstream (code, Excel headers, final sheets).
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

One workbook (`excel_file` from config). Active sheets:

| Sheet | Contents | Written by |
|---|---|---|
| `raw_sponsor` | **Verbatim** Zite sponsor records (config columns, no cleaning) | acquire |
| `raw_family` | **Verbatim** Zite family records, every submission and duplicate | acquire |
| `raw_child` | **Verbatim** Zite child records, every submission and duplicate | acquire |
| `final_sponsor` | Complete persistent sponsor store: data, computed bounds, controls, assignments | screen seeds; audit saves; match patches assignments |
| `final_family` | Complete persistent family store, one row per stable family identity | screen seeds; audit saves; match patches assignments |
| `final_child` | Complete persistent child-submission store keyed by `zite_record_id`; duplicates, ignored rows, orphans, and raw-missing rows remain | screen appends/AI patches; audit saves; generate patches generation marker |
| `duplicate_log` | One row per flagged duplicate submission; nothing merges | screen |
| `dob_conflict_log` | One row per same-family DOB pair within the configured gap | screen |
| `email_validation_log` | One row per checked (record, field), with the entire verbatim ZeroBounce response | screen |
| `address_validation_log` | One row per checked address, with the entire verbatim Nominatim response | screen |
| `match_report` | Unmatched families/sponsors, unresolved duplicates, reasons | match |

`duplicate_log.sheet` retains legacy source labels such as
`family_submissions` for response-payload compatibility. Those strings are
not active workbook sheets.

Final schemas are the complete former dense adjudication schemas, including
controls (`status`, `ignore`, `clarification_notes`,
`audited_at`; children also `screened`). No active submissions or `adj_*`
sheets exist. Explicit migration commands may read legacy layers; ordinary
modules may not.

I/O lives in `src/ccat/io.py`. Writes are atomic (temp file + replace).
Final writes validate complete schemas, non-null unique keys, and that no
persisted final key disappears.

`final_child.age_at_xmas` is a live formula,
`=DATEDIF(<dob cell>, DATE(Y,M,D), "y")`, with the age recomputed from the
effective `dob` and `xmas_date` and embedded as its cached value. openpyxl
cannot write formula and cache together, so `write_sheets` saves the number
then patches sheet XML. fastexcel reads the cache; spreadsheet apps see and
recalculate the formula. Every write including `final_child` requires
`xmas=cfg.xmas`; every other write restores existing formula caches. Cache
capture/restoration is fail-closed and whitespace-tolerant: formulas without
usable caches block replacement.

## 5. Module 1 — Verbatim Data Acquisition

Entry point: `src/ccat/acquire.py`, CLI `ccat acquire`.

Module 1 **only fetches**. It calls `zite.get_table_data()` for each of the
three `config.zite` references, applies the config allow-list, semantic
`rename`, system-column mappings, and snake_case keys, and writes values
verbatim. No cleaning, validation, deduplication, or ID assignment happens
here.

System mappings are `zite_record_id <- id` and `zite_created_at <-
createdAt`. Sponsor `created` comes from system `createdAt`; family/child
user-facing creation fields map to `created`. System `updatedAt` is not
imported. Missing configured source columns are structural errors.

The three `raw_*` sheets are fully rebuilt every run. Duplicates survive
unchanged. Acquire snapshots `final_*` and `match_report` before and after
and fails if either changes. It never writes finals or logs.

## 6. Module 2 — Local Preparation and AI Screening

Pure preparation lives in `src/ccat/preparation.py`; orchestration and AI
persistence live in `src/ccat/screening.py`. CLI:
`ccat screen [--redo ZITE_RECORD_ID] [--test] [--overwrite]
[--skip-email-check] [--skip-address-check]`.

### 6.1 Shared in-memory preparation

Every run performs one side-effect-free preparation pass:

1. Read all `raw_*` sheets. Missing sheets/columns and blank or duplicate
   raw `zite_record_id`s fail as `PreparationError`; rows are not dropped.
2. Clean locally: title-case names, lowercase/validate emails, normalize US
   phones, normalize dates/datetimes, enforce enums, blank to null, apply
   declared defaults, and coerce free-text `age_reported` to int or null.
3. Turn every cleaning/enum failure into a note keyed by record ID.
4. Detect duplicates and reconcile stable IDs and linkage in memory.
5. Compute child ages, sponsor age bounds, sibling-DOB rows, and
   created-vs-system timestamp logs.

No AI, external API, or workbook write occurs in this pass.

### 6.2 Stable identity reconciliation

Current full final rows are supplied to preparation:

- Child identity is `zite_record_id`; every submission keeps its own row.
- Existing sponsors match by `zite_record_id` and retain `sponsor_id`.
- Current family submissions map to one family by source record membership,
  then normalized `primary_email`; all share its stable `family_id`.
- New IDs are unused values above the current maximum, assigned in
  deterministic order. Existing IDs and angel IDs are reserved.
- An existing child retains a non-null family and angel; a null family may
  link. A new duplicate never takes an angel owned by an existing member of
  its duplicate group.
- Existing final rows absent from raw remain stored.

### 6.3 Persistent append and merge

Screen reconciles all three stores through one guarded storage step:

- New rows seed from prepared values, default controls, and
  `screened=False` for children. Orphans receive the orphan note and
  `needs_clarification`.
- Existing rows retain IDs, human edits, controls, timestamps, assignments,
  generation state, ignored skeletons, raw-missing state, and user-authored
  notes. Safe source metadata, stable child linkage, duplicate/validation
  notes, and DOB anomaly notes merge idempotently.
- Effective child age and sponsor bounds recompute; ignored rows are not
  re-expanded.
- No row is reconstructed or deleted merely because a stage reran.

`duplicate_log` and `dob_conflict_log` are current diagnostic snapshots,
written even on no-AI runs. They never block later processing.

### 6.4 Duplicates

Keys are family `primary_email`, child `primary_email + first_name`, and
sponsor `email`. Rows lacking a key are not grouped. Survivors are earliest
for family/sponsor and latest for child. Every member keeps its row, gets
`potential_duplicate=true`, and gets a note naming the survivor. The log
carries source label, group key, record ID, created, survivor flag,
conflict columns/details, and full record JSON. Audit ignoring is the
resolution path; match reports unresolved email-sharing pool members.

### 6.5 Derived IDs

- `age_at_xmas`: completed years from effective DOB to Christmas; bad DOB
  stays null and is flagged.
- Sponsor bounds map exactly from `age_preference`. `Special Needs (any
  age)` is 0–18 and marks special-needs processing.
- `family_id` groups by email and orders by earliest created/email.
- `sponsor_id` is one per sponsor submission.
- `angel_id` is `<family_id><letter>`. Current duplicate survivor rules
  determine eligibility, but existing angels never transfer. New eligible
  members get the lowest unused letter in age-descending order, tie-break
  created then DOB.
- Orphans retain null family/angel and remain stored for correction.

### 6.6 Sibling DOB diagnostics

Every same-family pair with DOBs at most
`screening.sibling_dob_max_gap_days` apart is logged once (twins included).
Duplicate submissions of the same identity do not pair. Logs include
family/email, both child IDs/names/angels/DOBs, and exact month/day gap.
Notes merge into both children even when AI is skipped.

### 6.7 Email checks

ZeroBounce checks every nonblank sponsor `email` and family primary/secondary
email. Normalized addresses already in the log are skipped. Unique addresses
call once, then expand to one log row per (record, field). Rows retain
status, sub-status, suggestion, the entire verbatim API response JSON, and
timestamp. Bulk is preferred when available; per-address fallback is
threaded. Failures remain unlogged and retry. The key comes from
`email_check.api_key_env`.

### 6.8 Address checks

Nominatim checks every family address with components. Existing exact
queries are skipped; duplicate current queries check once. Rows retain
found/ambiguous/not_found/error, display name, lat/lon, the entire response
JSON, and timestamp. Config delay/User-Agent implement OSM policy. Errors
are non-fatal; only unlogged queries retry.

### 6.9 AI screening

Ordinary candidates are current children whose final `screened` is false.
`--overwrite` selects every current child; `--redo ID` selects exactly one
and fails early if absent. `--test` limits candidates to the first stable
family, or first child when all are orphans.

Before redo/overwrite work, targets' `screened` markers persist as false.
A failed, malformed, or interrupted attempt therefore remains retryable;
successful work stays completed.

The prompt substitutes `{{xmas_date}}` and contains only pseudonymous record
ID, DOB/ages, gender, needs/desires, and sizes. It never contains names or
contact/family data. Sponsors and families are never AI-screened.

The response is literal `null` or exactly the documented JSON keys. Pydantic,
record-ID, DOB-format, and vocabulary validation accept it; one malformed
response retries, a second failure leaves the child unscreened.

`dense_final_row` overlays corrections only onto fields still equal to the
current prepared baseline, then restores existing editable/computed/control/
module-owned values. Each success upserts and atomically writes the complete
`final_child` store on the main thread. Ctrl-C cancels queued tasks; finished
rows remain. Opencode v2 is required only when targets exist; each worker
thread owns one session.

## 7. Module 3 — Human Audit Tool

Authoritative detail is in `audit_architecture.md` (historical for the removed
NiceGUI UI). Surviving data logic: `src/ccat/audit/store.py` and
`src/ccat/audit/migrate.py`. The interactive editor is the Vue SPA served by
`ccat serve`.

Startup (`ensure_final_sheets`) validates full stores, prepares raw in
memory, and backfills only genuinely missing entities. It never refreshes
existing rows or projects away controls. Missing raw/prepared data gives
actionable acquire/screen errors.

The Vue SPA has audit lists (sponsors, families, children) and detail
editors. Full stores intentionally expose ignored/orphan/raw-missing rows for
correction. Side-by-side baselines come from in-memory prepared rows; raw-
missing rows use their stored effective values.

Saves serialize under the store write lock and accept editable fields plus
`status`, `ignore`, and `clarification_notes`. `status == "audited"` is the
only audit authority; every save stamps
`audited_at` and recomputes stored status; child age and sponsor bounds
recompute. Ignoring retains stable family/angel linkage, identity, controls,
and module-owned fields; ignored rows are excluded from AI work and cannot be
reconstructed by redo/overwrite. Ordinary editable values follow established
editable values follow the established destructive reset and confirmed
unignore baseline behavior. Assignments, `screened`, and
`generated_info_sheet` never change as audit side effects. A null-family
child cannot be marked audited. Child saves write complete `final_child`
with `xmas`.

## 8. Module 4 — Matchmaking

Entry point: `src/ccat/match.py`, CLI
`ccat match [--test] [--overwrite] [--skip-match]`.

### 8.1 Pools

Match reads complete finals, then filters in memory. Participating children
must be `audited`, not ignored, and have an angel ID. Families/sponsors
participate only when not ignored. Raw sheets are never read.

### 8.2 Rules

- A family is indivisible and goes to one sponsor; a sponsor may receive
  multiple families.
- Assigned children must equal `num_children` exactly; unplaceable families
  are reported.
- Phases are special-needs sponsors, then Individual, then
  Organization/Walmart together; `sponsor_id` ascending inside each.
- Sponsors with prior angel IDs never draw from the pool.

### 8.3 Preferences

Weighted lexicographic tiers:

0. Special-needs child fraction, only for special-needs sponsors.
1. Canonical `location_groups` location; `Any Location`/null is indifferent.
2. Fraction of child ages inside sponsor bounds.
3. Gender fraction; `Any Gender` scores fully. A Non-binary child only pairs
   with an `Any Gender` sponsor.

### 8.4 Search

For each sponsor, search subsets of remaining families with exact total child
count. Maximize summed family score; tie-break lowest family-ID tuple. Search
is pruned; oversized pools use deterministic greedy/backtracking and log the
fallback. Remaining families/sponsors and reasons enter `match_report`.

### 8.5 Prior assignments

`prior_assigned_angel_ids` parses, uppercases, checks existence in the
participating child pool, resolves duplicates by earlier sponsor, and adds
covered siblings when `num_children` allows. Split/overfull families remain
pointed at the prior sponsor with only its prior angels and stay flagged.
Pre-assigned sponsors and children leave the pool.

### 8.6 Output

Match patches only assignment columns: family
`assigned_sponsor_id`, `assigned_angel_ids`, `num_angels_in_family`; sponsor
`assigned_angel_ids`, `num_angels_assigned`. Every other field and row,
including ignored/raw-missing entities and all controls, remains intact.
`final_child` is not written.

Normal runs archive existing `final_family`, `final_sponsor`, and
`match_report` to timestamped sheets; `--overwrite` skips archives. The
report exports as CSV to `config.match_report_csv`.

### 8.7 `--skip-match`

No matching or report. Assignment columns reconcile in final stores: prior
IDs propagate, existing assignments carry over, counts refresh, and prior
angels point unassigned families to their sponsor. Duplicate ownership
raises before write. Stale assignments follow deterministic rules. Finals
archive unless `--overwrite`; `match_report` remains untouched.

## 9. Ownership and Idempotency

| Sheet | acquire | screen | audit | match | generate | migrate |
|---|---|---|---|---|---|---|
| `raw_*` | replace | read | read | - | - | explicit conversion/bootstrap |
| `final_sponsor` | verify unchanged | seed + safe merges/backstop | full save | assignment fields | read | explicit conversion/bootstrap |
| `final_family` | verify unchanged | seed + safe merges/backstop | full save | assignment fields | read | explicit conversion/bootstrap |
| `final_child` | verify unchanged | seed + safe merges/AI | full save | read | generation marker | explicit conversion/bootstrap |
| duplicate/DOB logs | - | replace snapshot | read | - | - | explicit conversion/bootstrap |
| email/address logs | - | append unlogged checks | read | - | - | explicit conversion/bootstrap |
| `match_report` | verify unchanged | - | - | write when produced | read | explicit conversion/bootstrap |

Stats, clarify, and packet readers write no workbook. `migrate` is reserved
for explicit migration commands. Column ownership is enforced by each writer
and tests; no writer sets process markers as an unrelated side effect.
Final writes reject projected schemas, null/duplicate keys, and removal of a
stored entity. Writes remain atomic. Concurrent CLI/editor access is still
unsupported: close spreadsheet apps and run one controlling process.

## 10. Code Layout

```
src/ccat/
  __init__.py           # fire CLI dispatch
  config.py             # pydantic configuration
  io.py                 # workbook ownership, atomic writes, age formulas
  zite.py, gdrive.py    # unchanged external clients
  acquire.py            # verbatim Zite fetch + scalar/ID/DOB primitives
  preparation.py        # pure clean/link/dedup/stable-ID/DOB preparation
  screening.py          # diagnostics/logs, final reconciliation, AI
  match.py              # matchmaking and assignment patches
  child_info_generate.py# PDFs, generation markers, packet invocation
  sponsor_packets.py    # packet assembly helpers
  stats.py              # HTML/PDF statistics report
  consolidate.py        # full adj -> separate persistent workbook
  audit/                # store, v1 bootstrap (NiceGUI UI removed)
  clarify/              # extraction, server, external drafts/status
test/                   # unit, migration, and persistent integration tests
```

Style: ruff format/check, `ty` check, Google docstrings, snake_case code,
CamelCase classes, pytest tests in `test/`.

## 11. Errors and Logging

Modules log to `config.log_file` and mirror to stderr. Typed CLI errors
include `ConfigError`, `AcquisitionError`, `PreparationError`,
`ScreeningError`, `AuditError`, `MatchError`, `InfoSheetError`,
`SponsorPacketError`, `StatsError`, `ConsolidationError`,
`SheetOwnershipError`, and `SheetSchemaError`.

Zite remains strictly read-only. Preparation completes before persistent
writes; each workbook replacement is atomic; AI and generation successes
persist individually. Legacy projected finals and malformed stores fail
closed with migration guidance instead of being repaired by guessing.

## 12. Operational Status

Season setup still requires filled `config.zite.*` values and a real
`ai_screening.model`; the prompt text is Appendix B.

After cutover: acquire replaces raw; screen appends/merges and resumes AI;
audit is the sole interactive editor; match and generate own their marker
fields; diagnostics run regardless of audit flags. Explicit redo/overwrite
controls intentional rework. Raw is source, final is truth, logs are history.

## 13. Module 5 — Child Info Sheet Generation

Renders one per-child information sheet PDF for the sponsor packet from the
LibreOffice template `data/child_information_sheet_2026.ott` via the
`py3o.template` package, converted to PDF by headless LibreOffice
(`soffice --convert-to pdf`, isolated `-env:UserInstallation` profile so a
running GUI cannot block conversion).

### 13.1 Input

- `final_child`: the eligible output population (non-ignored and non-null
  `family_id`). Ignored/orphan rows remain in the full store but cannot
  generate. When every eligible row is absent, the run fails with
  "nothing to generate".
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

- The Boolean marker lives directly in `final_child`.
- Only generation writes it, and only after a PDF actually exists. Audit
  preserves it; intentional ignore retains it in the stored row while output
  filtering excludes that child.

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
- Per-row failures are logged and skipped. After the loop, successful IDs
  are marked true in the complete `final_child` store in one atomic write
  (with `xmas`); a typed `InfoSheetError` reports failed angel IDs and
  reruns pick up exactly those rows.
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
- Enrichment columns are never written back. Only
  `generated_info_sheet` changes in the complete final-child store;
  ignored, orphan, raw-missing, pending, and all other rows survive.

## 13b. Sponsor Packet Phase

Entry point: `src/ccat/sponsor_packets.py`, invoked as the packet phase of
`ccat generate` (`--overwrite` and `--sponsor ID` apply); there is no
standalone `ccat sponsor_packets` command.

For every output-eligible `final_sponsor` row with assigned angels
(`assigned_angel_ids` non-empty), builds one packet directory:
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
- Packet membership uses eligible full finals independently: ignored
  sponsors/families/children and orphaned children are excluded; family and
  child filters do not cascade. Unknown angel IDs in `assigned_angel_ids`
  are dropped with a warning.
- `validate_all_sheets_exist` requires PDFs for every eligible child in an
  eligible family with a non-null assigned sponsor; it does not require
  audit status or generation markers. With `--sponsor`, only that sponsor's
  eligible families count.
- Missing template file, missing eligible `final_sponsor`/`final_child`, or
  no sponsor with assigned angels is a typed `SponsorPacketError`.
- Finals are read-only; module writes only files under
  `sponsor_packets.output_dir`.

## 14. Statistics and Clarification

`ccat stats [--raw]` writes dated HTML/PDF reports under
`config.report.output_dir`; same-day filenames replace. Default reads the
eligible persistent populations (non-ignored families/sponsors; non-ignored,
non-orphan children), joins pickup location and assigned sponsor, and
preserves existing admissions/location/gender/age calculations. `--raw`
retains its CLI name but means ephemeral cleaned/ID-assigned preparation from
raw forms; no prepared submission sheet is created. Prepared mode does not
apply output ignore/orphan filters and has no sponsor assignments.

Clarification state is external to the workbook. Draft generation (moved from
the removed `ccat clarify` server into `ccat.clarify.email_gen` and exposed
through the API/service layer) reads eligible noted children/families and
raw-child DOBs and writes family draft HTML plus editable JSON under
`clarify.output_dir`; contacted/replied state lives in
`clarification_status.json`. `ccat clarify-export` refreshes the CSV from those
artifacts, and editing runs in the Vue SPA. None of these paths writes
workbook sheets, and consolidation never moves or rewrites clarification
drafts/status.

## 15. Migration Commands

`ccat audit-migrate --from OLD [--force]` bootstraps an old v1 workbook
directly into full persistent finals in the configured target. Raw is copied;
old sparse adjudications/finals overlay only as documented; no
submissions/adj layers are emitted. It refuses an existing active final
target without `--force`.

`ccat consolidate-migrate --from OLD --output NEW [--force --backup BACKUP]
[--report REPORT]` converts full v2 `adj_*` stores. It never writes the
input, hashes it before/after, copies raw/four logs/match report unchanged,
copies every adj field/control/ID as normalized values into finals, and
creates no submissions/adj/archive sheets. It refuses existing output unless
force is paired with a byte-identical restorable workbook backup. It validates
row/key parity, every field/control, raw/log/report survival, formulas/caches,
and read-only opening, then atomically commits workbook and JSON report with
rollback on late failure. Reports include counts,
distributions, assignments, generated state, pending AI, and hashes.

Rollback restores the original config and immutable input workbook and uses
only old commands. Never feed full persistent finals to old projection-based
code as if they were projections.


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

## Appendix A. Current implementation (2026-11 refactor)

The sections above describe the pipeline contract. The implementation now
runs that contract over SQLite with a FastAPI backend, a Vue frontend, and
a durable job worker. The refactor plan is `temp/web_server_refactor.md`;
its decisions are `temp/refactor_decisions.md`.

### A.1 Layers

| Layer | Location | Responsibility |
|---|---|---|
| Domain | `src/ccat/domain/`, `preparation.py`, `match.py`, `screening.py`, `child_info_generate.py`, `sponsor_packets.py`, `stats.py` | Pure algorithms: cleaning, linkage, stable IDs, duplicate/DOB diagnostics, match scoring, enrichment, report math |
| Services | `src/ccat/services/` | Use-case orchestration shared by CLI and API; typed result DTOs (`results.py`); storage port (`ports.py`) |
| Persistence | `src/ccat/db/`, `alembic/` | SQLAlchemy models, repositories, unit of work, migrations, workbook importer/exporter |
| HTTP | `src/ccat/api/` | Versioned REST (`/api/v1`), DTOs, sessions/RBAC, typed error mapping, OpenAPI contract |
| Jobs | `src/ccat/jobs/` | Durable job registry, worker loop, cancellation, heartbeat/lease/recovery |

The workbook survives only as an import/export format: `ccat db-import`
migrates it into SQLite as one validated, hash-guarded batch, and
`ccat workbook-export` writes the read-only reverse export used as rollback
evidence. `ccat serve` (FastAPI) and `ccat worker` (job loop) are the
active entry points after cutover.

### A.2 Command mapping

| Legacy command | Current equivalent |
|---|---|
| `ccat acquire` | job `acquire` (DB mode) / legacy CLI (workbook mode) |
| `ccat screen` | job `screen` |
| `ccat match` | job `match` |
| `ccat generate` | job `generate` (sheets then packets) |
| `ccat stats` | job `stats` |
| `ccat clarify --generate` | job `clarify_generate` |
| `ccat clarify-export` | job `clarify_export` |
| `ccat audit` (NiceGUI) | removed; replaced by `ccat serve` + Vue audit views |
| — | `ccat db-import`, `ccat db-upgrade`, `ccat worker` (new) |

### A.3 Durable job semantics

Jobs live in the database (`jobs`, `job_events`) with explicit states
(`queued`, `running`, `succeeded`, `failed`, `cancel_requested`,
`cancelled`), attempt/retry budgets, idempotency keys, a per-season
exclusion for pipeline-class work, heartbeats with lease recovery
(`recovery_required`, never a blind repeat), throttled progress, and
cooperative cancellation. Completed AI corrections and PDFs stay committed;
pending rows keep `screened=false` / `generated_info_sheet=false` and are
picked up by the next run.

### A.4 Deliberate divergences from the prose above

- `potential_anomoly` is stored and exposed as `ai_screening_notes`; the
  legacy workbook column and the AI JSON key stay mapped at the boundary.
- The sponsor `create` column is stored as `created`.
- Age is recomputed in Python from DOB and the season Christmas date; the
  workbook `DATEDIF` formula is import/export only.
- Generation commits `generated_info_sheet` per item (after a validated,
  atomically renamed PDF) instead of in one end-of-run batch.
- Match commits assignments, report and archive together; the CSV is a
  tracked artifact whose failure yields an explicit partial state.
- Clarification drafts are versioned rows keyed by child; legacy positional
  drafts are imported as `legacy_positional`.
- The status evaluator returns `ignored` before the sticky `audited` check,
  matching persisted save behavior (decision D11).

Everything else in this document remains the behavioral contract, including
`--test` scope limits (they mutate state), the eligibility differences
between match, generation, packets, statistics, clarification and audit, and
the writer-ownership rules.
