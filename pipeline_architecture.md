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
    "sponsor_affiliation":   ["Individual", "Group", "Organization"],
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

  // Acquisition (module 1). Sibling DOB check: same-family children born
  // at most this many days apart are recorded in dob_conflict_log (§5.6).
  "acquire": {
    "sibling_dob_max_gap_days": 274
  },

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
  // final_child row (§13); template_file is the LibreOffice template.
  "info_sheet": {
    "template_file": "./data/child_information_sheet_2026.ott",
    "output_dir": "./output/child_info_sheets",
    "soffice_binary": "soffice",
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
| `raw_sponsor`   | Cleaned, deduplicated, ID-assigned sponsor rows                 | module 1   |
| `raw_family`    | Cleaned, deduplicated (by primary email), ID-assigned families | module 1   |
| `raw_child`     | Cleaned, deduplicated (same family + name keeps latest) child rows with `age_at_xmas` and `angel_id` | module 1   |
| `dedup_log`     | One row per merged duplicate family or child record (§5.4)      | module 1   |
| `dob_conflict_log` | One row per same-family pair born <=9 months apart (§5.6)    | module 1   |
| `adj_child`     | Screening results, one row per screened child (keyed by `zite_record_id`, sparse cells) | module 2   |
| `final_child`   | Ground-truth audited child rows (plus `generated_info_sheet`, module 5) | modules 3, 5 |
| `final_family`  | Ground-truth families with match results                       | module 4   |
| `final_sponsor` | Ground-truth sponsors with `assigned_angel_ids`                 | module 4   |
| `match_report`  | Unmatched families/sponsors and reasons                         | module 4   |

I/O implementation lives in `src/ccat/io.py`: thin polars-based
read/write helpers over the workbook. Writes are atomic (write temp file,
replace) to avoid corruption if a run is interrupted.

## 5. Module 1 — Data Acquisition and Validation

Entry point: `src/ccat/acquire.py`, CLI `ccat acquire [--config PATH]`.

### 5.1 Pull

- Call `ccat.zite.get_table_data()` for each of the three table references in
  `config.zite`.
- Import columns per `config.tables.*.columns`; apply `rename` first, then
  cast to snake_case keys.
- Zite system columns map to `zite_record_id` and `zite_created_at`; the
  system `updatedAt` is not imported (sponsor's user "Updated time" column
  provides `updated`).
- User-facing "Created time" (family) and "Creation Time" (child) map to
  `created`. The sponsor table has no user-created column, so its `created`
  is the Zite system `zite_created_at` (confirmed correct for ID ordering).
- Where both a user `created` and `zite_created_at` exist, they are
  cross-checked; divergence is logged (not fatal).
- Columns not listed in the config are dropped (explicit allow-list).

### 5.2 Validate (pandera + polars)

Each table gets a pandera schema built from config:

- Per-column type check from the declared `type`.
- `null` handling: nulls are replaced by the `default` when one is declared;
  nulls in columns without a default are validation failures for that cell.

### 5.3 Clean

Automatic, deterministic fixes applied before validation where possible:

- **Names** (`type: name`): title case, preserving apostrophes/hyphens
  (e.g. `o'brien` -> `O'Brien`, `smith-jones` -> `Smith-Jones`).
- **Emails** (`type: email`): lowercased; must match an RFC 5322 subset regex.
  Invalid emails are validation failures.
- **US phones** (`type: phone_us`): strip non-digits; accept 10 digits, or 11
  digits starting with `1`; normalize to `(XXX) XXX-XXXX`. Anything else is a
  validation failure. Non-US values are not supported by this type.
- **Dates** (`type: date`): accept `MM/DD/YYYY` and ISO `YYYY-MM-DD`, normalize
  to ISO. **Datetime** (`type: datetime`): ISO 8601 with timezone.
- **Enum** (`type: enum`): value must be in the declared `values` — either an
  inline list or a `config.vocabularies` entry (Appendix A). Values are kept
  exactly as Zite stores them (labels, case-sensitive); a value outside the
  vocabulary is a validation failure for that cell.

Failure handling:

- Structural failures (missing/extra columns, unparseable table) abort the
  run.
- Cell-level failures after cleaning exclude that row from the raw sheets and
  append it to the log (`log_file`) with table, column, value, and reason.
  Run summary (rows pulled / kept / rejected) is also logged.

### 5.4 Family dedup

The `family` table may contain multiple rows per `primary_email`:

- Group by normalized `primary_email`.
- Coalesce: for each column, take the first non-null value across the group's
  rows (rows ordered by `created` ascending).
- If two rows have conflicting non-null values for the same column, keep the
  value from the earliest `created` row and log the conflict for review.
- The surviving row keeps the earliest `created` timestamp of the group.
- Every merged duplicate is recorded in the `dedup_log` sheet — one row per
  dropped duplicate record — with: `sheet` ("family"), `primary_email`,
  `kept_zite_record_id`/`kept_created`, `duplicate_zite_record_id`/
  `duplicate_created`, `conflict_columns` and `conflict_details` (columns
  where the duplicate's non-null value differs from the surviving value;
  identity/ordering metadata `zite_record_id`, `created`, and
  `zite_created_at` are excluded), and `kept_record`/`duplicate_record`
  (the full recorded contents of both rows as JSON). Rows without an
  email are never merged and never logged.

### 5.4b Child dedup

The `child` table may contain multiple submissions for the same child in
the same family:

- Group by (`primary_email`, `first_name`) on cleaned rows before
  validation.
- Keep the most recent submission (latest `created`, ties broken by
  `zite_record_id`) as-is — no coalescing, unlike families.
- Every dropped duplicate submission is recorded in the `dedup_log`
  sheet exactly like the family rows, with `sheet` set to `"child"`.
- Rows without an email or without a first name are never merged and
  never logged.

### 5.5 Derived columns and IDs

- `age_at_xmas` (child): integer years between `dob` and `xmas_date` from
  config. If the source `age_reported` differs from the computed value, the
  mismatch is logged and passed as an input to AI screening (§6.2).
- `pref_age_min` / `pref_age_max` (sponsor): exact mapping from the
  `age_preference` vocabulary (no free-text parsing needed):

  | `age_preference` label                   | min | max |
  |------------------------------------------|-----|-----|
  | Any Age                                  | 0   | 18  |
  | Newborn through Toddler (0-3 yrs)        | 0   | 3   |
  | Pre-K through Grade 5 (4-10 yrs)         | 4   | 10  |
  | Middle School (11-13 yrs)                | 11  | 13  |
  | High School (14-18+ yrs)                 | 14  | 18  |
  | Special Needs (any age)                  | 0   | 18  |

  "Special Needs (any age)" additionally marks the sponsor as a
  special-needs sponsor: matching against children flagged `special_needs`
  (set in the human audit tool, §7) is that sponsor's highest preference
  (§8.3).
- `family_id` (family): sequential integers starting at 1, ordered by earliest
  `created` then `primary_email` (tie-break for stability).
- `sponsor_id` (sponsor): sequential integers starting at 1, ordered by
  earliest `created` then `email` (tie-break for stability).
- `angel_id` (child): `<family_id><letter>` (e.g. `1A`, `12C`). Children are
  linked to families via `primary_email`. Within a family, letters start at
  `A` and are assigned by descending `age_at_xmas` (oldest child gets `A`);
  tie-break by earliest `created` then `dob`. Letters exceed `Z` as `AA`,
  `AB`, ... (practically unreachable).
- **Orphaned children**: children whose `primary_email` matches no family
  row have no `family_id` and `angel_id = null`. They are **kept in
  `raw_child`** (not dropped) and logged. The audit tool flags them for
  clarification automatically (§7.2); they never receive an `angel_id`
  until a matching family registration exists, and therefore never
  participate in matchmaking (§8.1).

### 5.6 Sibling DOB proximity check

After family linking and angel-id assignment, every pair of children
sharing a `family_id` whose birth dates are at most
`acquire.sibling_dob_max_gap_days` days apart (default 274, ~9 months;
twins included) is recorded for human review in the
`dob_conflict_log` sheet — one row per conflicting pair, with:

- `family_id`, `primary_email`.
- `child_a_zite_record_id`, `child_a_angel_id`, `child_a_first_name`,
  `child_a_dob` (earlier birth) and the same `child_b_*` columns.
- `gap_months` (completed calendar months, day-of-month aware; twins
  record 0) and `gap_days` (absolute day difference).

Children without a `family_id` (orphans) or without a `dob` are skipped.
Each conflict is also logged as a warning.  The check is informational:
it never rejects rows and never blocks the run.

### 5.7 Persistence and re-run semantics

- `raw_sponsor`, `raw_family`, `raw_child` are fully rebuilt on every run.
- ID assignment is deterministic (ordering rules above), so re-runs produce
  stable IDs for existing rows; genuinely new Zite records receive new IDs.
- `adj_child`, `final_child`, `final_family`, `final_sponsor`, `match_report`
  are never touched by this module.
- Validation: after write, read back and confirm the `final_*` sheets are
  byte-identical to before the run (guard against accidental corruption).

## 6. Module 2 — AI Screening

Entry point: `src/ccat/screening.py`, CLI `ccat screen [--config PATH]`.

### 6.1 Input

Reads `raw_child` (all children). Reads `adj_child` (existing
zite_record_ids, to skip). Reads `final_child` only to log how many children
are already audited (no writes).

### 6.2 Screening logic (LLM-assisted, via opencode python SDK)

For each child row not already present in `adj_child`:

- Build a prompt from `config.ai_screening.system_prompt` (full text in
  Appendix B) populated with the child's `zite_record_id`, `dob`, computed
  `age_at_xmas`, reported `age_reported`, `gender`, every size field with its
  type (`shoe_size`/`shoe_size_type`, `pant_waist_size`, `pant_inseam`,
  `pant_size_letter`/`pant_size_type`, `jean_size`,
  `shirt_size_letter`/`shirt_size_type`), and the needs/desires (`need_1`,
  `need_2`, `desire_1`, `desire_2`).
- **Privacy: the child's name is never passed to the LLM.** The prompt
  contains only the pseudonymous `zite_record_id` and the screening-relevant
  fields — no `first_name`, no contact details, no family information.
  `zite_record_id` is the sole identifier in the prompt so the response can
  be joined back to the row. Orphaned children (no family, §5.5) are
  screened like any other — screening does not depend on `angel_id`.
- The model must adjudicate (rules and infant threshold encoded in the
  prompt, Appendix B):
  1. **DOB sanity**: cross-check computed `age_at_xmas` against
     `age_reported` and against the size values/types — e.g. non-infant
     sizes on a child younger than 2, or adult sizes on a toddler, mean the
     reported `dob` may be wrong.
  2. **Needs/desires clarity**: spellcheck and reword unclear text in
     `need_1`, `need_2`, `desire_1`, `desire_2`.
- **Output contract**: the model returns a single JSON object that is
  directly usable as one `adj_child` row — exactly the keys
  `zite_record_id`, `dob`, `need_1`, `need_2`, `desire_1`, `desire_2`,
  `shoe_size`, `shoe_size_type`, `pant_waist_size`, `pant_inseam`,
  `pant_size_letter`, `pant_size_type`, `jean_size`, `shirt_size_letter`,
  `shirt_size_type`, `potential_anomoly`:
  - a key is null when the adjudicated value equals the raw value (no
    change),
  - `potential_anomoly` is null when no age/size discrepancy was found,
  - if every key is null, the model returns `null` for the whole row —
    the pipeline then writes the `adj_child` row anyway with all-null
    adjudication cells, so the row's screening status is recorded (see
    §6.3),
  - size values must use the exact Zite vocabulary labels (Appendix A).
