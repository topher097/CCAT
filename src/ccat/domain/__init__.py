"""Neutral shared-primitives module for the pipeline.

Pure age, text, ID-assignment, and duplicate/DOB-conflict primitives
shared across the pipeline stages (acquisition, preparation, screening,
audit store, matching).  This module is deliberately dependency-neutral:
stdlib only -- no polars, no workbook I/O, no config, no other ccat
modules -- so every stage can depend on it without creating import
cycles.  Values stay plain dicts and scalars; the polars-typed DOB log
schema/frame remain in :mod:`ccat.acquire`, which re-exports everything
here for backward compatibility.
"""

import json
import logging
import re
from datetime import UTC, date, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _ColumnSpecLike(Protocol):
    """Structural stand-in for a config column spec.

    Only ``type`` is read; importing the real spec would drag config
    into this stdlib-only module.
    """

    type: str


class _ScreeningSettingsLike(Protocol):
    """Structural stand-in for the config screening settings."""

    @property
    def sibling_dob_max_gap_days(self) -> int:
        """Maximum same-family DOB gap in days."""
        ...


class _ConfigLike(Protocol):
    """Structural stand-in for the pipeline config."""

    @property
    def xmas(self) -> date:
        """The configured Christmas date."""
        ...

    @property
    def screening(self) -> _ScreeningSettingsLike:
        """The screening settings."""
        ...


#: Exact mapping from the `age_preference` vocabulary to age bounds (§5.5).
AGE_PREF_BOUNDS: dict[str, tuple[int, int]] = {
    "Any Age": (0, 18),
    "Newborn through Toddler (0-3 yrs)": (0, 3),
    "Pre-K through Grade 5 (4-10 yrs)": (4, 10),
    "Middle School (11-13 yrs)": (11, 13),
    "High School (14-18+ yrs)": (14, 18),
    "Special Needs (any age)": (0, 18),
}

#: The age-preference label that marks a special-needs sponsor (§5.5, §8.2).
SPECIAL_NEEDS_LABEL = "Special Needs (any age)"

#: RFC 5322 subset used for email validation (§6.2, applied by screening).
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)

#: Derived columns added by in-memory preparation for each entity table.
DERIVED_COLUMNS = {
    "sponsor": ["sponsor_id", "pref_age_min", "pref_age_max"],
    "family": ["family_id"],
    "child": ["family_id", "angel_id", "age_at_xmas"],
}


def snake_case(name: str) -> str:
    """Normalize a column name to lower snake_case (§3).

    Args:
        name: The source column name.

    Returns:
        The lower snake_case form.
    """
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()


def clean_value(value: Any, spec: _ColumnSpecLike) -> tuple[Any, str | None]:
    """Clean one cell value per its declared type (§6.2, applied by screen).

    Local, trivial normalization only: names title-cased, emails
    lowercased, US phones reformatted, dates ISO.  These fixes never go
    near the AI screening prompt.

    Args:
        value: Raw value (may be None).
        spec: The column spec.

    Returns:
        A tuple of (cleaned value, failure reason or None).
    """
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None, None
    if spec.type == "string":
        if isinstance(value, dict):
            return json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ), None
        return str(value).strip(), None
    if spec.type == "name":
        return str(value).strip().title(), None
    if spec.type == "email":
        cleaned = str(value).strip().lower()
        if EMAIL_RE.fullmatch(cleaned):
            return cleaned, None
        return value, "invalid email"
    if spec.type == "phone_us":
        digits = re.sub(r"\D", "", str(value))
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}", None
        return value, "not a 10-digit US phone number"
    if spec.type in {"int", "float"}:
        try:
            return (int if spec.type == "int" else float)(
                str(value).strip()
            ), None
        except ValueError:
            return value, f"not a valid {spec.type}"
    if spec.type == "bool":
        if isinstance(value, bool):
            return value, None
        text = str(value).strip().lower()
        if text in {"true", "yes", "1"}:
            return True, None
        if text in {"false", "no", "0"}:
            return False, None
        return value, "not a boolean"
    if spec.type == "date":
        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat(), None
            except ValueError:
                continue
        return value, "not MM/DD/YYYY or ISO date"
    if spec.type == "datetime":
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return value, "not ISO 8601"
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC)
        return parsed.isoformat(), None
    if spec.type == "enum":
        return value, None
    return value, None


