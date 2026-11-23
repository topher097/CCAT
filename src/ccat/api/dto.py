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


class ManualMatchHold(StrictModel):
    """One family hold (manual matching).

    Attributes:
        held: Whether the family carries an active hold.
        note: Why the family was set aside; the reason, verbatim.
        created_by: Who set it aside.
        created_at: When it was set aside.
    """

    held: bool = False
    note: str | None = None
    created_by: str | None = None
    created_at: str | None = None


class ManualMatchChildCard(StrictModel):
    """One child's overview line on a family card.

    Attributes:
        zite_record_id: The child's key; the assignment grain.
        angel_id: The child's angel letter.
        first_name: The child's first name.
        age_at_xmas: The child's age at Christmas.
        gender: The child's gender.
        special_needs: Whether the child has special needs.
        need_1: First stated need.
        need_2: Second stated need.
        desire_1: First stated want.
        desire_2: Second stated want.
        interests: The child's interests.
        other_notes: Free-text notes.
        favorite_colors: The child's favourite colours.
        sizes: The child's sizes, grouped for display.
        status: The child's audit status.
        assigned_sponsor_id: The child's sponsor, when placed.
        assignment_source: "auto" or "manual".
        generated_info_sheet: Whether a sheet already exists on disk.
        row_version: The child's optimistic-concurrency version.
        selectable: Whether the child may be assigned.
        unselectable_reason: Why the child may not be assigned.
    """

    zite_record_id: str
    angel_id: str | None = None
    first_name: str | None = None
    age_at_xmas: int | None = None
    gender: str | None = None
    special_needs: bool = False
    need_1: str | None = None
    need_2: str | None = None
    desire_1: str | None = None
    desire_2: str | None = None
    interests: str | None = None
    other_notes: str | None = None
    favorite_colors: str | None = None
    sizes: dict[str, str | None] = Field(default_factory=dict)
    status: str | None = None
    assigned_sponsor_id: int | None = None
    assignment_source: str | None = None
    generated_info_sheet: bool = False
    row_version: int = 0
    selectable: bool = True
    unselectable_reason: str | None = None


class ManualMatchFamilyCard(StrictModel):
    """One family's card: the header plus its children.

    Attributes:
        family_id: The family's key.
        row_version: The family's optimistic-concurrency version.
        first_name: The family's first name.
        last_name: The family's last name.
        primary_email: The family's email.
        phone: The family's phone.
        city: The family's city.
        zip_code: The family's zip code.
        state: The family's state.
        gift_pickup_location: The family's pickup location label.
        status: The family's audit status.
        family_size: The number of non-ignored children.
        assigned_count: The number of children with a sponsor.
        assigned_sponsor_ids: The sponsors the family's children hold.
        assignment_state: "unassigned", "partial", "split", "assigned".
        children: The family's children.
        hold: The family's active hold, if any.
        warnings: Family-level advisories.
    """

    family_id: int
    row_version: int = 0
    first_name: str | None = None
    last_name: str | None = None
    primary_email: str | None = None
    phone: str | None = None
    city: str | None = None
    zip_code: str | None = None
    state: str | None = None
    gift_pickup_location: str | None = None
    status: str | None = None
    family_size: int = 0
    assigned_count: int = 0
    assigned_sponsor_ids: list[int] = Field(default_factory=list)
    assignment_state: str = "unassigned"
    children: list[ManualMatchChildCard] = Field(default_factory=list)
    hold: ManualMatchHold = Field(default_factory=ManualMatchHold)
    warnings: list[str] = Field(default_factory=list)


class ManualMatchAutomaticRun(StrictModel):
    """The automatic matcher's state for the season.

    Attributes:
        active: Whether a match job is queued or running.
        job_id: The active job's id.
        status: The active job's status.
        started_at: When the active job started.
    """

    active: bool = False
    job_id: int | None = None
    status: str | None = None
    started_at: str | None = None