- The response is validated against a pydantic schema (exact key set, enum
  vocabularies, dob date format) before it is accepted.
- One SDK session per run; one request per child; a malformed response is
  retried once, then the child is logged as unscreened (its row is simply not
  written to `adj_child`; it will be picked up on the next run).

### 6.3 Output — `adj_child` sheet

- Columns: `zite_record_id` (key), the adjudicable child columns (`dob`,
  `need_1`, `need_2`, `desire_1`, `desire_2`, size columns), and
  `potential_anomoly`. The model's JSON object maps 1:1 onto this row.
- **Sparse cells, always-present rows**: a cell is null when the adjudicated
  value equals the raw value (no change) — but **every screened child gets
  a row**, even when no field changed. The presence of a `zite_record_id`
  row in `adj_child` is the pipeline's record that the corresponding
  `raw_child` row has been screened; an all-null row means "checked, no
  changes".
- Rows are only appended for zite_record_ids not already present in
  `adj_child`. Existing adj rows are never modified by this module (the
  human audit tool owns edits from here on).
- Regeneration: `ccat screen --redo ZITE_RECORD_ID` re-screens one child and
  replaces its adj row (explicit opt-in only). `ccat screen --overwrite`
  re-screens every child and replaces all their adj rows (ignores the
  existing sheet).