def _parse_reported_age(value: Any) -> int | None:
    """Coerce the free-text reported age to an integer (§6.2).

    Families type values like ``4 turning 5`` or ``2.5`` into the
    optional cross-check field; the computed ``age_at_xmas`` is the
    authoritative age, so unparseable values degrade to null (logged)
    instead of rejecting the whole child row.

    Args:
        value: The raw age_reported value.

    Returns:
        The coerced integer, or None when no number is present.
    """
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        pass
    match = re.match(r"\s*(\d+)", text)
    if match is not None:
        coerced = int(match.group(1))
        logger.info("age_reported %r coerced to %d", text, coerced)
        return coerced
    logger.info("age_reported %r is not numeric; using null", text)
    return None


def compute_age_at_xmas(dob: str, xmas: date) -> int:
    """Compute full years between dob and the configured Christmas date.

    Args:
        dob: ISO date-of-birth string.
        xmas: The configured Christmas date.

    Returns:
        The age in completed years.
    """
    born = date.fromisoformat(dob)
    return (
        xmas.year
        - born.year
        - ((xmas.month, xmas.day) < (born.month, born.day))
    )


def index_to_letters(index: int) -> str:
    """Map a zero-based sibling index to an angel letter suffix (§6.5).

    A=0 ... Z=25, AA=26, AB=27, ... (letters exceed Z bijectively).

    Args:
        index: Zero-based position within the family.

    Returns:
        The uppercase letter code.
    """
    index += 1
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _created_value(row: dict[str, Any]) -> Any:
    """Return the row's created timestamp under either config key.

    The sponsor table's created column may be keyed ``created`` or
    ``create`` depending on the config; family/child use ``created``.

    Args:
        row: One imported row.

    Returns:
        The created value (possibly None).
    """
    return (
        row.get("created")
        if row.get("created") is not None
        else row.get("create")
    )


def assign_sponsor_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign sequential sponsor_id by created then email (§6.5).

    Every sponsor submission keeps its own ``sponsor_id``; duplicate
    sponsors (same email) each get a row and are flagged by screening's
    duplicate detection, never merged.

    Args:
        rows: Cleaned sponsor rows.

    Returns:
        Rows with sponsor_id, pref_age_min, and pref_age_max added.
    """
    ordered = sorted(
        rows,
        key=lambda row: (_created_value(row) or "", row.get("email") or ""),
    )
    result: list[dict[str, Any]] = []
    for sponsor_id, row in enumerate(ordered, start=1):
        row = dict(row)
        row["sponsor_id"] = sponsor_id
        low, high = AGE_PREF_BOUNDS[row["age_preference"]]
        row["pref_age_min"] = low
        row["pref_age_max"] = high
        result.append(row)
    return result


def assign_family_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign one family_id per primary_email group (§6.5).

    Duplicate submissions (same ``primary_email``) share the family's
    ``family_id``; nothing is merged.  Groups are ordered by their
    earliest ``created`` then email; email-less rows are singleton
    groups so every family row carries an id.

    Args:
        rows: Cleaned family rows.

    Returns:
        Rows with family_id added.
    """
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row.get("primary_email"), []).append(row)
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (
            min(_created_value(row) or "" for row in item[1]),
            item[0] or "",
        ),
    )
    result: list[dict[str, Any]] = []
    for family_id, (_, group) in enumerate(ordered_groups, start=1):
        for row in group:
            row = dict(row)
            row["family_id"] = family_id
            result.append(row)
    return result


