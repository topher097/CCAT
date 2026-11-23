"""Declarative models for the CCAT database (plan §4).

The three ``final_*`` tables mirror the workbook persistent-final
schemas exactly (``ccat.audit.store`` entity specs) plus the
version/attribution columns the refactor adds (plan §4.3):
``row_version``, ``created_by``, ``last_updated_by``, ``created_at``,
``last_updated_at``.  Raw tables are verbatim acquisition snapshots;
diagnostic, validation, match, artifact, job, account, and
clarification tables follow the plan's table groups (§4.1).

Portability rules (§4.5): only portable types (Text, Integer,
BigInteger, Boolean, Float, DateTime(timezone=True), JSON); all
constraints and indexes carry explicit names; no dialect-specific
defaults.  Uniqueness is deliberately absent where the plan forbids it
(raw ``zite_record_id``, family/sponsor email, email-check targets);
the single active acquisition run per (season, table) is enforced by
the repository's atomic promotion transaction, not a unique index.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
)

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base with a stable constraint-naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Nullable per-column type aliases keep the table bodies readable;
# required columns spell out their mapped_column in full.
OptionalStr = Mapped[str | None]
OptionalInt = Mapped[int | None]
OptionalBigInt = Mapped[int | None]
OptionalBool = Mapped[bool | None]
OptionalJson = Mapped[Any | None]
OptionalTzDateTime = Mapped[datetime | None]


class RowVersionMixin:
    """Optimistic-concurrency and attribution columns (plan §4.3).

    ``row_version`` supports If-Match style updates; ``created_by`` /
    ``last_updated_by`` attribute every change to a user or system
    actor (imported rows carry the migration identity); ``audited_at``
    stays the separate human-audit authority and never doubles as a
    generic "last touched" marker.
    """

    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    last_updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Season(Base):
    """One season's identity, Christmas date, and config snapshot.

    The config hash/snapshot is pinned on every run so later config
    edits never alter a running job's meaning (plan §4.1, decision D4).
    """

    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    #: Christmas date in the config's MM/DD/YYYY spelling, verbatim.
    xmas_date: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FinalChild(RowVersionMixin, Base):
    """Persistent final child store (plan §4.1).

    Key: ``(season_id, zite_record_id)`` — one final row per Zite
    child submission.  ``family_id`` is nullable for orphans and
    deliberately carries no FK (legacy data may predate the family
    row); ``angel_id`` uniqueness is enforced per season only where
    non-null.  ``ai_screening_notes`` is the renamed legacy
    ``potential_anomoly`` workbook column.
    """

    __tablename__ = "final_child"
    __table_args__ = (
        # Not unique: a child that is still a pending duplicate shares
        # its group's angel letter until the duplication audit resolves
        # it, so two rows can carry one letter for a while.
        Index("ix_final_child_season_angel", "season_id", "angel_id"),
        Index("ix_final_child_season_family", "season_id", "family_id"),
        Index("ix_final_child_season_status", "season_id", "status"),
        Index("ix_final_child_season_ignore", "season_id", "ignore"),
        # The child's own sponsor — the canonical assignment grain.  The
        # mirror of ``ix_final_family_season_sponsor``; it serves "this
        # sponsor's children", the packet builder's main query.
        Index(
            "ix_final_child_season_sponsor", "season_id", "assigned_sponsor_id"
        ),
    )

    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), primary_key=True
    )
    zite_record_id: Mapped[str] = mapped_column(Text, primary_key=True)
    zite_created_at: OptionalStr = mapped_column(Text)
    created: OptionalStr = mapped_column(Text)
    family_id: OptionalBigInt = mapped_column(BigInteger)
    angel_id: OptionalStr = mapped_column(Text)
    first_name: OptionalStr = mapped_column(Text)
    primary_email: OptionalStr = mapped_column(Text)
    dob: OptionalStr = mapped_column(Text)
    age_reported: OptionalInt = mapped_column(Integer)
    gender: OptionalStr = mapped_column(Text)
    #: Sorted semicolon-joined text, not a Boolean (plan §4.3).
    special_needs: OptionalStr = mapped_column(Text)
    need_1: OptionalStr = mapped_column(Text)
    need_2: OptionalStr = mapped_column(Text)
    desire_1: OptionalStr = mapped_column(Text)
    desire_2: OptionalStr = mapped_column(Text)
    shoe_size: OptionalStr = mapped_column(Text)
    shoe_size_type: OptionalStr = mapped_column(Text)
    pant_waist_size: OptionalStr = mapped_column(Text)
    pant_inseam: OptionalStr = mapped_column(Text)
    pant_size_letter: OptionalStr = mapped_column(Text)
    pant_size_type: OptionalStr = mapped_column(Text)
    jean_size: OptionalStr = mapped_column(Text)
    shirt_size_letter: OptionalStr = mapped_column(Text)
    shirt_size_type: OptionalStr = mapped_column(Text)
    favorite_colors: OptionalStr = mapped_column(Text)
    interests: OptionalStr = mapped_column(Text)
    other_notes: OptionalStr = mapped_column(Text)
    #: Renamed from the legacy workbook column ``potential_anomoly``.
    ai_screening_notes: OptionalStr = mapped_column(Text)
    source: OptionalStr = mapped_column(Text)
    age_at_xmas: OptionalInt = mapped_column(Integer)
    generated_info_sheet: OptionalBool = mapped_column(Boolean)
    screened: OptionalBool = mapped_column(Boolean)
    potential_duplicate: OptionalBool = mapped_column(Boolean)
    status: OptionalStr = mapped_column(Text)
    ignore: OptionalBool = mapped_column(Boolean)
    clarification_notes: OptionalStr = mapped_column(Text)
    #: Legacy ISO string kept verbatim; the human audit authority.
    audited_at: OptionalStr = mapped_column(Text)
    #: The sponsor of THIS child (NULL = unassigned).  Canonical: the
    #: family and sponsor assignment columns are derived from it.  No FK
    #: to ``final_sponsor``, matching the house convention.
    assigned_sponsor_id: OptionalBigInt = mapped_column(BigInteger)
    #: "auto" | "manual" — who placed the child.  A manual assignment
    #: is protected from the automatic matcher.
    assignment_source: OptionalStr = mapped_column(Text)


class FinalFamily(RowVersionMixin, Base):
    """Persistent final family store (plan §4.1).

    Key: ``(season_id, family_id)``.  ``primary_email`` is
    intentionally NOT unique — duplicate family submissions share the
    stable ``family_id`` (plan §4.1).  ``assigned_angel_ids`` keeps the
    legacy comma-joined text form.
    """

    __tablename__ = "final_family"
    __table_args__ = (
        Index("ix_final_family_season_status", "season_id", "status"),
        Index("ix_final_family_season_ignore", "season_id", "ignore"),
        Index(
            "ix_final_family_season_sponsor",
            "season_id",
            "assigned_sponsor_id",
        ),
    )

    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), primary_key=True
    )
    family_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    zite_record_id: OptionalStr = mapped_column(Text)
    zite_created_at: OptionalStr = mapped_column(Text)
    created: OptionalStr = mapped_column(Text)
    primary_email: OptionalStr = mapped_column(Text)
    first_name: OptionalStr = mapped_column(Text)
    last_name: OptionalStr = mapped_column(Text)
    phone: OptionalStr = mapped_column(Text)
    street_address: OptionalStr = mapped_column(Text)
    apt_floor: OptionalStr = mapped_column(Text)
    city: OptionalStr = mapped_column(Text)
    zip_code: OptionalStr = mapped_column(Text)
    state: OptionalStr = mapped_column(Text)
    secondary_first_name: OptionalStr = mapped_column(Text)
    secondary_last_name: OptionalStr = mapped_column(Text)
    secondary_email: OptionalStr = mapped_column(Text)
    secondary_phone: OptionalStr = mapped_column(Text)
    gift_pickup_location: OptionalStr = mapped_column(Text)
    source: OptionalStr = mapped_column(Text)
    potential_duplicate: OptionalBool = mapped_column(Boolean)
    accepts_disclaimer: OptionalBool = mapped_column(Boolean)
    assigned_sponsor_id: OptionalBigInt = mapped_column(BigInteger)
    #: Comma-joined legacy spelling preserved at the boundary.
    assigned_angel_ids: OptionalStr = mapped_column(Text)
    num_angels_in_family: OptionalInt = mapped_column(Integer)
    status: OptionalStr = mapped_column(Text)
    clarification_notes: OptionalStr = mapped_column(Text)
    audited_at: OptionalStr = mapped_column(Text)
    ignore: OptionalBool = mapped_column(Boolean)


class FinalSponsor(RowVersionMixin, Base):
    """Persistent final sponsor store (plan §4.1).

    Key: ``(season_id, sponsor_id)``.  ``created`` is the renamed
    legacy ``create`` column (plan §4.3).  ``email`` is intentionally
    NOT unique — duplicate sponsor submissions each keep a row.
    """

    __tablename__ = "final_sponsor"
    __table_args__ = (
        Index("ix_final_sponsor_season_status", "season_id", "status"),
        Index("ix_final_sponsor_season_ignore", "season_id", "ignore"),
    )

    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), primary_key=True
    )
    sponsor_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    zite_record_id: OptionalStr = mapped_column(Text)
    #: Renamed from the legacy workbook column ``create``.
    created: OptionalStr = mapped_column(Text)
    email: OptionalStr = mapped_column(Text)
    sponsor_type: OptionalStr = mapped_column(Text)
    organization_name: OptionalStr = mapped_column(Text)
    prior_assigned_angel_ids: OptionalStr = mapped_column(Text)
    first_name: OptionalStr = mapped_column(Text)
    last_name: OptionalStr = mapped_column(Text)
    phone: OptionalStr = mapped_column(Text)
    num_children: OptionalInt = mapped_column(Integer)
    age_preference: OptionalStr = mapped_column(Text)
    gender_preference: OptionalStr = mapped_column(Text)
    dropoff_location: OptionalStr = mapped_column(Text)
    source: OptionalStr = mapped_column(Text)
    potential_duplicate: OptionalBool = mapped_column(Boolean)
    accepts_disclaimer: OptionalBool = mapped_column(Boolean)
    pref_age_min: OptionalInt = mapped_column(Integer)
    pref_age_max: OptionalInt = mapped_column(Integer)
    #: Comma-joined legacy spelling preserved at the boundary.
    assigned_angel_ids: OptionalStr = mapped_column(Text)
    num_angels_assigned: OptionalInt = mapped_column(Integer)
    #: Auditor flags; NULL means "not decided".
    elevated_budget: OptionalBool = mapped_column(Boolean)
    physical_packet: OptionalBool = mapped_column(Boolean)
    #: ISO 8601 stamp of the packet dispatch; set once the info sheets
    #: went to the sponsor, after which the sponsor's children are frozen.
    packets_dispatched_at: OptionalStr = mapped_column(Text)
    status: OptionalStr = mapped_column(Text)
    clarification_notes: OptionalStr = mapped_column(Text)
    audited_at: OptionalStr = mapped_column(Text)
    ignore: OptionalBool = mapped_column(Boolean)


class FamilyHold(Base):
    """A family set aside on purpose, with the reason (manual matching).

    A hold keeps a family out of the automatic matcher's pool without
    placing it.  The note is required — a hold without a reason is not a
    decision.  ``family_id`` carries no FK to ``final_family``, matching
    the house convention of not putting FKs between the final stores.

    The partial unique index gives at most one ACTIVE hold per family
    (the same idiom as ``uq_jobs_idempotency_key``).  A released hold
    keeps its row and note: the decision is history.
    """

    __tablename__ = "family_holds"
    __table_args__ = (
        Index("ix_family_holds_season_family", "season_id", "family_id"),
        Index(
            "uq_family_holds_active",
            "season_id",
            "family_id",
            unique=True,
            sqlite_where=text("released_at IS NULL"),
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), nullable=False
    )
    family_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Why the family was set aside.  Never blank.
    note: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    released_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    released_by: OptionalStr = mapped_column(Text)


class AcquisitionRun(Base):
    """One raw acquisition snapshot attempt (plan §4.1).

    ``table_name`` is one of "sponsor"/"family"/"child".  Exactly one
    active row per (season_id, table_name) is enforced by the
    repository's atomic promotion transaction (read-old + insert-new +
    deactivate in one transaction) — deliberately NOT a unique
    constraint.
    """

    __tablename__ = "acquisition_runs"
    __table_args__ = (
        Index(
            "ix_acquisition_runs_season_table_active",
            "season_id",
            "table_name",
            "active",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), nullable=False
    )
    table_name: OptionalStr = mapped_column(Text)
    started_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    finished_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    row_count: OptionalInt = mapped_column(Integer)
    source_ref: OptionalStr = mapped_column(Text)


class RawSponsor(Base):
    """Verbatim raw sponsor row (plan §4.1, acquisition staging).

    ``created`` is the renamed legacy ``create`` column.  No unique
    constraint on ``zite_record_id``: preparation must detect
    duplicate/blank source IDs under the existing rules.
    """

    __tablename__ = "raw_sponsor"
    __table_args__ = (
        Index("ix_raw_sponsor_run_zite", "run_id", "zite_record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("acquisition_runs.id"),
        nullable=False,
        index=True,
    )
    zite_record_id: OptionalStr = mapped_column(Text)
    created: OptionalStr = mapped_column(Text)
    email: OptionalStr = mapped_column(Text)
    accepts_disclaimer: OptionalBool = mapped_column(Boolean)
    sponsor_type: OptionalStr = mapped_column(Text)
    organization_name: OptionalStr = mapped_column(Text)
    prior_assigned_angel_ids: OptionalStr = mapped_column(Text)
    first_name: OptionalStr = mapped_column(Text)
    last_name: OptionalStr = mapped_column(Text)
    phone: OptionalStr = mapped_column(Text)
    num_children: OptionalInt = mapped_column(Integer)
    age_preference: OptionalStr = mapped_column(Text)
    gender_preference: OptionalStr = mapped_column(Text)
    dropoff_location: OptionalStr = mapped_column(Text)
    source: OptionalStr = mapped_column(Text)


class RawFamily(Base):
    """Verbatim raw family row (plan §4.1, acquisition staging)."""

    __tablename__ = "raw_family"
    __table_args__ = (
        Index("ix_raw_family_run_zite", "run_id", "zite_record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("acquisition_runs.id"),
        nullable=False,
        index=True,
    )
    zite_record_id: OptionalStr = mapped_column(Text)
    zite_created_at: OptionalStr = mapped_column(Text)
    created: OptionalStr = mapped_column(Text)
    primary_email: OptionalStr = mapped_column(Text)
    accepts_disclaimer: OptionalBool = mapped_column(Boolean)
    first_name: OptionalStr = mapped_column(Text)
    last_name: OptionalStr = mapped_column(Text)
    phone: OptionalStr = mapped_column(Text)
    street_address: OptionalStr = mapped_column(Text)
    apt_floor: OptionalStr = mapped_column(Text)
    city: OptionalStr = mapped_column(Text)
    zip_code: OptionalStr = mapped_column(Text)
    state: OptionalStr = mapped_column(Text)
    secondary_first_name: OptionalStr = mapped_column(Text)
    secondary_last_name: OptionalStr = mapped_column(Text)
    secondary_email: OptionalStr = mapped_column(Text)
    secondary_phone: OptionalStr = mapped_column(Text)
    gift_pickup_location: OptionalStr = mapped_column(Text)
    source: OptionalStr = mapped_column(Text)


class RawChild(Base):
    """Verbatim raw child row (plan §4.1, acquisition staging)."""

    __tablename__ = "raw_child"
    __table_args__ = (
        Index("ix_raw_child_run_zite", "run_id", "zite_record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("acquisition_runs.id"),
        nullable=False,
        index=True,
    )
    zite_record_id: OptionalStr = mapped_column(Text)
    zite_created_at: OptionalStr = mapped_column(Text)
    created: OptionalStr = mapped_column(Text)
    first_name: OptionalStr = mapped_column(Text)
    primary_email: OptionalStr = mapped_column(Text)
    dob: OptionalStr = mapped_column(Text)
    #: Free text in the raw table; only the final value is integer.
    age_reported: OptionalInt = mapped_column(Integer)
    gender: OptionalStr = mapped_column(Text)
    need_1: OptionalStr = mapped_column(Text)
    need_2: OptionalStr = mapped_column(Text)
    desire_1: OptionalStr = mapped_column(Text)
    desire_2: OptionalStr = mapped_column(Text)
    shoe_size: OptionalStr = mapped_column(Text)
    shoe_size_type: OptionalStr = mapped_column(Text)
    pant_waist_size: OptionalStr = mapped_column(Text)
    pant_inseam: OptionalStr = mapped_column(Text)
    pant_size_letter: OptionalStr = mapped_column(Text)
    pant_size_type: OptionalStr = mapped_column(Text)
    jean_size: OptionalStr = mapped_column(Text)
    shirt_size_letter: OptionalStr = mapped_column(Text)
    shirt_size_type: OptionalStr = mapped_column(Text)
    favorite_colors: OptionalStr = mapped_column(Text)
    interests: OptionalStr = mapped_column(Text)
    other_notes: OptionalStr = mapped_column(Text)
    source: OptionalStr = mapped_column(Text)


class ScreeningRun(Base):
    """One screening run header (plan §4.1).

    Duplicate and DOB logs owned by a run are current diagnostic
    snapshots (replaced per run), not append-only history.
    """

    __tablename__ = "screening_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), nullable=False
    )
    created_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    actor: OptionalStr = mapped_column(Text)
    config_hash: OptionalStr = mapped_column(Text)


class DuplicateLog(Base):
    """One duplicate-diagnostic row for a screening run (§4.1)."""

    __tablename__ = "duplicate_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("screening_runs.id"),
        nullable=False,
        index=True,
    )
    #: Legacy sheet value such as "family_submissions" preserved.
    sheet: OptionalStr = mapped_column(Text)
    group_key: OptionalStr = mapped_column(Text)
    zite_record_id: OptionalStr = mapped_column(Text)
    created: OptionalStr = mapped_column(Text)
    survivor: OptionalBool = mapped_column(Boolean)
    conflict_columns: OptionalStr = mapped_column(Text)
    conflict_details: OptionalStr = mapped_column(Text)
    #: JSON serialized to text, verbatim.
    record: OptionalStr = mapped_column(Text)


class DuplicationAudit(Base):
    """One human duplication-audit decision (the Duplications section).

    Append-only: a re-decision adds a row instead of overwriting the
    earlier one, so the trail of what was decided and by whom survives.
    ``decision`` is ``not_duplicate`` (the member is a unique entity and
    received its own id) or ``ignore`` (the member is ignored, exactly
    like the entity ignore action).  ``assigned_id`` records the unique
    id the decision gave the member (angel letter, family id, or
    sponsor id).
    """

    __tablename__ = "duplication_audit"
    __table_args__ = (
        Index(
            "ix_duplication_audit_season_group",
            "season_id",
            "sheet",
            "group_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), nullable=False
    )
    #: Legacy sheet label such as "child_submissions".
    sheet: Mapped[str] = mapped_column(Text, nullable=False)
    #: The duplicate group's key (email, or email|name for children).
    group_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: The audited group member (one raw submission).
    zite_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: "not_duplicate" or "ignore".
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    #: The unique id the decision assigned, when it assigned one.
    assigned_id: OptionalStr = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: OptionalTzDateTime = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DuplicationState(Base):
    """One duplicate group's adjudication state (the Duplications section).

    The per-member decisions live in :class:`DuplicationAudit`; this
    table holds the group's own state.  "Audited" is the flag that the
    affected ``final_*`` tables have been reconciled with the group's
    adjudication, so a later reconcile run can re-apply it.
    """

    __tablename__ = "duplication_state"
    __table_args__ = (
        UniqueConstraint(
            "season_id", "sheet", "group_key", name="uq_duplication_state_group"
        ),
        Index("ix_duplication_state_season_status", "season_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), nullable=False
    )
    #: Legacy sheet label such as "child_submissions".
    sheet: Mapped[str] = mapped_column(Text, nullable=False)
    #: The duplicate group's key (email, or email|name for children).
    group_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: "audited".
    status: Mapped[str] = mapped_column(Text, nullable=False)
    audited_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    audited_by: OptionalStr = mapped_column(Text)
    #: The key of the row carrying the group's merged entry.
    entry_key: OptionalStr = mapped_column(Text)
    #: When the affected ``final_*`` rows were last reconciled.
    reconciled_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))


class DobConflictLog(Base):
    """One sibling DOB-conflict row for a screening run (§4.1)."""

    __tablename__ = "dob_conflict_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("screening_runs.id"),
        nullable=False,
        index=True,
    )
    family_id: OptionalBigInt = mapped_column(BigInteger)
    primary_email: OptionalStr = mapped_column(Text)
    child_a_zite_record_id: OptionalStr = mapped_column(Text)
    child_a_angel_id: OptionalStr = mapped_column(Text)
    child_a_first_name: OptionalStr = mapped_column(Text)
    child_a_dob: OptionalStr = mapped_column(Text)
    child_b_zite_record_id: OptionalStr = mapped_column(Text)
    child_b_angel_id: OptionalStr = mapped_column(Text)
    child_b_first_name: OptionalStr = mapped_column(Text)
    child_b_dob: OptionalStr = mapped_column(Text)
    gap_months: OptionalInt = mapped_column(Integer)
    gap_days: OptionalInt = mapped_column(Integer)


class EmailCheck(Base):
    """One ZeroBounce result per normalized email per season (§4.1).

    This is the replay entity: the unique (season_id,
    normalized_email) key prevents repeat provider calls.
    """

    __tablename__ = "email_checks"
    __table_args__ = (
        UniqueConstraint(
            "season_id",
            "normalized_email",
            name="uq_email_checks_email",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), nullable=False
    )
    normalized_email: Mapped[str] = mapped_column(Text, nullable=False)
    status: OptionalStr = mapped_column(Text)
    sub_status: OptionalStr = mapped_column(Text)
    did_you_mean: OptionalStr = mapped_column(Text)
    response_json: OptionalStr = mapped_column(Text)
    #: Legacy timestamp string kept verbatim.
    checked_at: OptionalStr = mapped_column(Text)


class EmailCheckTarget(Base):
    """Per-record/field expansion row for one email check (§4.1).

    No unique constraints: repeated targets legitimately exist.
    """

    __tablename__ = "email_check_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_check_id: Mapped[int] = mapped_column(
        ForeignKey("email_checks.id"),
        nullable=False,
        index=True,
    )
    source_sheet: OptionalStr = mapped_column(Text)
    zite_record_id: OptionalStr = mapped_column(Text)
    field: OptionalStr = mapped_column(Text)
    email: OptionalStr = mapped_column(Text)


class AddressCheck(Base):
    """One Nominatim result per distinct query per season (§4.1).

    One representative row per distinct query — current behavior; not
    the email-style per-record expansion.
    """

    __tablename__ = "address_checks"
    __table_args__ = (
        UniqueConstraint("season_id", "query", name="uq_address_checks_query"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), nullable=False
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    status: OptionalStr = mapped_column(Text)
    display_name: OptionalStr = mapped_column(Text)
    lat: OptionalStr = mapped_column(Text)
    lon: OptionalStr = mapped_column(Text)
    response_json: OptionalStr = mapped_column(Text)
    #: Legacy timestamp string kept verbatim.
    checked_at: OptionalStr = mapped_column(Text)


class MatchRun(Base):
    """One match run header (plan §4.1).

    ``archived_from`` snapshots the prior assignment state as JSON;
    run-owned snapshots replace timestamped workbook sheets.
    """

    __tablename__ = "match_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), nullable=False
    )
    created_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    actor: OptionalStr = mapped_column(Text)
    overwrite: OptionalBool = mapped_column(Boolean)
    test_mode: OptionalBool = mapped_column(Boolean)
    config_hash: OptionalStr = mapped_column(Text)
    archived_from: OptionalJson = mapped_column(JSON)


class MatchReportRow(Base):
    """One match-report row owned by a match run (§4.1)."""

    __tablename__ = "match_report_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("match_runs.id"),
        nullable=False,
        index=True,
    )
    kind: OptionalStr = mapped_column(Text)
    ref: OptionalStr = mapped_column(Text)
    reason: OptionalStr = mapped_column(Text)


class MatchAssignmentSnapshot(Base):
    """One archived assignment-snapshot row for a match run (§4.1)."""

    __tablename__ = "match_assignment_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("match_runs.id"),
        nullable=False,
        index=True,
    )
    #: "family" or "sponsor".
    entity: OptionalStr = mapped_column(Text)
    key: OptionalStr = mapped_column(Text)
    assignment_values: OptionalJson = mapped_column(JSON)


class Artifact(Base):
    """Metadata for one generated file artifact (plan §4.1).

    ``path`` is server-relative to the artifact root and never
    client-supplied.
    """

    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_season_kind", "season_id", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), nullable=False
    )
    #: child_sheet / sponsor_packet / stats_report / clarify_draft /
    #: csv_export / other.
    kind: OptionalStr = mapped_column(Text)
    path: OptionalStr = mapped_column(Text)
    filename: OptionalStr = mapped_column(Text)
    content_type: OptionalStr = mapped_column(Text)
    size_bytes: OptionalBigInt = mapped_column(BigInteger)
    sha256: OptionalStr = mapped_column(Text)
    meta: OptionalJson = mapped_column(JSON)
    created_by: OptionalStr = mapped_column(Text)
    created_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))


class Job(Base):
    """One durable job row (plan §6, decision D7).

    ``season_id`` is nullable — reports may be cross-season.  The
    partial unique index on ``idempotency_key`` gives same-key
    replay deduplication.  ``cancel_requested`` plus the
    cancel_requested/cancelled statuses implement cooperative
    cancellation.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_season_status", "season_id", "status"),
        Index(
            "uq_jobs_idempotency_key",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    season_id: OptionalInt = mapped_column(
        ForeignKey("seasons.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'queued'")
    )
    requester: Mapped[str] = mapped_column(Text, nullable=False)
    options: OptionalJson = mapped_column(JSON)
    idempotency_key: OptionalStr = mapped_column(Text)
    request_hash: OptionalStr = mapped_column(Text)
    config_hash: OptionalStr = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    max_attempts: OptionalInt = mapped_column(Integer)
    progress: OptionalJson = mapped_column(JSON)
    leased_until: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    heartbeat_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    error: OptionalJson = mapped_column(JSON)
    result: OptionalJson = mapped_column(JSON)
    created_at: OptionalTzDateTime = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: OptionalTzDateTime = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    finished_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))