class ManualMatchPoolPage(StrictModel):
    """One page of the manual-matching pool.

    Attributes:
        items: The family cards.
        total: The number of matching families.
        page: The 1-based page number.
        page_size: The page size.
        automatic_run: The automatic matcher's state.
    """

    items: list[ManualMatchFamilyCard] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 25
    automatic_run: ManualMatchAutomaticRun = Field(
        default_factory=ManualMatchAutomaticRun
    )


class ManualMatchSponsor(StrictModel):
    """One sponsor in the manual picker.

    Attributes:
        sponsor_id: The sponsor's key.
        row_version: The sponsor's optimistic-concurrency version.
        display_name: The organization name, or the first and last name.
        email: The sponsor's email.
        phone: The sponsor's phone.
        sponsor_type: "Individual", "Organization", "Walmart", ...
        num_children: How many children the sponsor committed to.
        assigned_count: How many children are placed with the sponsor.
        remaining: The free capacity; None when uncapped.
        age_preference: The sponsor's age preference label.
        gender_preference: The sponsor's gender preference label.
        dropoff_location: The sponsor's drop-off location label.
        pref_age_min: The lowest preferred age.
        pref_age_max: The highest preferred age.
        status: The sponsor's audit status.
        first_name: The sponsor contact's first name.
        last_name: The sponsor contact's last name.
        organization_name: The organization name, when there is one.
        elevated_budget: The auditor's elevated-budget flag (the store
            reads an unset flag as False).
        physical_packet: The auditor's physical-packet flag (the store
            reads an unset flag as False).
        packets_dispatched: Whether the info-sheet packets were sent.
        packets_dispatched_at: When the packets were dispatched (ISO 8601).
    """

    sponsor_id: int
    row_version: int = 0
    display_name: str = ""
    first_name: str | None = None
    last_name: str | None = None
    organization_name: str | None = None
    elevated_budget: bool | None = None
    physical_packet: bool | None = None
    packets_dispatched: bool = False
    packets_dispatched_at: str | None = None
    email: str | None = None
    phone: str | None = None
    sponsor_type: str | None = None
    num_children: int | None = None
    assigned_count: int = 0
    remaining: int | None = None
    age_preference: str | None = None
    gender_preference: str | None = None
    dropoff_location: str | None = None
    pref_age_min: int | None = None
    pref_age_max: int | None = None
    status: str | None = None


class ManualMatchSponsorPage(StrictModel):
    """One page of the manual sponsor picker.

    Attributes:
        items: The sponsors.
        total: The number of matching sponsors.
        page: The 1-based page number.
        page_size: The page size.
    """

    items: list[ManualMatchSponsor] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 25


class ManualAssignmentRequest(StrictModel):
    """Assign a selection of children to a sponsor.

    Attributes:
        sponsor_id: The sponsor the selection goes to.
        family_ids: Whole families to place; the server expands each to
            its eligible children.
        child_ids: Individual children, by ``zite_record_id``.
        expected_versions: The caller's expected row versions, keyed
            ``child:<id>``, ``family:<id>``, ``sponsor:<id>``.
        confirm: Required when a selected child already has a sponsor.
        note: Free text recorded in the audit event.
    """

    model_config = ConfigDict(extra="forbid")

    sponsor_id: int
    family_ids: list[int] = Field(default_factory=list)
    child_ids: list[str] = Field(default_factory=list)
    expected_versions: dict[str, int] = Field(default_factory=dict)
    confirm: bool = False
    note: str | None = None


class ManualReleaseRequest(StrictModel):
    """Release assignments.

    Attributes:
        child_ids: Individual children to release.
        family_ids: Families whose assigned children are all released.
        expected_versions: The caller's expected row versions.
        confirm: Required; releasing is destructive.
        note: Free text recorded in the audit event.
    """

    model_config = ConfigDict(extra="forbid")

    child_ids: list[str] = Field(default_factory=list)
    family_ids: list[int] = Field(default_factory=list)
    expected_versions: dict[str, int] = Field(default_factory=dict)
    confirm: bool = False
    note: str | None = None


