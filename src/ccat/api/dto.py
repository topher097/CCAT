"""Pydantic request/response models for the API (refactor §5.2).

Canonical names rule (§1.2 rule 10, D-rename): the legacy workbook
column ``potential_anomoly`` is exposed as ``ai_screening_notes`` and
sponsor ``create`` as ``created`` — every DTO uses the canonical
spelling; the legacy names exist only inside the store/repository
boundary (:data:`ccat.db.repositories.LEGACY_COLUMN_RENAMES`).  All
mutation DTOs forbid unknown fields.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from ccat.db.repositories import LEGACY_COLUMN_RENAMES
from ccat.services.audit import SavePatch

#: legacy name -> canonical DTO name, one mapping per entity.
CANONICAL_NAMES: dict[str, dict[str, str]] = {
    entity: dict(renames) for entity, renames in LEGACY_COLUMN_RENAMES.items()
}

#: canonical DTO name -> legacy store name (patch payload mapping).
_LEGACY_NAMES: dict[str, str] = {
    canonical: legacy
    for renames in LEGACY_COLUMN_RENAMES.values()
    for legacy, canonical in renames.items()
}


def canonicalize(entity: str, values: dict[str, Any] | None) -> dict[str, Any]:
    """Rename one row dict's legacy keys to canonical DTO names.

    Args:
        entity: "child", "family", or "sponsor".
        values: Row values under legacy dict names.

    Returns:
        A shallow copy with canonical keys.
    """
    if values is None:
        return {}
    renames = CANONICAL_NAMES.get(entity, {})
    return {renames.get(key, key): value for key, value in values.items()}


def decanonicalize_patch(values: dict[str, Any]) -> dict[str, Any]:
    """Map canonical patch keys back to the store's legacy names.

    Args:
        values: Supplied patch fields under canonical names.

    Returns:
        The same fields under legacy names where they differ.
    """
    return {_LEGACY_NAMES.get(key, key): value for key, value in values.items()}


def _entity_patch_model() -> type[BaseModel]:
    """Build :class:`EntityPatch` from :class:`SavePatch` (one source).

    The tri-state patch fields are SavePatch's, with the child AI
    column exposed under its canonical name, plus the ``status``
    control the legacy save accepts (``ignore`` stays a separate
    confirmed action); everything else is identical, so a new editable
    field automatically appears here too.

    Returns:
        The generated pydantic model class.
    """
    fields: dict[str, Any] = {}
    for name, info in SavePatch.model_fields.items():
        canonical = CANONICAL_NAMES["child"].get(name, name)
        fields[canonical] = (info.annotation, info)
    fields["status"] = (str | None, Field(default=None))
    return create_model(
        "EntityPatch",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


#: The patch model the audit routes accept.  It is built at runtime, so a
#: type checker cannot treat the name as a class.
EntityPatch = _entity_patch_model()


class StrictModel(BaseModel):
    """Base response model (responses tolerate extra; requests don't)."""

    model_config = ConfigDict(extra="allow")


class UserView(StrictModel):
    """One staff account (never secrets/tokens)."""

    id: int
    username: str
    display_name: str | None = None
    active: bool
    roles: list[str] = Field(default_factory=list)
    dev_no_password: bool = False


class UserCreate(StrictModel):
    """Create one staff account (admin only)."""

    model_config = ConfigDict(extra="forbid")

    username: str
    display_name: str | None = None
    roles: list[Literal["read_only", "auditor", "coordinator", "admin"]] = (
        Field(default_factory=list)
    )
    active: bool = True


class UserPatch(StrictModel):
    """Patch one staff account (admin only; omitted = unchanged)."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    active: bool | None = None
    roles: (
        list[Literal["read_only", "auditor", "coordinator", "admin"]] | None
    ) = None


class MeView(StrictModel):
    """The authenticated identity plus its CSRF token (§8)."""

    username: str
    display_name: str | None = None
    roles: list[str] = Field(default_factory=list)
    csrf_token: str


class SeasonView(StrictModel):
    """One season's identity (read-only; D4 config stays in JSON)."""

    id: int | None = None
    name: str
    xmas_date: str


class EntityListPage(StrictModel):
    """One paginated entity list page (§5.2)."""

    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


class EntityDetail(StrictModel):
    """One entity's full audit detail (§5.2).

    Attributes:
        row: The effective stored row (canonical names) plus
            ``row_version`` / ``last_updated_by`` / ``last_updated_at``.
        baseline: The prepared baseline values for diffing.
        submissions: The entity's raw duplicate-group submissions.
        status: The stored status.
        row_version: Optimistic-concurrency version (If-Match value).
        last_updated_by: Latest editor.
        last_updated_at: Latest edit timestamp (ISO).
        audited_at: The human-audit authority timestamp.
        diagnostics: Per-entity diagnostics (child: DOB conflicts).
    """

    entity: str
    key: str
    row: dict[str, Any]
    baseline: dict[str, Any]
    submissions: list[dict[str, Any]] = Field(default_factory=list)
    status: str | None = None
    row_version: int
    last_updated_by: str | None = None
    last_updated_at: str | None = None
    audited_at: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class IgnoreRequest(StrictModel):
    """Confirmed ignore/unignore action (§9.3)."""

    model_config = ConfigDict(extra="forbid")

    confirm: bool
    expected_version: int


class JobView(StrictModel):
    """One durable job's observation view (§6.1)."""

    id: int
    job_type: str
    season_id: int | None = None
    status: str
    requester: str
    options: dict[str, Any] = Field(default_factory=dict)
    progress: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    cancel_requested: bool = False
    attempt: int = 0
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)