class JobEvent(Base):
    """One throttled job progress/lifecycle event (plan §4.1)."""

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id"),
        nullable=False,
        index=True,
    )
    ts: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    #: queued / started / progress / heartbeat / cancel_requested /
    #: cancelled / failed / succeeded.
    kind: OptionalStr = mapped_column(Text)
    data: OptionalJson = mapped_column(JSON)


class User(Base):
    """Staff account (plan §8, decision D1/D2).

    ``roles`` is a JSON list of "read_only"/"auditor"/"coordinator"/
    "admin".  ``dev_no_password`` marks the development-only
    bootstrap accounts; no plaintext secrets are stored.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: OptionalStr = mapped_column(Text)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true()
    )
    roles: OptionalJson = mapped_column(JSON)
    dev_no_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    otp_enrolled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    created_at: OptionalTzDateTime = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: OptionalTzDateTime = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Session(Base):
    """Server-side cookie session; ``id`` is the token hash (§8)."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    csrf_token: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    expires_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    revoked_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))


class SecurityAuditEvent(Base):
    """Login/security activity history.

    Separate from the domain's human ``status == "audited"``
    workflow (plan §4.1 accounts/accountability group).
    """

    __tablename__ = "security_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    user_id: OptionalInt = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    action: OptionalStr = mapped_column(Text)
    object_type: OptionalStr = mapped_column(Text)
    object_id: OptionalStr = mapped_column(Text)
    season: OptionalStr = mapped_column(Text)
    details: OptionalJson = mapped_column(JSON)


