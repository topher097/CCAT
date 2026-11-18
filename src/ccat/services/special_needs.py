"""Special-needs text serialization (moved from the NiceGUI callback).

Child ``special_needs`` is stored as one semicolon-joined text value
(``"ADHD; other: speech delay"``); an empty selection means none. The
fixed checkbox vocabulary and the exact parse/serialize format are the
legacy behavior (§8.6, D4) and must stay byte-identical — the audit UI
and any future API writer share these functions as the single owner.
"""

from dataclasses import dataclass, field

#: Fixed special-needs vocabulary; "other" carries an optional custom
#: value and always sorts last when serialized.
SPECIAL_NEEDS_OPTIONS: tuple[str, ...] = (
    "ADHD",
    "Autism",
    "Down Syndrome",
    "other",
)


@dataclass
class SpecialNeeds:
    """One child's parsed special-needs selection.

    Attributes:
        fixed: The selected fixed pieces (subset of
            :data:`SPECIAL_NEEDS_OPTIONS`, plus any legacy free-text
            piece that did not start with "other").
        other: The custom text behind the "other" piece ("" when the
            bare "other" checkbox alone is selected).
    """

    fixed: set[str] = field(default_factory=set)
    other: str = ""


def parse_special_needs(text: str | None) -> SpecialNeeds:
    """Parse one stored ``special_needs`` value.

    Args:
        text: The stored semicolon-joined value ("" or None when the
            child has none).

    Returns:
        The selection: fixed pieces plus the "other" custom text. Any
        piece starting with "other" (case-insensitive) feeds the
        custom text; its remainder after "other" is stripped of
        leading colons/spaces, matching the legacy UI parser exactly.
    """
    parts = [part.strip() for part in (text or "").split(";") if part.strip()]
    fixed: list[str] = []
    other_text = ""
    for part in parts:
        if part.lower().startswith("other"):
            other_text = part[5:].strip(": ")
        else:
            fixed.append(part)
    return SpecialNeeds(fixed=set(fixed), other=other_text)


def serialize_special_needs(value: SpecialNeeds) -> str | None:
    """Serialize one selection back to its stored form.

    Args:
        value: The parsed selection.

    Returns:
        The semicolon-joined text with fixed pieces sorted and
        ``"other: {custom}"`` (or bare ``"other"`` when the custom text
        is blank) always last; None when nothing is selected.
    """
    pieces = sorted(value.fixed)
    if "other" in pieces:
        pieces = [piece for piece in pieces if piece != "other"]
        custom = str(value.other).strip()
        pieces.append(f"other: {custom}" if custom else "other")
    return "; ".join(pieces) if pieces else None


__all__ = [
    "SPECIAL_NEEDS_OPTIONS",
    "SpecialNeeds",
    "parse_special_needs",
    "serialize_special_needs",
]
