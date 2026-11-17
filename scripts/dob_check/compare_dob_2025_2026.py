"""Compare child DOBs between the 2025 and 2026 CCAT workbooks.

Ground truth for 2025 lives in two sheets of ``output/ccat_2025.xlsx``
with the same row layout but different header labels:
``CCAM Reduced`` (long Google-form labels) and ``TSA Reduced`` (short
labels, with ``Last Name`` reused for both the registering person and
the child). Family contact details are only filled on the first row of
each family and are forward-filled within a family group. Blank
family-number rows mean different things per sheet: TSA rows continue
the previous family (child last name left blank on those rows), while
CCAM rows are standalone carry-over roster entries that inherit
nothing. The 2026 side is read from the ``final_child`` and
``final_family`` sheets of ``output/ccat_2026.xlsx``.

Matching is tiered so that renamed or hand-entered variants still line
up, with a provenance column (``matched_via``) on every match:

1. ``(reg-last, first)`` of 2025 vs (family-last, first) of 2026,
2. ``(child-last, first)`` fallback (needed for 2025 carry-over rows
   that never name a registering person),
3. fuzzy first-name variants within the same last-name anchor
   (flagged ``fuzzy_name`` for human confirmation).

Every child from either year lands on a year sheet; matched pairs go
to the ``dob_comparison`` sheet where a ``status`` column flags
``MATCH`` / ``DOB_MISMATCH`` / ``MISSING_DOB_2025`` / ``MISSING_DOB_2026``
/ ``INVALID_DOB_2025`` / ``INVALID_DOB_2026`` / ``AMBIGUOUS``.

The script is a pure function of its two input workbooks: no child
names, IDs, or row offsets are hardcoded, so new children added to
``ccat_2026.xlsx`` are picked up by simply rerunning.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

if TYPE_CHECKING:
    from openpyxl.worksheet.worksheet import Worksheet


class _FastexcelDtypeFilter(logging.Filter):
    """Drop fastexcel's all-null-column dtype fallback warnings.

    ``pl.read_excel`` logs a WARNING per column it cannot infer a dtype
    for (all-null columns); falling back to String is desired here, so
    the warning carries no information.  Mirrors the filter in
    ``src/ccat/io.py``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False for dtype-fallback records, True otherwise."""
        return "Could not determine dtype" not in record.getMessage()


logging.getLogger("fastexcel").addFilter(_FastexcelDtypeFilter())
# A logger level applies to descendants while a filter would not, so
# keep both the same way ``src/ccat/io.py`` does.
logging.getLogger("fastexcel").setLevel(logging.ERROR)

DEFAULT_FILE_2025 = Path("output/ccat_2025.xlsx")
DEFAULT_FILE_2026 = Path("output/ccat_2026.xlsx")
DEFAULT_OUTPUT = Path("output/2025_vs_2026_dob.xlsx")

#: 2026 sheets holding audited child and registering-family data.
SHEET_2026_CHILD = "final_child"
SHEET_2026_FAMILY = "final_family"

#: Minimum effective SequenceMatcher ratio for the fuzzy tier. 0.83
#: admits single-character swaps (Cayden/Kayden) and, with the prefix
#: bonus below, joined names (RuubenJay/Ruuben); probing the 2025 and
#: 2026 rosters showed no unrelated name pairs scoring this high.
FUZZY_MIN_RATIO = 0.83
#: Bonus applied when one first name is a prefix of the other, so
#: concatenated or middle-added names stay comparable.
FUZZY_PREFIX_BONUS = 0.05
#: Candidates within this score margin of the best one are considered
#: tied and the pairing is flagged AMBIGUOUS for human resolution.
FUZZY_TIE_MARGIN = 0.02

#: Sort sentinel for candidate pairs where a DOB cannot be compared.
_MISSING_DELTA = 10**9

#: 2025 CCAM Reduced header labels -> logical fields. The labels are
#: literal (:class:`Sheet2025Spec` strips whitespace before compare).
CCAM_COLUMNS: dict[str, tuple[str, int]] = {
    "family_number": ("Family #", 0),
    "child_tag": ("Child Tag", 0),
    "reg_first_name": ("Person Registering Child, First Name", 0),
    "reg_last_name": ("Person registering child, Last Name.", 0),
    "reg_email": ("Your email address.", 0),
    "reg_phone": ("Your phone number.", 0),
    "first_name": ("First Name of child", 0),
    "last_name": ("Last name of child.", 0),
    "dob": ("Birthday of child", 0),
    "age": ("Age", 0),
}
#: 2025 TSA Reduced header labels -> logical fields. TSA uses short
#: labels; ``Last Name`` appears twice (registering person first, then
#: child), so the occurrence index disambiguates them.
TSA_COLUMNS: dict[str, tuple[str, int]] = {
    "family_number": ("Family", 0),
    "child_tag": ("Child Tag", 0),
    "reg_first_name": ("First Name", 0),
    "reg_last_name": ("Last Name", 0),
    "reg_email": ("email", 0),
    "reg_phone": ("Phone", 0),
    "first_name": ("First name", 0),
    "last_name": ("Last Name", 1),
    "dob": ("DOB", 0),
    "age": ("Age", 0),
}

#: Reg-person fields forward-filled within a family group.
_FILL_FIELDS = (
    "reg_first_name",
    "reg_last_name",
    "reg_email",
    "reg_phone",
)


@dataclass(frozen=True)
class Sheet2025Spec:
    """Layout spec for one 2025 child sheet.

    Attributes:
        sentinel: Stripped header cell that identifies the header row.
        columns: Logical field -> (exact header label, occurrence among
            repeats of that label).
        inherit_family_on_none: Whether rows with a blank family
            number continue the previous family (TSA Reduced) instead
            of standing alone as carry-over roster entries (CCAM
            Reduced).
    """

    sentinel: str
    columns: dict[str, tuple[str, int]]
    inherit_family_on_none: bool


#: The 2025 ground-truth sheets and their layout specs. The two sheets
#: share the same row layout (meta rows, header, data) but use
#: different header labels and family-continuation semantics.
SHEETS_2025: dict[str, Sheet2025Spec] = {
    "CCAM Reduced": Sheet2025Spec(
        sentinel="First Name of child",
        columns=CCAM_COLUMNS,
        inherit_family_on_none=False,
    ),
    "TSA Reduced": Sheet2025Spec(
        sentinel="DOB",
        columns=TSA_COLUMNS,
        inherit_family_on_none=True,
    ),
}

_PAREN_RE = re.compile(r"\([^)]*\)")
_STRIP_RE = re.compile(r"[\s'\-.]+")

_DOB_FORMATS = (
    "%m/%d/%Y",
    "%m/%d/%y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
)

#: Excel built-in style colors used for the status column.
_FILL_GREEN = PatternFill("solid", fgColor="C6EFCE")
_FILL_RED = PatternFill("solid", fgColor="FFC7CE")
_FILL_AMBER = PatternFill("solid", fgColor="FFEB9C")
_FONT_GREEN = Font(color="006100")
_FONT_RED = Font(color="9C0006")
_FONT_AMBER = Font(color="9C6500")
_FONT_BOLD = Font(bold=True)


# ---------------------------------------------------------------------------
# Value helpers (pure, unit-testable).
# ---------------------------------------------------------------------------


def normalize_name(value: object) -> str:
    """Normalize a person name for matching.

    Casefolds, drops parenthetical asides (``Riley ( girl)`` ->
    ``riley``), and removes whitespace, apostrophes, hyphens, and
    periods so that ``Heikkinen- krans`` and ``O'Connell`` match their
    differently spelled variants.

    Args:
        value: Raw cell value; None treated as empty.

    Returns:
        Normalized key fragment (possibly empty).
    """
    if value is None:
        return ""
    text = _PAREN_RE.sub("", str(value).strip().casefold())
    return _STRIP_RE.sub("", text)


def _text(value: object) -> str:
    """Coerce a cell value to a clean display string.

    Args:
        value: Raw cell value; None becomes empty.

    Returns:
        Stripped string; integral floats lose their ``.0``.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _maybe_int(value: object) -> int | None:
    """Coerce a cell value to int when it is a plain whole number.

    Args:
        value: Raw cell value.

    Returns:
        Integer value, or None when the cell is blank or non-numeric.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = _text(value)
    if text.isdigit():
        return int(text)
    return None


def parse_dob(value: object) -> date | None:
    """Parse a date of birth from an Excel cell or raw string.

    Args:
        value: Cell value -- datetime, date, or text in any of the
            common layouts seen in the CCAT workbooks.

    Returns:
        Parsed date, or None when the value is blank or unparsable
        (callers distinguish the two via :func:`_text` on the raw
        value).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _DOB_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _dob_display(value: object) -> str:
    """Render a raw DOB cell as text for the output sheets.

    Args:
        value: Raw cell value.

    Returns:
        ISO date string, the trimmed raw text, or empty.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _text(value)


def _delta_days(left: date | None, right: date | None) -> int | None:
    """Absolute day distance between two dates.

    Args:
        left: First date (may be None).
        right: Second date (may be None).

    Returns:
        Day distance, or None when either date is missing.
    """
    if left is None or right is None:
        return None
    return abs((right - left).days)


def _fuzzy_score(left: str, right: str) -> float:
    """Similarity score for two normalized first names.

    Applies a prefix bonus so concatenated names ('ruubenjay' vs
    'ruuben') stay comparable to single edits.

    Args:
        left: Normalized 2025 first name.
        right: Normalized 2026 first name.

    Returns:
        Similarity score in [0, 1].
    """
    if not left or not right:
        return 0.0
    ratio = SequenceMatcher(None, left, right).ratio()
    shorter, longer = (
        (left, right) if len(left) <= len(right) else (right, left)
    )
    if longer.startswith(shorter) and len(shorter) >= 3:
        ratio = min(1.0, ratio + FUZZY_PREFIX_BONUS)
    return ratio


def _name_tokens(value: object) -> tuple[str, ...]:
    """Split a display name into fuzzy-comparison tokens.

    Args:
        value: Raw name string.

    Returns:
        Lower-case tokens of three or more characters.
    """
    if value is None:
        return ()
    text = _PAREN_RE.sub("", str(value).strip().casefold())
    return tuple(
        token for token in re.split(r"[\s'\-.]+", text) if len(token) >= 3
    )


def _fuzzy_token_score(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    """Best single-token similarity score between two name token sets.

    Catches names like ``Jannatul Juwayriyah`` vs ``Juwayriyah`` whose
    concatenated forms score too low for the threshold.

    Args:
        left: First name's tokens.
        right: Other first name's tokens.

    Returns:
        Best ratio in [0, 1], or 0.0 when either side has no tokens.
    """
    if not left or not right:
        return 0.0
    return max(SequenceMatcher(None, a, b).ratio() for a in left for b in right)


# ---------------------------------------------------------------------------
# Record dataclasses.
# ---------------------------------------------------------------------------


@dataclass
class Child2026:
    """One child row from the 2026 ``final_child`` sheet."""

    zite_record_id: str
    angel_id: str
    family_id: int | None
    first_name: str
    dob_raw: object
    dob: date | None
    gender: str
    age_at_xmas: int | None
    family_last_name: str
    family_first_name: str
    phone: str
    email: str
    gift_pickup_location: str
    key_first: str = ""
    key_family_last: str = ""
    first_name_tokens: tuple[str, ...] = ()
    matched_via: str = ""
    forced_ambiguous: bool = False
    fuzzy_score: float | None = None
    counterpart: Child2025 | None = None
    match_status: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class Child2025:
    """One child row from the 2025 ``CCAM Reduced`` sheet."""

    row_number: int
    family_number: int | None
    child_tag: str
    reg_first_name: str
    reg_last_name: str
    reg_email: str
    reg_phone: str
    first_name: str
    last_name: str
    dob_raw: object
    dob: date | None
    age: int | None
    source_sheet: str = ""
    key_first: str = ""
    key_reg_last: str = ""
    key_child_last: str = ""
    first_name_tokens: tuple[str, ...] = ()
    representative: bool = True
    duplicate_count: int = 1
    matched_via: str = ""
    forced_ambiguous: bool = False
    fuzzy_score: float | None = None
    counterpart: Child2026 | None = None
    duplicate_of: Child2025 | None = None
    match_status: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class MatchedPair:
    """A resolved (2025 child, 2026 child) pairing."""

    child_2025: Child2025
    child_2026: Child2026
    matched_via: str
    forced_ambiguous: bool
    score: float | None = None


# ---------------------------------------------------------------------------
# Readers.
# ---------------------------------------------------------------------------


def _parse_sheet_2025(
    sheet_name: str,
    spec: Sheet2025Spec,
    rows: list[tuple[object, ...]],
    path: Path,
) -> list[Child2025]:
    """Parse one 2025 child sheet into records.

    Family contact details are only filled on the first row of a
    family, so reg-person fields are forward-filled within a family
    group. Blank family-number rows either continue the previous
    family (TSA, which also leaves the child's last name blank on
    those rows) or stand alone as carry-over roster entries (CCAM,
    which never inherit contact details).

    Args:
        sheet_name: Sheet name, for errors and provenance.
        spec: Layout spec for this sheet.
        rows: Raw sheet rows.
        path: Workbook path, for error messages.

    Returns:
        One Child2025 per row that names a child, in sheet order.

    Raises:
        ValueError: If the header row or any required column is
            missing.
    """
    header_index = None
    for index, row in enumerate(rows[:20]):
        cells = [_text(cell) for cell in row]
        if spec.sentinel in cells:
            header_index = index
            break
    if header_index is None:
        raise ValueError(
            f"no header row containing {spec.sentinel!r} found in "
            f"{sheet_name} of {path}"
        )

    header_cells = [_text(cell) for cell in rows[header_index]]
    label_index: dict[str, list[int]] = defaultdict(list)
    for position, label in enumerate(header_cells):
        if label:
            label_index[label].append(position)
    column_map: dict[str, int] = {}
    for logical, (label, occurrence) in spec.columns.items():
        positions = label_index.get(label, [])
        if occurrence >= len(positions):
            raise ValueError(
                f"column {label!r} occurrence {occurrence} missing "
                f"from {sheet_name} in {path}"
            )
        column_map[logical] = positions[occurrence]

    def cell(row: tuple[object, ...], logical: str) -> object:
        index = column_map[logical]
        return row[index] if index < len(row) else None

    children: list[Child2025] = []
    current_family: int | None = None
    fill = {name: "" for name in _FILL_FIELDS}
    last_child_last = ""
    for offset, row in enumerate(
        rows[header_index + 1 :], start=header_index + 2
    ):
        if _text(cell(row, "first_name")) == "":
            continue
        raw_family = _maybe_int(cell(row, "family_number"))
        if raw_family is not None:
            family = raw_family
            if family != current_family:
                current_family = family
                fill = {name: "" for name in _FILL_FIELDS}
                last_child_last = ""
        elif spec.inherit_family_on_none:
            family = current_family
        else:
            family = None
            current_family = None
            fill = {name: "" for name in _FILL_FIELDS}
            last_child_last = ""
        if raw_family is not None:
            for logical in _FILL_FIELDS:
                value = _text(cell(row, logical))
                if value:
                    fill[logical] = value
        own_last_name = _text(cell(row, "last_name"))
        if own_last_name:
            last_name = own_last_name
            if family is not None:
                last_child_last = own_last_name
        else:
            last_name = last_child_last if family is not None else ""
        child = Child2025(
            row_number=offset,
            family_number=family,
            child_tag=_text(cell(row, "child_tag")),
            reg_first_name=fill["reg_first_name"],
            reg_last_name=fill["reg_last_name"],
            reg_email=fill["reg_email"],
            reg_phone=fill["reg_phone"],
            first_name=_text(cell(row, "first_name")),
            last_name=last_name,
            dob_raw=cell(row, "dob"),
            dob=parse_dob(cell(row, "dob")),
            age=_maybe_int(cell(row, "age")),
            source_sheet=sheet_name,
        )
        child.key_first = normalize_name(child.first_name)
        child.key_reg_last = normalize_name(child.reg_last_name)
        child.key_child_last = normalize_name(child.last_name)
        child.first_name_tokens = _name_tokens(child.first_name)
        children.append(child)
    return children


def load_children_2025(path: Path) -> list[Child2025]:
    """Read every child row from the 2025 ground-truth sheets.

    Both ``CCAM Reduced`` and ``TSA Reduced`` share the same row
    layout (meta rows, header row, blank separator, then one row per
    child), but their header labels differ; :data:`SHEETS_2025`
    carries one spec per sheet. TSA rows with a blank family number
    continue the previous family; blank CCAM family numbers mark
    standalone carry-over roster entries.

    Args:
        path: Workbook path.

    Returns:
        One Child2025 per row that names a child, by sheet then sheet
        order.

    Raises:
        FileNotFoundError: If the workbook does not exist.
        ValueError: If a required sheet, header row, or column is
            missing.
    """
    if not path.is_file():
        raise FileNotFoundError(f"2025 workbook not found: {path}")
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        missing = [
            name for name in SHEETS_2025 if name not in workbook.sheetnames
        ]
        if missing:
            raise ValueError(
                f"sheet(s) {missing} missing from {path}; found "
                f"{workbook.sheetnames}"
            )
        raw_rows = {
            name: list(workbook[name].iter_rows(values_only=True))
            for name in SHEETS_2025
        }
    finally:
        workbook.close()

    children: list[Child2025] = []
    for sheet_name, spec in SHEETS_2025.items():
        children.extend(
            _parse_sheet_2025(sheet_name, spec, raw_rows[sheet_name], path)
        )
    if not children:
        raise ValueError(f"no child rows found in 2025 sheets of {path}")
    return children


def load_children_2026(path: Path) -> list[Child2026]:
    """Read every child row from the 2026 ``final_child`` sheet.

    Family details come from ``final_family`` joined on ``family_id``;
    the registering person's last name there is the matching anchor for
    2025 registrations.

    Args:
        path: Workbook path.

    Returns:
        One Child2026 per child row, in sheet order.

    Raises:
        FileNotFoundError: If the workbook does not exist.
        ValueError: If required sheets or columns are missing.
    """
    if not path.is_file():
        raise FileNotFoundError(f"2026 workbook not found: {path}")
    child_columns = (
        "zite_record_id",
        "first_name",
        "dob",
        "gender",
        "age_at_xmas",
        "family_id",
        "angel_id",
        "primary_email",
        "ignore",
    )
    family_columns = (
        "family_id",
        "first_name",
        "last_name",
        "phone",
        "gift_pickup_location",
        "ignore",
    )
    try:
        child_frame = pl.read_excel(path, sheet_name=SHEET_2026_CHILD)
        family_frame = pl.read_excel(path, sheet_name=SHEET_2026_FAMILY)
    except Exception as exc:  # fastexcel raises bare Exceptions.
        raise ValueError(
            f"cannot read {SHEET_2026_CHILD}/{SHEET_2026_FAMILY} from {path}: {exc}"
        ) from exc
    missing_child = [c for c in child_columns if c not in child_frame.columns]
    if missing_child:
        raise ValueError(
            f"column(s) {missing_child} missing from {SHEET_2026_CHILD} in {path}"
        )
    missing_family = [
        c for c in family_columns if c not in family_frame.columns
    ]
    if missing_family:
        raise ValueError(
            f"column(s) {missing_family} missing from {SHEET_2026_FAMILY} in {path}"
        )
    if child_frame.is_empty():
        raise ValueError(f"{SHEET_2026_CHILD} sheet in {path} is empty")

    eligible_children = child_frame.filter(
        ~pl.col("ignore").fill_null(False) & pl.col("family_id").is_not_null()
    )
    eligible_families = family_frame.filter(~pl.col("ignore").fill_null(False))
    joined = eligible_children.select(list(child_columns)).join(
        eligible_families.select(list(family_columns)),
        on="family_id",
        how="left",
        suffix="_family",
    )
    children: list[Child2026] = []
    for row in joined.iter_rows(named=True):
        child = Child2026(
            zite_record_id=_text(row["zite_record_id"]),
            angel_id=_text(row["angel_id"]),
            family_id=_maybe_int(row["family_id"]),
            first_name=_text(row["first_name"]),
            dob_raw=row["dob"],
            dob=parse_dob(row["dob"]),
            gender=_text(row["gender"]),
            age_at_xmas=_maybe_int(row["age_at_xmas"]),
            family_last_name=_text(row["last_name"]),
            family_first_name=_text(row["first_name_family"]),
            phone=_text(row["phone"]),
            email=_text(row["primary_email"]),
            gift_pickup_location=_text(row["gift_pickup_location"]),
        )
        child.key_first = normalize_name(child.first_name)
        child.key_family_last = normalize_name(child.family_last_name)
        child.first_name_tokens = _name_tokens(child.first_name)
        children.append(child)
    return children


# ---------------------------------------------------------------------------
# Matching.
# ---------------------------------------------------------------------------


def group_duplicate_registrations(children: list[Child2025]) -> None:
    """Mark repeated 2025 rows that describe the same child.

    Rows sharing a normalized first name, an identical parsed DOB, and
    an identical last-name anchor are one child registered twice (the
    workbook contains e.g. the same (Goudge, Zoey, 2016-02-17) under
    two family numbers). The first row becomes the representative that
    participates in matching; the others inherit its result and are
    flagged for the record.

    Args:
        children: Parsed 2025 rows; mutated in place.
    """
    groups: dict[tuple[str, str, str], list[Child2025]] = defaultdict(list)
    for child in children:
        anchor = child.key_reg_last or child.key_child_last
        groups[(child.key_first, str(child.dob), anchor)].append(child)
    for members in groups.values():
        if len(members) < 2:
            continue
        for member in members[1:]:
            member.representative = False
            member.duplicate_of = members[0]
            member.notes.append("duplicate_registration")
        members[0].duplicate_count = len(members)
        members[0].notes.append(f"duplicate_registration ({len(members)}x)")


def _assign_pairs(
    reps: list[Child2025], candidates: list[Child2026]
) -> list[tuple[Child2025, Child2026, bool]]:
    """Greedily pair 2025 rows with 2026 children sharing their key.

    Safe (unforced) pairings are equal-DOB matches and unique 1:1
    bijections; anything else (contended keys, uncompared DOBs) is
    still paired when a best guess exists, but flagged so the
    ``dob_comparison`` sheet marks it AMBIGUOUS rather than trusting
    the pairing.

    Args:
        reps: Pending 2025 representatives for one key.
        candidates: Unmatched 2026 children for the same key.

    Returns:
        (representative, candidate, forced) triples.
    """
    remaining_reps = sorted(reps, key=lambda child: child.row_number)
    remaining_candidates = sorted(
        candidates, key=lambda child: (child.family_id or 0, child.angel_id)
    )
    pairs: list[tuple[Child2025, Child2026, bool]] = []
    while remaining_reps and remaining_candidates:
        ordered = sorted(
            (
                (
                    delta
                    if (delta := _delta_days(rep.dob, cand.dob)) is not None
                    else _MISSING_DELTA,
                    rep.row_number,
                    cand_index,
                    rep,
                    cand,
                )
                for rep in remaining_reps
                for cand_index, cand in enumerate(remaining_candidates)
            )
        )
        _, _, _, rep, cand = ordered[0]
        bijective = len(remaining_reps) == len(remaining_candidates)
        delta = _delta_days(rep.dob, cand.dob)
        forced = delta is None or (delta != 0 and not bijective)
        pairs.append((rep, cand, forced))
        remaining_reps.remove(rep)
        remaining_candidates.remove(cand)
    return pairs


def _apply_pair(
    rep: Child2025,
    cand: Child2026,
    matched_via: str,
    forced: bool,
    score: float | None,
) -> None:
    """Record a pairing on both record objects immediately.

    Assignments must happen inside each tier so later tiers see the
    updated counterpart state and never re-pair the same child.

    Args:
        rep: Matched 2025 representative.
        cand: Matched 2026 child.
        matched_via: Provenance tier label.
        forced: Whether the pairing needs human confirmation.
        score: Fuzzy score when produced by the fuzzy tier.
    """
    rep.counterpart = cand
    rep.matched_via = matched_via
    rep.forced_ambiguous = forced
    rep.fuzzy_score = score
    cand.counterpart = rep
    cand.matched_via = matched_via
    cand.forced_ambiguous = forced
    cand.fuzzy_score = score


def _match_exact_tier(
    reps: list[Child2025],
    children_2026: list[Child2026],
    key_25: str,
    matched_via: str,
) -> list[MatchedPair]:
    """Run one exact-key tier for all still-unmatched representatives.

    Args:
        reps: Unmatched 2025 representatives (caller refilters after
            each tier).
        children_2026: All 2026 children (matched ones skipped
            internally, so per-tier pools stay correct).
        key_25: Which 2025 last-name key to use -- ``reg`` or
            ``child``.
        matched_via: Provenance label recorded on the pair.

    Returns:
        Matched pairs produced by this tier.
    """
    reps_by_key: dict[tuple[str, str], list[Child2025]] = defaultdict(list)
    for rep in reps:
        last_key = rep.key_reg_last if key_25 == "reg" else rep.key_child_last
        if rep.key_first and last_key:
            reps_by_key[(last_key, rep.key_first)].append(rep)
    cands_by_key: dict[tuple[str, str], list[Child2026]] = defaultdict(list)
    for child in children_2026:
        if (
            child.counterpart is None
            and child.key_family_last
            and child.key_first
        ):
            cands_by_key[(child.key_family_last, child.key_first)].append(child)

    pairs: list[MatchedPair] = []
    for key in sorted(reps_by_key):
        tier_candidates = [
            child
            for child in cands_by_key.get(key, [])
            if child.counterpart is None
        ]
        for rep, cand, forced in _assign_pairs(
            reps_by_key[key], tier_candidates
        ):
            _apply_pair(rep, cand, matched_via, forced, None)
            pairs.append(
                MatchedPair(
                    child_2025=rep,
                    child_2026=cand,
                    matched_via=matched_via,
                    forced_ambiguous=forced,
                )
            )
    return pairs


def _match_fuzzy_tier(
    reps: list[Child2025],
    children_2026: list[Child2026],
) -> list[MatchedPair]:
    """Fuzzy-match remaining representatives on first-name variants.

    A 2025 row and a 2026 child are comparable only when the 2026
    family last name equals one of the 2025 last-name anchors; the
    first names are then scored with :func:`_fuzzy_score`. Ties within
    :data:`FUZZY_TIE_MARGIN` of the best score are paired anyway but
    flagged for review.

    Args:
        reps: Still-unmatched 2025 representatives.
        children_2026: All 2026 children (matched ones skipped).

    Returns:
        Matched pairs produced by this tier.
    """
    pairs: list[MatchedPair] = []
    for rep in sorted(reps, key=lambda child: child.row_number):
        anchors = {rep.key_reg_last, rep.key_child_last} - {""}
        if not anchors or not rep.key_first:
            continue
        best_child: Child2026 | None = None
        best_score = 0.0
        runner_up = 0.0
        for child in sorted(
            children_2026,
            key=lambda item: (item.family_id or 0, item.angel_id),
        ):
            if child.counterpart is not None or not child.key_first:
                continue
            if child.key_family_last not in anchors:
                continue
            score = max(
                _fuzzy_score(rep.key_first, child.key_first),
                _fuzzy_token_score(
                    rep.first_name_tokens, child.first_name_tokens
                ),
            )
            if score < FUZZY_MIN_RATIO:
                continue
            if score > best_score:
                runner_up = best_score
                best_score = score
                best_child = child
            elif score > runner_up:
                runner_up = score
        if best_child is None:
            continue
        forced = (best_score - runner_up) <= FUZZY_TIE_MARGIN
        score = round(best_score, 4)
        _apply_pair(rep, best_child, "fuzzy_name", forced, score)
        pairs.append(
            MatchedPair(
                child_2025=rep,
                child_2026=best_child,
                matched_via="fuzzy_name",
                forced_ambiguous=forced,
                score=score,
            )
        )
    return pairs


def match_children(
    children_2025: list[Child2025], children_2026: list[Child2026]
) -> list[MatchedPair]:
    """Match 2025 children to 2026 children using the tier ladder.

    Applies duplicate grouping first, then tiers 1-3 in order; each
    tier only sees rows still unmatched. Matched pairs get their
    counterpart fields filled on both record objects.

    Args:
        children_2025: Parsed 2025 rows.
        children_2026: Parsed 2026 children.

    Returns:
        All matched pairs, one per matched representative.
    """
    group_duplicate_registrations(children_2025)

    def pending() -> list[Child2025]:
        return [
            child
            for child in children_2025
            if child.representative and child.counterpart is None
        ]

    pairs: list[MatchedPair] = []
    pairs.extend(_match_exact_tier(pending(), children_2026, "reg", "reg_last"))
    pairs.extend(
        _match_exact_tier(pending(), children_2026, "child", "child_last")
    )
    pairs.extend(_match_fuzzy_tier(pending(), children_2026))
    return pairs


def comparison_status(rep: Child2025, cand: Child2026) -> str:
    """Status string describing the DOB comparison for one pair.

    Args:
        rep: Matched 2025 representative.
        cand: Matched 2026 child.

    Returns:
        One of ``AMBIGUOUS``, ``MATCH``, ``DOB_MISMATCH``,
        ``MISSING_DOB_2025``, ``MISSING_DOB_2026``,
        ``INVALID_DOB_2025``, ``INVALID_DOB_2026``.
    """
    if rep.forced_ambiguous or cand.forced_ambiguous:
        return "AMBIGUOUS"
    raw_25 = _text(rep.dob_raw)
    raw_26 = _text(cand.dob_raw)
    if rep.dob is None:
        return "INVALID_DOB_2025" if raw_25 else "MISSING_DOB_2025"
    if cand.dob is None:
        return "INVALID_DOB_2026" if raw_26 else "MISSING_DOB_2026"
    if rep.dob == cand.dob:
        return "MATCH"
    return "DOB_MISMATCH"


def finalize_statuses(
    children_2025: list[Child2025], children_2026: list[Child2026]
) -> None:
    """Fill ``match_status`` on every record either side of a pair.

    Duplicate (non-representative) 2025 rows inherit their
    representative's status and counterpart; unmatched rows get
    ``NO_MATCH_2026`` / ``NO_MATCH_2025``.

    Args:
        children_2025: Parsed 2025 rows after matching.
        children_2026: Parsed 2026 children after matching.
    """
    for rep in [c for c in children_2025 if c.representative]:
        rep.match_status = (
            comparison_status(rep, rep.counterpart)
            if rep.counterpart is not None
            else "NO_MATCH_2026"
        )
    for duplicate in children_2025:
        if duplicate.representative:
            continue
        source = duplicate.duplicate_of
        duplicate.match_status = (
            source.match_status
            if source is not None and source.match_status
            else "NO_MATCH_2026"
        )
        if source is not None:
            duplicate.counterpart = source.counterpart
            duplicate.matched_via = source.matched_via
            duplicate.forced_ambiguous = source.forced_ambiguous
            duplicate.fuzzy_score = source.fuzzy_score
    for child in children_2026:
        if child.counterpart is None:
            child.match_status = "NO_MATCH_2025"
        else:
            child.match_status = child.counterpart.match_status


# ---------------------------------------------------------------------------
# Output workbook.
# ---------------------------------------------------------------------------


def _status_style(
    status: str, matched_via: str
) -> tuple[object, object] | None:
    """Pick highlight style for a status cell.

    Args:
        status: Status value (or ``NO_MATCH_*``).
        matched_via: Provenance; fuzzy matches always get the caution
            style so a human confirms them.

    Returns:
        (fill, font) pair, or None for plain cells.
    """
    if status == "MATCH" and matched_via == "fuzzy_name":
        return _FILL_AMBER, _FONT_AMBER
    if status == "MATCH":
        return _FILL_GREEN, _FONT_GREEN
    if status.startswith("MISSING_DOB"):
        return _FILL_AMBER, _FONT_AMBER
    if status in {
        "DOB_MISMATCH",
        "AMBIGUOUS",
        "INVALID_DOB_2025",
        "INVALID_DOB_2026",
    }:
        return _FILL_RED, _FONT_RED
    return None


def _write_sheet(
    sheet: Worksheet,
    headers: list[str],
    records: list[list[object]],
    widths: dict[str, int | float],
    status_column: str = "status",
    via_column: str | None = None,
) -> None:
    """Write rows to a worksheet with styling, widths, filter, freeze.

    Args:
        sheet: Target worksheet.
        headers: Header row.
        records: Data rows in matching column order.
        widths: Column widths applied after writing.
        status_column: Name of the status column to colorize.
        via_column: Name of the ``matched_via`` column, when present,
            so fuzzy matches get the caution style over green.
    """
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = _FONT_BOLD
    status_index = headers.index(status_column) if status_column else None
    via_index = headers.index(via_column) if via_column else None
    for record in records:
        sheet.append(record)
        if status_index is None:
            continue
        matched_via = ""
        if via_index is not None:
            matched_via = str(record[via_index])
        style = _status_style(str(record[status_index]), matched_via)
        if style is None:
            continue
        fill, font = style
        cell = sheet.cell(row=sheet.max_row, column=status_index + 1)
        cell.fill = fill
        cell.font = font
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for _index, header in enumerate(sheet[1]):
        letter = header.column_letter
        if header.value is not None and header.value in widths:
            sheet.column_dimensions[letter].width = widths[header.value]


def _join_notes(notes: list[str]) -> str:
    """Render notes as a single, semicolon-separated string.

    Args:
        notes: Note fragments.

    Returns:
        Joined string.
    """
    return "; ".join(notes)


def build_children_2026_rows(
    children: list[Child2026],
) -> tuple[list[str], list[list[object]]]:
    """Build the ``children_2026`` sheet content.

    Args:
        children: All 2026 children after matching.

    Returns:
        (headers, rows) for the sheet writer.
    """
    headers = [
        "angel_id",
        "zite_record_id",
        "first_name",
        "family_id",
        "family_last_name",
        "family_first_name",
        "phone",
        "primary_email",
        "gift_pickup_location",
        "gender",
        "dob",
        "age_at_xmas",
        "matched_via",
        "counterpart_2025_source_sheet",
        "counterpart_2025_family_number",
        "counterpart_2025_child_tag",
        "counterpart_2025_first_name",
        "status",
        "notes",
    ]
    rows: list[list[object]] = []
    for child in sorted(
        children,
        key=lambda item: (
            item.family_id is None,
            item.family_id or 0,
            item.angel_id,
        ),
    ):
        counterpart = child.counterpart
        rows.append(
            [
                child.angel_id,
                child.zite_record_id,
                child.first_name,
                child.family_id,
                child.family_last_name,
                child.family_first_name,
                child.phone,
                child.email,
                child.gift_pickup_location,
                child.gender,
                _dob_display(child.dob_raw),
                child.age_at_xmas,
                child.matched_via,
                counterpart.source_sheet if counterpart else None,
                counterpart.family_number if counterpart else None,
                counterpart.child_tag if counterpart else None,
                counterpart.first_name if counterpart else None,
                child.match_status,
                _join_notes(child.notes),
            ]
        )
    return headers, rows


def build_children_2025_rows(
    children: list[Child2025],
) -> tuple[list[str], list[list[object]]]:
    """Build the ``children_2025`` sheet content.

    Args:
        children: All 2025 rows after matching.

    Returns:
        (headers, rows) for the sheet writer.
    """
    headers = [
        "source_sheet",
        "family_number",
        "child_tag",
        "reg_first_name",
        "reg_last_name",
        "reg_email",
        "reg_phone",
        "child_first_name",
        "child_last_name",
        "dob",
        "dob_raw",
        "age_2025",
        "matched_via",
        "counterpart_2026_family_id",
        "counterpart_2026_angel_id",
        "counterpart_2026_first_name",
        "status",
        "notes",
    ]
    rows: list[list[object]] = []
    for child in sorted(
        children,
        key=lambda item: (
            item.source_sheet,
            item.family_number is None,
            item.family_number or 0,
            item.child_tag,
            item.row_number,
        ),
    ):
        counterpart = child.counterpart
        rows.append(
            [
                child.source_sheet,
                child.family_number,
                child.child_tag,
                child.reg_first_name,
                child.reg_last_name,
                child.reg_email,
                child.reg_phone,
                child.first_name,
                child.last_name,
                _dob_display(child.dob_raw),
                child.dob_raw if isinstance(child.dob_raw, str) else "",
                child.age,
                child.matched_via,
                counterpart.family_id if counterpart else None,
                counterpart.angel_id if counterpart else None,
                counterpart.first_name if counterpart else None,
                child.match_status,
                _join_notes(child.notes),
            ]
        )
    return headers, rows


def build_comparison_rows(
    pairs: list[MatchedPair],
) -> tuple[list[str], list[list[object]]]:
    """Build the ``dob_comparison`` sheet content.

    Args:
        pairs: All matched pairs after status computation.

    Returns:
        (headers, rows) for the sheet writer.
    """
    headers = [
        "family_id_2026",
        "angel_id_2026",
        "source_sheet_2025",
        "family_number_2025",
        "child_tag_2025",
        "child_first_name_2025",
        "child_last_name_2025",
        "child_first_name_2026",
        "dob_2025",
        "dob_2026",
        "delta_days",
        "matched_via",
        "fuzzy_score",
        "status",
        "notes",
        "contact_email_2025",
        "contact_phone_2025",
        "contact_email_2026",
        "contact_phone_2026",
        "gift_pickup_location_2026",
    ]
    rows: list[list[object]] = []
    for pair in sorted(
        pairs,
        key=lambda p: (
            p.child_2026.family_id is None,
            p.child_2026.family_id or 0,
            p.child_2026.angel_id,
        ),
    ):
        rep = pair.child_2025
        cand = pair.child_2026
        status = rep.match_status
        notes = list(rep.notes)
        if pair.matched_via == "fuzzy_name":
            notes.append("fuzzy name match: verify")
        rows.append(
            [
                cand.family_id,
                cand.angel_id,
                rep.source_sheet,
                rep.family_number,
                rep.child_tag,
                rep.first_name,
                rep.last_name,
                cand.first_name,
                _dob_display(rep.dob_raw),
                _dob_display(cand.dob_raw),
                _delta_days(rep.dob, cand.dob),
                pair.matched_via,
                pair.score if pair.score is not None else "",
                status,
                _join_notes(notes),
                rep.reg_email,
                rep.reg_phone,
                cand.email,
                cand.phone,
                cand.gift_pickup_location,
            ]
        )
    return headers, rows


_WIDTHS: dict[str, int | float] = {
    "angel_id": 10,
    "zite_record_id": 24,
    "child_tag": 10,
    "source_sheet": 14,
    "source_sheet_2025": 14,
    "counterpart_2025_source_sheet": 22,
    "first_name": 16,
    "last_name": 18,
    "family_last_name": 18,
    "reg_last_name": 18,
    "reg_first_name": 16,
    "child_first_name": 18,
    "child_last_name": 18,
    "child_first_name_2025": 18,
    "child_first_name_2026": 18,
    "primary_email": 30,
    "reg_email": 28,
    "contact_email_2025": 28,
    "contact_email_2026": 30,
    "reg_phone": 15,
    "contact_phone_2025": 15,
    "contact_phone_2026": 15,
    "phone": 15,
    "dob": 12,
    "dob_raw": 14,
    "dob_2025": 12,
    "dob_2026": 12,
    "delta_days": 10,
    "matched_via": 12,
    "fuzzy_score": 11,
    "status": 16,
    "notes": 34,
    "gift_pickup_location": 20,
    "family_first_name": 16,
    "counterpart_2025_first_name": 18,
    "counterpart_2026_first_name": 18,
    "counterpart_2026_angel_id": 16,
    "counterpart_2025_child_tag": 20,
    "counterpart_2025_family_number": 20,
    "counterpart_2026_family_id": 22,
    "age_2025": 10,
    "family_number": 14,
    "family_id": 10,
    "family_id_2026": 14,
    "family_number_2025": 18,
    "child_tag_2025": 12,
    "angel_id_2026": 10,
    "gift_pickup_location_2026": 22,
}


def build_workbook(
    children_2025: list[Child2025],
    children_2026: list[Child2026],
    pairs: list[MatchedPair],
) -> Workbook:
    """Assemble the three-sheet output workbook.

    Args:
        children_2025: Finalized 2025 rows.
        children_2026: Finalized 2026 children.
        pairs: Matched pairs.

    Returns:
        Populated Workbook ready to save.
    """
    workbook = Workbook()
    default = workbook.active
    if default is not None and default.title == "Sheet":
        workbook.remove(default)

    child26_headers, child26_rows = build_children_2026_rows(children_2026)
    _write_sheet(
        workbook.create_sheet("children_2026"),
        child26_headers,
        child26_rows,
        _WIDTHS,
        via_column="matched_via",
    )
    child25_headers, child25_rows = build_children_2025_rows(children_2025)
    _write_sheet(
        workbook.create_sheet("children_2025"),
        child25_headers,
        child25_rows,
        _WIDTHS,
        via_column="matched_via",
    )
    comparison_headers, comparison_rows = build_comparison_rows(pairs)
    _write_sheet(
        workbook.create_sheet("dob_comparison"),
        comparison_headers,
        comparison_rows,
        _WIDTHS,
        via_column="matched_via",
    )
    return workbook


def save_workbook(workbook: Workbook, path: Path) -> None:
    """Save a workbook atomically, with a friendly lock error.

    Args:
        workbook: Workbook instance.
        path: Destination path; replaced via ``os.replace``.

    Raises:
        RuntimeError: When the destination is held open (e.g. by
            LibreOffice), telling the user to close it and rerun.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp"
    try:
        workbook.save(tmp_path)
        os.replace(tmp_path, path)
    except PermissionError as exc:
        if tmp_path.exists():
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise RuntimeError(
            f"cannot write {path}: close the file in any open "
            f"spreadsheet application and rerun ({exc})"
        ) from exc


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def run(file_2025: Path, file_2026: Path, output: Path) -> dict[str, int]:
    """Run the full compare pipeline and write the output workbook.

    Args:
        file_2025: 2025 workbook path.
        file_2026: 2026 workbook path.
        output: Output workbook path.

    Returns:
        Summary counters for the printed report.
    """
    children_2025 = load_children_2025(file_2025)
    children_2026 = load_children_2026(file_2026)
    pairs = match_children(children_2025, children_2026)
    finalize_statuses(children_2025, children_2026)
    workbook = build_workbook(children_2025, children_2026, pairs)
    save_workbook(workbook, output)

    status_counts: Counter[str] = Counter(
        pair.child_2025.match_status for pair in pairs
    )
    summary: dict[str, int] = {
        "children_2025": len(children_2025),
        "children_2026": len(children_2026),
        "matched_pairs": len(pairs),
        "unmatched_2025": sum(
            1
            for child in children_2025
            if child.representative and child.counterpart is None
        ),
        "unmatched_2026": sum(
            1 for child in children_2026 if child.counterpart is None
        ),
    }
    for sheet in SHEETS_2025:
        summary[f"2025_rows_{sheet}"] = sum(
            1 for child in children_2025 if child.source_sheet == sheet
        )
    summary.update(status_counts)
    return summary


