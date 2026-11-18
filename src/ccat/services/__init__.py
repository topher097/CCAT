"""Phase 1b application services (refactor §5.1, §9.3).

Typed, workbook-backed use-case functions shared by the future API and
job layers. The legacy CLI entry points and NiceGUI apps are unchanged
and remain the active callers of the legacy modules; these services
wrap the same behavior behind stable signatures so the Phase 2 DB
repositories can slot in behind :class:`ccat.services.ports.SeasonStore`.

Modules:

- ``results`` — pydantic result DTOs.
- ``audit`` — tri-state save, ignore/unignore actions, reconciliation.
- ``special_needs`` — special-needs parse/serialize (ex-NiceGUI).
- ``pipeline`` — acquire/screen/match/generate/stats/clarify wrappers.
- ``ports`` — ``SeasonStore`` storage protocol (documentation only).
"""

from ccat.services.audit import (
    SavePatch,
    entity_count_changes,
    ignore_entity,
    reconcile,
    save_entity,
    status_of_row,
    unignore_entity,
)
from ccat.services.pipeline import (
    acquire,
    clarify_export,
    clarify_generate,
    generate,
    match,
    screen,
    stats,
)
from ccat.services.ports import SeasonStore
from ccat.services.results import (
    AcquireResult,
    ClarifyExportResult,
    ClarifyGenerateResult,
    EntityCounts,
    GenerateResult,
    MatchResult,
    ReconcileResult,
    SaveResult,
    ScreenResult,
    StatsResult,
)
from ccat.services.special_needs import (
    SPECIAL_NEEDS_OPTIONS,
    SpecialNeeds,
    parse_special_needs,
    serialize_special_needs,
)

__all__ = [
    "AcquireResult",
    "ClarifyExportResult",
    "ClarifyGenerateResult",
    "EntityCounts",
    "GenerateResult",
    "MatchResult",
    "ReconcileResult",
    "SPECIAL_NEEDS_OPTIONS",
    "SavePatch",
    "SaveResult",
    "SeasonStore",
    "ScreenResult",
    "SpecialNeeds",
    "StatsResult",
    "acquire",
    "clarify_export",
    "clarify_generate",
    "entity_count_changes",
    "generate",
    "ignore_entity",
    "match",
    "parse_special_needs",
    "reconcile",
    "save_entity",
    "screen",
    "serialize_special_needs",
    "stats",
    "status_of_row",
    "unignore_entity",
]