class ClarificationFamily(Base):
    """Per-season clarification contact state (decision D5).

    ``contacted_at`` keeps the legacy naive local-time string
    verbatim on import; new writes store UTC ISO text.
    """

    __tablename__ = "clarification_families"

    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), primary_key=True
    )
    family_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    contacted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    contacted_at: OptionalStr = mapped_column(Text)
    replied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'db'")
    )
    updated_by: OptionalStr = mapped_column(Text)
    updated_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))


class ClarificationDraft(Base):
    """One versioned clarification email draft (decision D5).

    ``paragraphs`` is a JSON list of {zite_record_id, angel_id,
    order, text}; ``legacy_positional`` marks imported drafts that
    predate the keyed-paragraph format and must not be rekeyed.
    """

    __tablename__ = "clarification_drafts"
    __table_args__ = (
        UniqueConstraint(
            "season_id",
            "family_id",
            "version",
            name="uq_clarification_drafts_version",
        ),
        Index(
            "ix_clarification_drafts_season_family",
            "season_id",
            "family_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), nullable=False
    )
    family_id: OptionalBigInt = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    subject: OptionalStr = mapped_column(Text)
    greeting: OptionalStr = mapped_column(Text)
    intro: OptionalStr = mapped_column(Text)
    closing: OptionalStr = mapped_column(Text)
    paragraphs: OptionalJson = mapped_column(JSON)
    legacy_positional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    generated_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    generated_by: OptionalStr = mapped_column(Text)
    edited_by: OptionalStr = mapped_column(Text)
    edited_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))


