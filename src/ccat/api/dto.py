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


class NeighbourView(StrictModel):
    """One neighbouring entry in the audit list.

    Attributes:
        key: The entry's key.
        label: A human label for the entry.
    """

    key: str
    label: str = ""


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
        previous: The entry before this one in the list's order.
        next: The entry after this one in the list's order.
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
    previous: NeighbourView | None = None
    next: NeighbourView | None = None


class IgnoreRequest(StrictModel):
    """Confirmed ignore/unignore action (§9.3)."""

    model_config = ConfigDict(extra="forbid")

    confirm: bool
    expected_version: int


class DuplicationListItem(StrictModel):
    """One detected duplicate group in the Duplications list.

    Attributes:
        id: The group's stable, URL-safe id.
        sheet: The duplicate-log sheet label.
        entity: "child", "family", or "sponsor".
        group_key: The duplicate group's key.
        member_count: Submissions in the group.
        decided_count: Members the audit already resolved.
        survivor_count: Members left undecided (candidates to survive).
        released_count: Members released with ``not_duplicate``.
        status: "pending" or "resolved".
        survivor_record_id: The group's surviving submission.
        conflict_columns: Columns whose values differ across the group.
        created: The group's earliest submission timestamp.
    """

    id: str
    sheet: str
    entity: str
    group_key: str
    member_count: int
    decided_count: int
    survivor_count: int = 0
    released_count: int = 0
    status: str
    survivor_record_id: str = ""
    conflict_columns: list[str] = Field(default_factory=list)
    created: str | None = None


class DuplicationListPage(StrictModel):
    """One page of duplicate groups."""

    items: list[DuplicationListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50


class DuplicationChild(StrictModel):
    """One child attributed to a duplicate family group.

    Attributes:
        zite_record_id: The child's raw submission.
        first_name: The child's first name.
        dob: The child's date of birth.
        family_id: The family the child currently belongs to.
        angel_id: The child's current angel letter.
    """

    zite_record_id: str
    first_name: str | None = None
    dob: str | None = None
    family_id: int | None = None
    angel_id: str | None = None


class DuplicationMember(StrictModel):
    """One member of a duplicate group.

    Attributes:
        zite_record_id: The raw submission.
        created: The submission timestamp.
        survivor: Whether the submission is the group's survivor.
        conflict_columns: Columns whose values differ across the group.
        conflict_details: The conflicting values.
        raw: The raw submission record.
        screened: The screened result (the final row), when it has one.
        decision: "not_duplicate", "ignore", or None.
        assigned_id: The unique id the decision assigned.
        decided_by: The actor who decided.
        decided_at: When the decision was recorded.
        row_version: The screened row's optimistic-concurrency version.
        state: "surviving", "released", or "discarded".
        row_key: The key of the final row representing the member.
        row_status: That row's stored status.
        children: The children attributed to this family entry.
    """

    zite_record_id: str
    created: str | None = None
    survivor: bool = False
    conflict_columns: list[str] = Field(default_factory=list)
    conflict_details: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    screened: dict[str, Any] | None = None
    decision: str | None = None
    assigned_id: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    row_version: int | None = None
    state: str = "surviving"
    row_key: str = ""
    row_status: str | None = None
    children: list[DuplicationChild] = Field(default_factory=list)


class DuplicationDetail(StrictModel):
    """One duplicate group's raw and screened results.

    Attributes:
        id: The group's stable, URL-safe id.
        sheet: The duplicate-log sheet label.
        entity: "child", "family", or "sponsor".
        group_key: The duplicate group's key.
        status: "pending", "resolved", or "audited".
        audited_at: When the group was marked audited.
        members: The group's submissions.
        survivor_count: Members left undecided (candidates to survive).
        children: The group's children (family groups).
        previous: The group before this one in the list's order.
        next: The group after this one in the list's order.
    """

    id: str
    sheet: str
    entity: str
    group_key: str
    status: str
    audited_at: str | None = None
    members: list[DuplicationMember] = Field(default_factory=list)
    survivor_count: int = 0
    children: list[DuplicationChild] = Field(default_factory=list)
    previous: NeighbourView | None = None
    next: NeighbourView | None = None


class DuplicationDecisionRequest(StrictModel):
    """One duplication-audit decision.

    Attributes:
        decision: "not_duplicate" or "ignore".
        confirm: Must be True; anything else refuses the action.
        expected_version: The caller's expected ``row_version``.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["not_duplicate", "ignore"]
    confirm: bool
    expected_version: int | None = None


class DuplicationAuditRequest(StrictModel):
    """The reviewed values carried by a group's audit.

    Attributes:
        values: The ``Current (editable)`` values under canonical names.
        expected_version: The surviving entry's expected ``row_version``.
    """

    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] | None = None
    expected_version: int | None = None


class DuplicationChildrenRequest(StrictModel):
    """A split family's child assignments.

    Attributes:
        assignments: Child record id -> target family id.
    """

    model_config = ConfigDict(extra="forbid")

    assignments: dict[str, int] = Field(default_factory=dict)


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
    "DuplicationAuditRequest",
    "DuplicationChild",
    "DuplicationChildrenRequest",
    "DuplicationDecisionRequest",
    "DuplicationDetail",
    "DuplicationListItem",
    "DuplicationListPage",
    "DuplicationMember",
    "EntityDetail",
    "EntityListPage",
    "EntityPatch",
    "HealthView",
    "IgnoreRequest",
    "JobCreate",
    "JobEventView",
    "JobView",
    "MeView",
    "NeighbourView",
    "Overview",
    "SeasonView",
    "UserCreate",
    "UserPatch",
    "UserView",
    "canonicalize",
    "decanonicalize_patch",
]