def assign_angel_ids(
    child_rows: list[dict[str, Any]],
    cfg: _ConfigLike,
    family_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Link children to families and assign angel_id letters (§6.5).

    Children link via primary_email to the family rows'
    ``family_id`` (``family_rows`` when given, else the child rows
    themselves).  Within a family, letters start at A for the oldest
    child (descending age_at_xmas, tie-break earliest created then dob).
    Duplicate submissions of the same child (same ``primary_email`` and
    ``first_name``) never get a letter of their own: only the group's
    most recent submission keeps an ``angel_id``, the others carry
    ``family_id`` with ``angel_id=None`` and are flagged as potential
    duplicates for the user to resolve.  Orphaned children keep null
    family_id and angel_id.

    Args:
        child_rows: Cleaned child rows.
        cfg: Pipeline configuration.
        family_rows: Prepared family rows carrying ``family_id``; when
            None, the child rows themselves must already carry it.

    Returns:
        Child rows with family_id, angel_id, and age_at_xmas added.
    """
    xmas = cfg.xmas
    email_to_family: dict[str, int] = {}
    for row in (*(family_rows or []), *child_rows):
        email = row.get("primary_email")
        family_id = row.get("family_id")
        if email is not None and family_id is not None:
            email_to_family.setdefault(email, family_id)
    enriched: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []
    families: dict[int, list[dict[str, Any]]] = {}
    for row in child_rows:
        row = dict(row)
        try:
            row["age_at_xmas"] = compute_age_at_xmas(row["dob"], xmas)
        except (TypeError, ValueError):
            row["age_at_xmas"] = None  # unparsable dob; flagged downstream
        reported = row.get("age_reported")
        if reported is not None and reported != row["age_at_xmas"]:
            logger.warning(
                "age mismatch for child %s: age_reported=%s, computed age_at_xmas=%s",
                row["zite_record_id"],
                reported,
                row["age_at_xmas"],
            )
        family_id = email_to_family.get(row.get("primary_email"))
        if family_id is None:
            row["family_id"] = None
            row["angel_id"] = None
            orphans.append(row)
            logger.warning(
                "orphaned child %s: no family registration for %r",
                row["zite_record_id"],
                row.get("primary_email"),
            )
            continue
        row["family_id"] = family_id
        families.setdefault(family_id, []).append(row)
    for family_id in sorted(families):
        # Survivor per duplicate-child group keeps the letter; extras
        # (earlier submissions of the same child) stay letterless.
        survivors: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        for row in sorted(
            families[family_id],
            key=lambda item: (
                item.get("created") or "",
                item.get("zite_record_id") or "",
            ),
            reverse=True,
        ):
            email = row.get("primary_email")
            name = row.get("first_name")
            row["angel_id"] = None
            if email is None or name is None:
                survivors.append(row)  # unkeyed child: never merged
                continue
            key = (str(email), str(name))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            survivors.append(row)
        ordered = sorted(
            survivors,
            key=lambda row: (
                -(row["age_at_xmas"] or 0),
                row.get("created") or "",
                row.get("dob") or "",
            ),
        )
        for position, row in enumerate(ordered):
            row["angel_id"] = f"{family_id}{index_to_letters(position)}"
        enriched.extend(
            sorted(
                families[family_id],
                key=lambda item: (
                    item.get("created") or "",
                    item.get("zite_record_id") or "",
                ),
            )
        )
    enriched.extend(orphans)
    return enriched


def _cross_check_created(rows: list[dict[str, Any]], table_name: str) -> None:
    """Log divergence between user created and zite_created_at (§5.1)."""
    for row in rows:
        created = _created_value(row)
        zite_created = row.get("zite_created_at")
        if created and zite_created and created != zite_created:
            logger.info(
                "%s %s: created %s != zite_created_at %s",
                table_name,
                row.get("zite_record_id"),
                created,
                zite_created,
            )


# ---------------------------------------------------------------------------
# Sibling DOB conflicts (§6.6, written by the screening stage).
# ---------------------------------------------------------------------------


def _dob_gap_months(earlier: date, later: date) -> int:
    """Count completed calendar months between two dates (§6.6).

    Reporting only: the conflict threshold is day-based, from
    ``cfg.screening.sibling_dob_max_gap_days``.

    Args:
        earlier: The earlier date.
        later: The later date.

    Returns:
        Whole months between the dates; two equal dates yield 0.
    """
    months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    if later.day < earlier.day:
        months -= 1
    return max(months, 0)


def sibling_dob_conflicts(
    rows: list[dict[str, Any]], cfg: _ConfigLike, report: bool = True
) -> list[dict[str, Any]]:
    """Find same-family children born too close together (§6.6).

    Every unordered pair of children sharing a ``family_id`` whose birth
    dates are at most ``cfg.screening.sibling_dob_max_gap_days`` days
    apart (twins included) is recorded for human review -- each pair is
    compared and logged exactly once (``child_a`` is the earlier birth,
    so no swapped duplicate rows are ever written).  Duplicate
    submissions of the same child (same ``primary_email`` and
    ``first_name``) never form a pair, so a resubmission's identical DOB
    is not a false conflict.  Children without a ``family_id`` (orphans)
    or without a ``dob`` are skipped.

    Args:
        rows: Cleaned, ID-assigned child rows.
        cfg: Pipeline configuration (threshold in days).
        report: Emit one warning per conflicting pair when True; call sites
            that only need the log rows (such as audit startup) pass False.

    Returns:
        One log row per conflicting pair, ordered deterministically
        (family, then birth date, then record id).
    """
    max_gap_days = cfg.screening.sibling_dob_max_gap_days
    by_family: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("family_id") is None or row.get("dob") is None:
            continue
        by_family.setdefault(row["family_id"], []).append(row)
    seen: set[frozenset[Any]] = set()
    log_rows: list[dict[str, Any]] = []
    for family_id in sorted(by_family, key=lambda value: str(value)):
        ordered = sorted(
            by_family[family_id],
            key=lambda row: (row["dob"], row.get("zite_record_id") or ""),
        )
        for index, child_a in enumerate(ordered):
            for child_b in ordered[index + 1 :]:
                if _child_identity(child_a) == _child_identity(child_b):
                    continue  # duplicated submission, not two children
                dob_a = date.fromisoformat(str(child_a["dob"]))
                dob_b = date.fromisoformat(str(child_b["dob"]))
                gap_days = abs((dob_b - dob_a).days)
                if gap_days > max_gap_days:
                    continue
                # Unordered-pair key: a duplicate submission (the same
                # pair reaching the loop twice) never logs twice.
                pair = frozenset(
                    (
                        child_a.get("zite_record_id"),
                        child_b.get("zite_record_id"),
                    )
                )
                if pair in seen:
                    continue
                seen.add(pair)
                log_rows.append(
                    {
                        "family_id": family_id,
                        "primary_email": child_a.get("primary_email"),
                        "child_a_zite_record_id": child_a.get("zite_record_id"),
                        "child_a_angel_id": child_a.get("angel_id"),
                        "child_a_first_name": child_a.get("first_name"),
                        "child_a_dob": child_a.get("dob"),
                        "child_b_zite_record_id": child_b.get("zite_record_id"),
                        "child_b_angel_id": child_b.get("angel_id"),
                        "child_b_first_name": child_b.get("first_name"),
                        "child_b_dob": child_b.get("dob"),
                        "gap_months": _dob_gap_months(dob_a, dob_b),
                        "gap_days": gap_days,
                    }
                )
                if report:
                    logger.warning(
                        "sibling dob conflict for family %s: %s (%s, dob %s)"
                        " and %s (%s, dob %s) are %d day(s) apart",
                        family_id,
                        child_a.get("first_name"),
                        child_a.get("angel_id"),
                        child_a.get("dob"),
                        child_b.get("first_name"),
                        child_b.get("angel_id"),
                        child_b.get("dob"),
                        gap_days,
                    )
    return log_rows


def _child_identity(row: dict[str, Any]) -> tuple[str, str]:
    """Duplicate-group identity of one child row (§6.5)."""
    return (str(row.get("primary_email")), str(row.get("first_name")))