- `zite_record_id` is the join key between `raw_child` and `adj_child` —
  and between both and `final_child` (§7.3). It is stable even when
  `angel_id` cannot be assigned (orphaned children, §5.5).

## 7. Module 3 — Human Audit Tool

Entry point: `src/ccat/audit/server.py`, CLI `ccat audit [--config PATH]`.

Super lightweight local web page: Python stdlib `http.server` serving one
static HTML page plus a small JSON API. No JS frameworks, no extra
dependencies, single human user.

### 7.1 API

- `GET /` — single-page UI (`src/ccat/audit/static/index.html`).
- `GET /api/queue` — children to review: every child in `raw_child` whose
  `zite_record_id` has no row in `final_child`, or has a `final_child` row
  with `audited == false`. Each item: `{zite_record_id, angel_id, raw:
  {...}, adj: {...}}` where `raw` is the full raw row and `adj` is the
  `adj_child` row (all-null fields when the child was screened with no
  changes; `null` object when the child has not been screened yet).
  `angel_id` is null for orphaned children.
- `PUT /api/children/{zite_record_id}` — body: edited adj JSON. Server
  validates (JSON parses; keys are known child columns), then writes the
  child's `final_child` row (see 7.3).

### 7.2 UI

- Two columns side by side: raw child JSON (read-only, syntax-highlighted) on
  the left; adjudicated JSON (editable textarea) on the right, pre-filled
  with the `adj_child` values merged over the raw values.