class SponsorDraft(Base):
    """The current editable sponsor packet email (dispatch page).

    One row per ``(season_id, sponsor_id)``: the subject, the generated
    body, the coordinator's plain-text note, the recipient, the BCC list
    and the attachments the body was rendered against.  ``version``
    carries optimistic concurrency for the popup's If-Match updates.

    The row holds no delivery state.  A send attempt owns that, so there
    is one source of truth for "did it go out".
    """

    __tablename__ = "sponsor_drafts"
    __table_args__ = (
        Index("ix_sponsor_drafts_season_sponsor", "season_id", "sponsor_id"),
    )

    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), primary_key=True
    )
    sponsor_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    subject: OptionalStr = mapped_column(Text)
    body_html: OptionalStr = mapped_column(Text)
    note: OptionalStr = mapped_column(Text)
    to_email: OptionalStr = mapped_column(Text)
    #: JSON list of archive addresses copied from the config at draft time.
    bcc_emails: OptionalJson = mapped_column(JSON)
    #: JSON list of {artifact_id, role, filename, content_type,
    #: size_bytes, sha256}.
    attachments: OptionalJson = mapped_column(JSON)
    #: The packet build this draft was rendered from.
    build_id: OptionalStr = mapped_column(Text)
    created_by: OptionalStr = mapped_column(Text)
    created_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    edited_by: OptionalStr = mapped_column(Text)
    edited_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))