class ManualDispatchRequest(StrictModel):
    """Dispatch a sponsor's info-sheet packets.

    Attributes:
        sponsor_id: The sponsor whose packets went out.
        expected_versions: The caller's expected row versions, keyed
            ``sponsor:<id>``.
    """

    model_config = ConfigDict(extra="forbid")

    sponsor_id: int
    expected_versions: dict[str, int] = Field(default_factory=dict)


class ManualAssignmentEntry(StrictModel):
    """One child's assignment outcome.

    Attributes:
        zite_record_id: The child's key.
        angel_id: The child's angel letter.
        family_id: The child's family.
        from_sponsor_id: The child's previous sponsor, when it moved.
    """

    zite_record_id: str
    angel_id: str | None = None
    family_id: int | None = None
    from_sponsor_id: int | None = None


class ManualAssignmentFamilySummary(StrictModel):
    """One family's assignment summary after a write.

    Attributes:
        family_id: The family's key.
        assigned_sponsor_id: The family's single sponsor, when it has one.
        assigned_sponsor_ids: Every sponsor the family's children hold.
        assigned_count: The number of placed children.
        family_size: The number of non-ignored children.
        assignment_state: "unassigned", "partial", "split", "assigned".
        hold_released: Whether the write released the family's hold.
    """

    family_id: int
    assigned_sponsor_id: int | None = None
    assigned_sponsor_ids: list[int] = Field(default_factory=list)
    assigned_count: int = 0
    family_size: int = 0
    assignment_state: str = "unassigned"
    hold_released: bool = False


class ManualAssignmentSponsorSummary(StrictModel):
    """One sponsor's assignment summary after a write.

    Attributes:
        sponsor_id: The sponsor's key.
        assigned_count: The number of children placed with the sponsor.
        remaining: The free capacity; None when uncapped.
    """

    sponsor_id: int
    assigned_count: int = 0
    remaining: int | None = None