- Above the columns, a **"Child is special needs"** checkbox. It sets the
  `special_needs` boolean on the `final_child` row (§7.3), which drives the
  "Special Needs (any age)" sponsor preference in matchmaking (§8.3).
- **Orphaned children** (null `angel_id`, no family registration, §5.5):
  always shown in the queue with a prominent warning, and
  `require_clarification` is pre-filled with "No matching family
  registration for this child's primary email" (the human may edit or add
  to it). An orphaned child cannot be marked `audited = true` — the
  **Mark audited** button is disabled until the child has an `angel_id`
  (i.e. a matching family appears in a later `ccat acquire` run); the
  human may still save the row with `audited = false`. Once a family
  registration appears, `ccat acquire` assigns the `angel_id` and the
  human can complete the audit.
- Buttons:
  - **Save (needs clarification)** — writes the row with `audited = false` and
    any `require_clarification` note.
  - **Mark audited** — writes the row with `audited = true` (clears
    `require_clarification` if empty).
- Navigation: next/previous unaudited child; queue length display; the current
  child is never lost on save (page stays on the same angel).
- The tool never displays children whose `final_child` row exists with
  `audited == true` (`ccat audit --overwrite` re-queues audited children
  and allows re-saving frozen rows; the `audited`/`audited_at` columns
  stop gating anything for that session).