class SponsorSendAttempt(Base):
    """One attempt to mail a sponsor packet email (dispatch page).

    The state machine is ``sending`` -> ``sent`` | ``failed`` |
    ``unknown``.  ``failed`` means the provider refused the message and
    a retry is safe; ``unknown`` means the provider's answer does not
    say whether it was accepted, and it is never retried automatically.

    The content snapshot (subject, body, recipients, attachment hashes)
    is written before the send and never rewritten, so the record of
    what left the building survives later edits to the draft.
    """

    __tablename__ = "sponsor_send_attempts"
    __table_args__ = (
        Index(
            "ix_sponsor_send_attempts_season_sponsor",
            "season_id",
            "sponsor_id",
        ),
        Index(
            "uq_sponsor_send_attempts_active",
            "season_id",
            "sponsor_id",
            unique=True,
            sqlite_where=text("state = 'sending'"),
            postgresql_where=text("state = 'sending'"),
        ),
    )

    #: The attempt id (a uuid) doubles as the row's primary key.
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"), nullable=False
    )
    sponsor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    draft_version: OptionalInt = mapped_column(Integer)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'sending'")
    )
    #: The RFC 5322 Message-ID, generated before the send.
    message_id: OptionalStr = mapped_column(Text)
    #: The provider's own id, known only after an accepted send.
    provider_message_id: OptionalStr = mapped_column(Text)
    subject: OptionalStr = mapped_column(Text)
    body_html: OptionalStr = mapped_column(Text)
    to_email: OptionalStr = mapped_column(Text)
    bcc_emails: OptionalJson = mapped_column(JSON)
    attachments: OptionalJson = mapped_column(JSON)
    actor: OptionalStr = mapped_column(Text)
    user_id: OptionalInt = mapped_column(Integer)
    started_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    finished_at: OptionalTzDateTime = mapped_column(DateTime(timezone=True))
    error: OptionalStr = mapped_column(Text)