class ManualAssignmentResult(StrictModel):
    """The outcome of an assign or release request.

    Attributes:
        assigned: The children newly assigned.
        reassigned: The children moved from another sponsor.
        released: The children released.
        unchanged: The children already held by this sponsor.
        warnings: The advisories; they never block.
        families: The affected families' summaries.
        sponsors: The affected sponsors' summaries.  A release that
            frees children on several sponsors names each of them.
        versions: The new row versions, so the client need not reload.
    """

    assigned: list[ManualAssignmentEntry] = Field(default_factory=list)
    reassigned: list[ManualAssignmentEntry] = Field(default_factory=list)
    released: list[ManualAssignmentEntry] = Field(default_factory=list)
    unchanged: list[ManualAssignmentEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    families: list[ManualAssignmentFamilySummary] = Field(default_factory=list)
    sponsors: list[ManualAssignmentSponsorSummary] = Field(default_factory=list)
    versions: dict[str, int] = Field(default_factory=dict)


class ManualAssignmentListItem(StrictModel):
    """One current assignment, as the manual list shows it.

    Attributes:
        zite_record_id: The child's key.
        angel_id: The child's angel letter.
        family_id: The child's family.
        sponsor_id: The child's sponsor.
        assignment_source: "auto" or "manual".
        last_updated_by: Who last wrote the child row.
        last_updated_at: When the child row was last written.
        first_name: The angel's first name.
        age_at_xmas: The angel's age at Christmas.
        need_1: First stated need.
        need_2: Second stated need.
        desire_1: First stated want.
        desire_2: Second stated want.
        dropoff_location: The family's gift pickup location (the angel's drop-off).
    """

    zite_record_id: str
    angel_id: str | None = None
    family_id: int | None = None
    sponsor_id: int | None = None
    assignment_source: str | None = None
    last_updated_by: str | None = None
    last_updated_at: str | None = None
    first_name: str | None = None
    age_at_xmas: int | None = None
    need_1: str | None = None
    need_2: str | None = None
    desire_1: str | None = None
    desire_2: str | None = None
    dropoff_location: str | None = None


class ManualAssignmentListPage(StrictModel):
    """One page of the current assignment list.

    Attributes:
        items: The assignments.
        total: The number of matching assignments.
        page: The 1-based page number.
        page_size: The page size.
    """

    items: list[ManualAssignmentListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 25


class ManualHoldRequest(StrictModel):
    """Hold a family, with the reason.

    Attributes:
        family_id: The family to hold.
        note: Why the family is set aside.  Required, never blank.
    """

    model_config = ConfigDict(extra="forbid")

    family_id: int
    note: str


class ManualHoldReleaseRequest(StrictModel):
    """Release a family's hold.

    Attributes:
        family_id: The family whose hold is released.
    """

    model_config = ConfigDict(extra="forbid")

    family_id: int


class ManualHoldResult(StrictModel):
    """One hold's state.

    Attributes:
        family_id: The family's key.
        held: Whether the family carries an active hold.
        note: The hold's note.
        created_by: Who set it aside.
        created_at: When it was set aside.
        released_at: When it was released.
        released_by: Who released it.
    """

    family_id: int
    held: bool = False
    note: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    released_at: str | None = None
    released_by: str | None = None


class ManualHoldListPage(StrictModel):
    """One page of the active holds.

    Attributes:
        items: The active holds.
        total: The number of active holds.
        page: The 1-based page number.
        page_size: The page size.
    """

    items: list[ManualHoldResult] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 25


class MatchRunSettings(StrictModel):
    """The automatic matcher's rememberable run flags.

    Attributes:
        test: Match only the first sponsor in phase order.  It still
            mutates persistent state.
        skip_match: Reconcile the assignment summaries without searching.
    """

    model_config = ConfigDict(extra="forbid")

    test: bool = False
    skip_match: bool = False


class GenerateRunSettings(StrictModel):
    """The sheet and packet generation command's rememberable run flags.

    Attributes:
        test: Render only the first unit of work.
        overwrite: Re-render sheets that already exist.
        max_workers: The number of rendering workers.
    """

    model_config = ConfigDict(extra="forbid")

    test: bool = False
    overwrite: bool = False
    max_workers: int = 4


class ScreenRunSettings(StrictModel):
    """The screening command's rememberable AI flag.

    Attributes:
        overwrite_new: Let the model's corrections overwrite the
            existing values of children whose status is still ``new``.
    """

    model_config = ConfigDict(extra="forbid")

    overwrite_new: bool = False


class RunSettingsView(StrictModel):
    """One command's effective run settings.

    Attributes:
        command: The command name.
        run: The command's run flags.
        weights: The matcher's tier weights; only for "match".
        config_path: The configuration file the flags live in.
        editable: Whether the caller may change them.
        saved: True when the response follows a successful write.
    """

    command: str
    run: dict[str, Any] = Field(default_factory=dict)
    weights: dict[str, Any] | None = None
    config_path: str = ""
    editable: bool = False
    saved: bool = False


class DispatchAttachment(StrictModel):
    """One file attached to a sponsor packet email.

    Attributes:
        artifact_id: The artifact row, when the file is registered.
        role: What the file is ("packet_pdf", "instructions_pdf").
        filename: The name the recipient sees.
        content_type: The MIME type.
        size_bytes: The file's size.
        sha256: The hash of the file's bytes.
    """

    artifact_id: int | None = None
    role: str
    filename: str
    content_type: str = "application/pdf"
    size_bytes: int = 0
    sha256: str = ""


class DispatchRow(StrictModel):
    """One sponsor's row in the dispatch work list.

    Attributes:
        sponsor_id: The sponsor's key.
        display_name: The sponsor's name.
        first_name: The sponsor's first name.
        last_name: The sponsor's last name.
        organization_name: The sponsor's organization.
        sponsor_type: The sponsor's affiliation.
        email: The sponsor's address.
        physical_packet: Whether the sponsor receives a physical packet.
        num_children: How many children the sponsor asked for.
        assigned_count: How many children are placed with the sponsor.
        assigned_angel_ids: The placed children's angel letters.
        sheets_ready_count: How many of them have an info sheet.
        sheets_total: How many of them there are.
        missing_sheet_angel_ids: The ones without a sheet.
        packet_status: "ready" or "missing".
        packet_pdf_artifact_id: The combined child information PDF.
        packet_created_at: When the packet was built.
        instruction_pdf_artifact_id: The sponsor instruction PDF.
        attachments: The files the email will carry.
        email_status: The email state ("not_drafted", "draft_ready",
            "draft_edited", "sending", "sent", "failed", "unknown").
        email_sent_at: When the email was accepted.
        email_error: The last failure's reason.
        draft_version: The draft's version, when it has one.
        draft_subject: The draft's subject.
        packets_dispatched: Whether the sponsor's packets went out.
        packets_dispatched_at: When they went out.
        sponsor_row_version: The sponsor row's version.
        duplicate_recipient_sponsor_ids: Other sponsors with the same
            address.
        blockers: Why the sponsor cannot be sent yet.
    """

    sponsor_id: int
    display_name: str = ""
    first_name: str | None = None
    last_name: str | None = None
    organization_name: str | None = None
    sponsor_type: str | None = None
    email: str | None = None
    physical_packet: bool = False
    num_children: int | None = None
    assigned_count: int = 0
    assigned_angel_ids: list[str] = []
    sheets_ready_count: int = 0
    sheets_total: int = 0
    missing_sheet_angel_ids: list[str] = []
    packet_status: str = "missing"
    packet_pdf_artifact_id: int | None = None
    packet_created_at: str | None = None
    instruction_pdf_artifact_id: int | None = None
    attachments: list[DispatchAttachment] = []
    email_status: str = "not_drafted"
    email_sent_at: str | None = None
    email_error: str | None = None
    draft_version: int | None = None
    draft_subject: str | None = None
    packets_dispatched: bool = False
    packets_dispatched_at: str | None = None
    sponsor_row_version: int = 0
    duplicate_recipient_sponsor_ids: list[int] = []
    blockers: list[str] = []


class DispatchTotals(StrictModel):
    """The work list's counters.

    Attributes:
        sponsors: Sponsors with children.
        assigned_children: Children placed with them.
        sheets_ready: Children with an info sheet.
        sheets_missing: Children without one.
        packets_ready: Sponsors with a usable packet.
        packets_missing: Sponsors without one.
        dispatched: Sponsors whose packets went out.
    """

    sponsors: int = 0
    assigned_children: int = 0
    sheets_ready: int = 0
    sheets_missing: int = 0
    packets_ready: int = 0
    packets_missing: int = 0
    dispatched: int = 0


class DispatchListView(StrictModel):
    """The dispatch work list.

    Attributes:
        season: The season name.
        mail_configured: Whether the mail account is usable.
        can_send: Whether the caller may send.
        totals: The page counters.
        rows: One row per sponsor with children.
    """

    season: str
    mail_configured: bool = False
    can_send: bool = False
    totals: DispatchTotals = DispatchTotals()
    rows: list[DispatchRow] = []


class DispatchChild(StrictModel):
    """One assigned child's sheet state.

    Attributes:
        zite_record_id: The child's key.
        angel_id: The child's angel letter.
        family_id: The child's family.
        sponsor_id: The sponsor the child is placed with.
        generated_info_sheet: Whether the sheet exists.
        sheet_artifact_id: The sheet artifact.
    """

    zite_record_id: str
    angel_id: str | None = None
    family_id: int | None = None
    sponsor_id: int | None = None
    generated_info_sheet: bool = False
    sheet_artifact_id: int | None = None


class DispatchChildListView(StrictModel):
    """The assigned children and their sheet state.

    Attributes:
        rows: One row per assigned child.
    """

    rows: list[DispatchChild] = []


class DispatchDraftView(StrictModel):
    """One sponsor's email draft.

    Attributes:
        sponsor_id: The sponsor's key.
        version: The draft's version, for If-Match updates.
        subject: The subject.
        body_html: The rendered body.
        note: The coordinator's plain-text addition.
        to_email: The recipient.
        from_email: The address the mail goes out as.
        bcc_emails: The archive addresses.
        attachments: The files the email will carry.
        build_id: The packet build the body came from.
        email_status: The email state.
        email_error: The last failure's reason.
        blockers: Why the sponsor cannot be sent yet.
    """

    sponsor_id: int
    version: int = 0
    subject: str = ""
    body_html: str = ""
    note: str = ""
    to_email: str = ""
    from_email: str = ""
    bcc_emails: list[str] = []
    attachments: list[DispatchAttachment] = []
    build_id: str | None = None
    email_status: str = "not_drafted"
    email_error: str | None = None
    blockers: list[str] = []


class DispatchDraftRequest(StrictModel):
    """Change one draft.

    Attributes:
        subject: The new subject.
        note: The new plain-text note.
    """

    model_config = ConfigDict(extra="forbid")

    subject: str | None = None
    note: str | None = None


class DispatchDraftBatchRequest(StrictModel):
    """Draft one or more sponsors' emails.

    Attributes:
        sponsor_ids: The sponsors to draft; every ready sponsor when
            empty.
        all: Draft every ready sponsor.
        overwrite: Re-render a draft that already exists.
    """

    model_config = ConfigDict(extra="forbid")

    sponsor_ids: list[int] = []
    all: bool = False
    overwrite: bool = False


class DispatchSendRequest(StrictModel):
    """Send one sponsor's packet email.

    Attributes:
        version: The draft version the popup was opened with.
        subject: The subject being sent.
        note: The note being sent.
    """

    model_config = ConfigDict(extra="forbid")

    version: int
    subject: str | None = None
    note: str | None = None


class DispatchSendResult(StrictModel):
    """The outcome of one send attempt.

    Attributes:
        sponsor_id: The sponsor's key.
        state: "sent", "failed", or "unknown".
        attempt_id: The attempt's id.
        message_id: The RFC 5322 Message-ID.
        draft_version: The draft's version after the attempt.
        sent_at: When the message was accepted.
        error: The failure's reason.
    """

    sponsor_id: int
    state: str
    attempt_id: str = ""
    message_id: str = ""
    draft_version: int = 0
    sent_at: str | None = None
    error: str | None = None


__all__ = [
    "DispatchAttachment",
    "DispatchChild",
    "DispatchChildListView",
    "DispatchDraftBatchRequest",
    "DispatchDraftRequest",
    "DispatchDraftView",
    "DispatchListView",
    "DispatchRow",
    "DispatchSendRequest",
    "DispatchSendResult",
    "DispatchTotals",
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
    "GenerateRunSettings",
    "HealthView",
    "IgnoreRequest",
    "JobCreate",
    "JobEventView",
    "JobView",
    "ManualAssignmentEntry",
    "ManualAssignmentFamilySummary",
    "ManualAssignmentListItem",
    "ManualAssignmentListPage",
    "ManualAssignmentRequest",
    "ManualAssignmentResult",
    "ManualAssignmentSponsorSummary",
    "ManualHoldListPage",
    "ManualHoldReleaseRequest",
    "ManualHoldRequest",
    "ManualHoldResult",
    "ManualMatchAutomaticRun",
    "ManualMatchChildCard",
    "ManualMatchFamilyCard",
    "ManualMatchHold",
    "ManualMatchPoolPage",
    "ManualMatchSponsor",
    "ManualMatchSponsorPage",
    "ManualReleaseRequest",
    "MatchRunSettings",
    "MeView",
    "NeighbourView",
    "Overview",
    "RunSettingsView",
    "ScreenRunSettings",
    "SeasonView",
    "UserCreate",
    "UserPatch",
    "UserView",
    "canonicalize",
    "decanonicalize_patch",
]