### 7.3 `final_child` row

Built at save time by **joining `raw_child` and `adj_child` on
`zite_record_id`** (the ID is the join key between the sheets) and
overlaying: start from the full `raw_child` row, replace any field whose
adj value is non-null, always set:

- `zite_record_id` (key), `angel_id` (copied from raw; null for orphans),
  all raw child columns (with overrides applied),
- `special_needs` (bool, from the audit UI checkbox, default false),
- `audited` (bool),
- `require_clarification` (optional free text, null when none),
- `audited_at` (ISO timestamp of the save).

Re-save rules: while `audited == false`, the tool may overwrite the child's
`final_child` row (human iterating). Once `audited == true`, the row is frozen:
`PUT` for that zite_record_id returns 409 and the child disappears from the
queue. Corrections after audit require rerunning matchmaking or manual
spreadsheet edit (out of pipeline scope, documented in README later).

`final_family` / `final_sponsor` are never touched.

## 8. Module 4 — Matchmaking

Entry point: `src/ccat/match.py`, CLI `ccat match [--config PATH]`.

### 8.1 Input

- `final_child` (only rows with `audited == true` **and** a non-null
  `angel_id` participate — orphaned children, §5.5, are excluded),
- `raw_family`, `raw_sponsor`.

### 8.2 Constraints

- **Family indivisibility**: all children of a family go to exactly one
  sponsor. Never split a family. A sponsor may receive multiple families.
- **Exact count**: the sum of children across a sponsor's assigned families
  must equal the sponsor's `num_children` exactly. Families that cannot be
  placed in any exact combination remain unmatched (reported in
  `match_report`).
- **Sponsor class ordering**: sponsors whose `age_preference` is "Special
  Needs (any age)" are matched first (their special-needs preference is the
  highest preference, §8.3). Then `Individual` sponsors, then `Group` and
  `Organization` sponsors together — `Group` is treated as an organization
  class. Within each phase, sponsors are ordered by `sponsor_id` ascending.
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
  `Individual`, then `Group` and `Organization` together (Group treated as
  organization), each ordered by `sponsor_id` ascending (creation order).
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
  be split between a pre-assignment and the pool).
- Pre-assigned sponsors do **not** participate in pool matchmaking, and
  their pre-assigned children are removed from the pool available to
  everyone else.
- Their `final_sponsor` row carries the reconciled `assigned_angel_ids`.

### 8.6 Output