class JobCreate(StrictModel):
    """Enqueue one durable job (§6.1)."""

    model_config = ConfigDict(extra="forbid")

    job_type: Literal[
        "acquire",
        "screen",
        "match",
        "generate",
        "stats",
        "clarify_generate",
        "clarify_export",
        "reconcile",
    ]
    options: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class JobEventView(StrictModel):
    """One job event (polling reads, §5.2)."""

    id: int
    job_id: int
    ts: str | None = None
    kind: str | None = None
    data: dict[str, Any] | None = None


class ArtifactView(StrictModel):
    """One registered artifact's metadata (§6.3)."""

    id: int
    kind: str | None = None
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    meta: dict[str, Any] | None = None
    created_by: str | None = None
    created_at: str | None = None


class ClarifyDraftView(StrictModel):
    """One versioned clarification draft (D5)."""

    id: int
    family_id: int | None = None
    version: int
    subject: str | None = None
    greeting: str | None = None
    intro: str | None = None
    closing: str | None = None
    paragraphs: list[dict[str, Any]] | None = None
    legacy_positional: bool = False
    generated_by: str | None = None
    generated_at: str | None = None
    edited_by: str | None = None
    edited_at: str | None = None


class DraftPatch(StrictModel):
    """Tri-state draft edit (omitted = unchanged, null = clear)."""

    model_config = ConfigDict(extra="forbid")

    subject: str | None = None
    greeting: str | None = None
    intro: str | None = None
    closing: str | None = None
    paragraphs: list[dict[str, Any]] | None = None


class ContactRequest(StrictModel):
    """Contact-flag action body; ``null`` is rejected (422)."""

    model_config = ConfigDict(extra="forbid")

    contacted: bool | None = None
    replied: bool | None = None


class ClarifyFamilyView(StrictModel):
    """One family's clarification view (draft + status + children)."""

    family_id: int
    contacted: bool = False
    contacted_at: str | None = None
    replied: bool = False
    updated_by: str | None = None
    updated_at: str | None = None
    draft: ClarifyDraftView | None = None
    children: list[dict[str, Any]] = Field(default_factory=list)


class Overview(StrictModel):
    """Server-computed season overview (§7.1.2, no client aggregation)."""

    season: str
    entities: dict[str, dict[str, Any]] = Field(default_factory=dict)
    diagnostics: dict[str, int] = Field(default_factory=dict)
    artifacts: dict[str, int] = Field(default_factory=dict)
    last_jobs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    data_revision: str | None = None


class HealthView(StrictModel):
    """Liveness/readiness body."""

    status: str


class DbTableView(StrictModel):
    """One database table with its columns and row count."""

    name: str
    columns: list[str] = Field(default_factory=list)
    row_count: int = 0


class DbQueryRequest(StrictModel):
    """One SQL statement to run (admin only).

    ``write`` must be true for anything that changes the database; the
    server refuses a modifying statement without it.
    """

    model_config = ConfigDict(extra="forbid")

    sql: str
    write: bool = False


class DbQueryResult(StrictModel):
    """The outcome of one SQL statement.

    Attributes:
        columns: Result column names (empty for a statement with no
            result set).
        rows: The returned rows.
        row_count: Number of rows returned.
        truncated: Whether more rows existed than were returned.
        affected_rows: Rows changed by a modifying statement.
        duration_ms: Server-side execution time.
    """

    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    affected_rows: int = 0
    duration_ms: float = 0.0


__all__ = [
    "ArtifactView",
    "ClarifyDraftView",
    "ClarifyFamilyView",
    "ContactRequest",
    "DbQueryRequest",
    "DbQueryResult",
    "DbTableView",
    "DraftPatch",
    "EntityDetail",
    "EntityListPage",
    "EntityPatch",
    "HealthView",
    "IgnoreRequest",
    "JobCreate",
    "JobEventView",
    "JobView",
    "MeView",
    "Overview",
    "SeasonView",
    "UserCreate",
    "UserPatch",
    "UserView",
    "canonicalize",
    "decanonicalize_patch",
]