def main() -> int:
    """Run the CLI.

    Returns:
        Process exit code: 0 on success, 1 on error.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Compare child DOBs between the 2025 CCAM/TSA Reduced "
            "sheets and the 2026 final_child sheet; writes a "
            "three-sheet comparison workbook where every match "
            "carries a provenance and a status column flags DOB "
            "inconsistencies."
        )
    )
    parser.add_argument(
        "--2025",
        dest="file_2025",
        type=Path,
        default=DEFAULT_FILE_2025,
        help=f"2025 workbook (default: {DEFAULT_FILE_2025})",
    )
    parser.add_argument(
        "--2026",
        dest="file_2026",
        type=Path,
        default=DEFAULT_FILE_2026,
        help=f"2026 workbook (default: {DEFAULT_FILE_2026})",
    )
    parser.add_argument(
        "--output",
        dest="output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output workbook (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()
    try:
        summary = run(args.file_2025, args.file_2026, args.output)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {args.output}")
    print(f"  2025 children: {summary['children_2025']}")
    for sheet in SHEETS_2025:
        key = f"2025_rows_{sheet}"
        if key in summary:
            print(f"    {sheet}: {summary[key]}")
    print(f"  2026 children: {summary['children_2026']}")
    print(f"  matched pairs: {summary['matched_pairs']}")
    print(
        f"    (unmatched: 2025={summary['unmatched_2025']}, 2026={summary['unmatched_2026']})"
    )
    excluded = {
        "children_2025",
        "children_2026",
        "matched_pairs",
        "unmatched_2025",
        "unmatched_2026",
    }
    status_lines = [
        f"    {status}: {count}"
        for status, count in sorted(
            (key, value)
            for key, value in summary.items()
            if key not in excluded and not key.startswith("2025_rows_")
        )
    ]
    for line in status_lines:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