- `final_family`: one row per family — all `raw_family` columns, `family_id`,
  `assigned_sponsor_id` (null if unmatched), `assigned_angel_ids`
  (comma-separated), and `num_angels_in_family` (count of `final_child`
  rows sharing the family's `family_id`; 0 when none).
- `final_sponsor`: one row per sponsor — all `raw_sponsor` columns,
  `sponsor_id`, `assigned_angel_ids` (comma-separated list of every `angel_id`
  sponsored across all assigned families and pre-assignments, sorted),
  and `num_angels_assigned` (count of angel ids in `assigned_angel_ids`;
  0 when none).
  Pre-assigned sponsors appear with their reconciled manual lists.
- `match_report`: unmatched families and sponsors with reasons, plus
  pre-assignment problems (unknown ids, duplicates, split families).
  Also exported as CSV to `config.match_report_csv` for the program
  coordinator.
- This module is the sole owner of these three sheets and fully regenerates
  them on each run; the previous contents are archived to timestamped sheets
  (`final_family_YYYYMMDD_HHMMSS`, etc.) before replacement, so prior
  matchings are never silently lost. `ccat match --overwrite` skips the
  archiving and replaces the sheets in place.
- `final_child` is read-only here (never rewritten).

## 9. Idempotency and Ground-Truth Protection

Ownership matrix — a module may only write cells in its own column:

| Sheet           | acquire | screen | audit | match | generate |
|-----------------|---------|--------|-------|-------|----------|
| `raw_*`         | write   | read   | read  | read  | -        |
| `dedup_log`     | write   | read   | read  | read  | -        |
| `dob_conflict_log` | write | read   | read  | read  | -        |
| `adj_child`     | -       | append-new / `--redo` | read | - | - |
| `final_child`   | -       | -      | write (while unaudited) | read | write (`generated_info_sheet` only) |
| `final_family`  | -       | -      | -     | write | read     |
| `final_sponsor` | -       | -      | -     | write | read     |
| `match_report`  | -       | -      | -     | write | -        |

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
  acquire.py           # module 1
  screening.py         # module 2
  match.py             # module 4
  child_info_generate.py  # module 5
  sponsor_packets.py   # module 5b: sponsor email draft + combined packet PDF
  stats.py             # module 6
  audit/
    __init__.py
    server.py          # module 3: http.server + JSON API
    static/
      index.html       # single-page UI, vanilla JS
test/
  test_config.py
  test_acquire.py      # validation, cleaning, dedup, IDs, angel_id rules, orphans, dedup_log, dob_conflict_log
  test_screening.py     # prompt build (name excluded), always-row output, retry/skip logic
  test_audit.py         # queue filter, merge rules, orphan flagging, frozen-row 409
  test_match.py         # indivisibility, exact counts, phase order, scoring
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
- ~~Affiliation ordering~~ — `Group` is treated as an organization; phases
  are special-needs, Individual, then Group + Organization (§8.2).
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
`sponsor.*` placeholders.

### 13.3 `generated_info_sheet` flag

- New `final_child` rows carry `generated_info_sheet = FALSE`
  (defaulted in module 3's row builder and sheet frame).
- The flag is Boolean; only module 5 writes it (`WRITABLE_SHEETS`,
  §9).  Module 3 preserves the existing value on re-saves of the same
  child.
- First run adds the column (all `FALSE`) when absent; nulls normalize
  to `FALSE`.

### 13.4 Run semantics

- Only rows whose family has an **assigned sponsor** (`assigned_sponsor_id`
  non-null) and whose `generated_info_sheet == FALSE` are processed (every
  sponsored row with `ccat generate --overwrite`, ignoring the flag column);
  unsponsored children are skipped (the sheet's purpose is the sponsor
  packet).  The output is
  `output/child_info_sheets/<location>/AT<season>_<angel_id>.pdf`, where
  `<location>` is the angel's family pickup location reduced to its
  canonical bucket via `config.location_groups` (`hancock`/`calumet`;
  unmapped labels go to `unknown`) — existing files are overwritten, so
  generation is idempotent.
- Per-row failures are logged and skipped; after the loop the flags of
  successful rows flip to `TRUE` in one atomic sheet write, then a typed
  `InfoSheetError` reports any failed angel ids.  Reruns pick up exactly
  the failed rows.
- `--test` stops after the first successfully generated sheet.
- Pending rows need a non-null `zite_record_id` and a safe `angel_id`
  filename (no separators); missing family/sponsor rows or duplicate
  `family_id`/`sponsor_id`s in `final_family`/`final_sponsor` are hard
  errors.
- The enrichment columns (`gift_pickup_location`, `assigned_sponsor_id`,
  `family_size`, and the `sponsor_*` join/computed columns) are never
  written back to the workbook; `final_child` gains only
  `generated_info_sheet`.

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
| Affiliation | Individual, Group, Organization |
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
