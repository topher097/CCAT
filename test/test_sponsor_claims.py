"""Tests for the Assigned Angel ID conflict feature.

Two sponsors can claim the same angel id (the Zite "Assigned Angel ID
Code"): a generic Walmart placeholder and the person who signed up to
take its slot.  The screening run detects the contest, the Duplications
section shows it, and the auditor settles it by ignoring the loser.

The file also pins the id invariants the resolution must not break: a
sponsor id is never renumbered, an ignored family or child never loses
its id to another row, and angel letters never skip.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import polars as pl
from conftest import load_test_cfg

from ccat.acquire import import_table
from ccat.audit.store import ENTITY_SPECS
from ccat.config import Config, StorageSpec
from ccat.db.engine import create_async_engine, run_migrations
from ccat.db.uow import UnitOfWork
from ccat.match import reconcile_pre_assignments
from ccat.preparation import detect_sponsor_claim_contests, prepare_rows
from ccat.services import db_duplications

# ---------------------------------------------------------------------------
# Fixtures and helpers.
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    """Test config: temp DB and artifacts."""
    base = load_test_cfg(
        excel_file=str(tmp_path / "wb.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
    )
    return base.model_copy(
        update={
            "storage": StorageSpec(
                mode="db",
                database_url=f"sqlite+aiosqlite:///{tmp_path / 'ccat.sqlite3'}",
                artifact_root=str(tmp_path / "artifacts"),
            )
        }
    )


def _columns(entity: str) -> tuple[str, ...]:
    """Return the entity's legacy dict column vocabulary."""
    return tuple(ENTITY_SPECS[entity]["columns"])


def child_row(record_id: str, **overrides: Any) -> dict[str, Any]:
    """One full final-child row under legacy dict names."""
    base: dict[str, Any] = {column: None for column in _columns("child")}
    base.update(
        zite_record_id=record_id,
        family_id=1,
        angel_id="1A",
        first_name="Kiddo",
        primary_email="fam@example.com",
        dob="2018-05-10",
        age_at_xmas=8,
        gender="Male",
        source="web",
        status="audited",
        ignore=False,
        screened=True,
        potential_duplicate=False,
        generated_info_sheet=False,
    )
    base.update(overrides)
    return base


def family_row(family_id: int, **overrides: Any) -> dict[str, Any]:
    """One full final-family row under legacy dict names."""
    base: dict[str, Any] = {column: None for column in _columns("family")}
    base.update(
        family_id=family_id,
        zite_record_id=f"rec_f{family_id}",
        primary_email=f"fam{family_id}@example.com",
        first_name="Jane",
        last_name="Doe",
        city="Hancock",
        source="web",
        status="audited",
        ignore=False,
        potential_duplicate=False,
        num_angels_in_family=1,
    )
    base.update(overrides)
    return base


def sponsor_row(sponsor_id: int, **overrides: Any) -> dict[str, Any]:
    """One full final-sponsor row under legacy dict names."""
    base: dict[str, Any] = {column: None for column in _columns("sponsor")}
    base.update(
        sponsor_id=sponsor_id,
        zite_record_id=f"rec_s{sponsor_id}",
        create="2026-01-01T10:00:00Z",
        email=f"sponsor{sponsor_id}@example.com",
        sponsor_type="Individual",
        first_name="Mary",
        last_name="Sponsor",
        num_children=1,
        age_preference="Any Age",
        source="web",
        status="audited",
        ignore=False,
        potential_duplicate=False,
    )
    base.update(overrides)
    return base


def raw_sponsor(record_id: str, **overrides: Any) -> dict[str, Any]:
    """One verbatim raw-sponsor row."""
    base = {
        "zite_record_id": record_id,
        "create": "2026-01-01T10:00:00Z",
        "email": "sponsor1@example.com",
        "sponsor_type": "Individual",
        "first_name": "Mary",
        "last_name": "Sponsor",
        "num_children": 1,
        "age_preference": "Any Age",
        "source": "web",
    }
    base.update(overrides)
    return base


def claim_log_row(
    angel_id: str, record_id: str, record: dict[str, Any]
) -> dict[str, Any]:
    """One duplicate-log row for a sponsor angel-claim contest."""
    return {
        "sheet": "sponsor_angel_claims",
        "group_key": angel_id,
        "zite_record_id": record_id,
        "created": record.get("create"),
        "survivor": record.get("create") == "2026-02-01T10:00:00Z",
        "conflict_columns": "prior_assigned_angel_ids",
        "conflict_details": (
            f"prior_assigned_angel_ids: angel {angel_id} claimed by"
            " sponsors ['rec_s1', 'rec_s2']"
        ),
        "record": json.dumps(record, default=str, sort_keys=True),
    }


async def _seed_contest(repo: Any) -> None:
    """Seed one generic-Walmart contest over the angel id ``1A``.

    Sponsor 1 is the generic Walmart placeholder, sponsor 2 the person
    who claimed the same angel id.
    """
    placeholder = raw_sponsor(
        "rec_s1",
        email="walmart@example.com",
        sponsor_type="Walmart",
        organization_name="Walmart",
        prior_assigned_angel_ids="1A",
        create="2026-01-01T10:00:00Z",
    )
    claimant = raw_sponsor(
        "rec_s2",
        email="claimant@example.com",
        sponsor_type="Individual",
        prior_assigned_angel_ids="1A",
        create="2026-02-01T10:00:00Z",
    )
    await repo.replace_raw_snapshot(
        "migrate", {"sponsor": [placeholder, claimant]}, "migration"
    )
    await repo.commit_finals("migrate", "child", [child_row("c1")], "migration")
    await repo.commit_finals("migrate", "family", [family_row(1)], "migration")
    await repo.commit_finals(
        "migrate",
        "sponsor",
        [
            sponsor_row(
                1,
                email="walmart@example.com",
                sponsor_type="Walmart",
                organization_name="Walmart",
                prior_assigned_angel_ids="1A",
                create="2026-01-01T10:00:00Z",
            ),
            sponsor_row(
                2,
                email="claimant@example.com",
                sponsor_type="Individual",
                prior_assigned_angel_ids="1A",
                create="2026-02-01T10:00:00Z",
            ),
        ],
        "migration",
    )
    await repo.replace_screen_diagnostics(
        [
            claim_log_row("1A", "rec_s1", placeholder),
            claim_log_row("1A", "rec_s2", claimant),
        ],
        [],
        "screen",
    )


def _run(coro: Any) -> Any:
    """Run one coroutine on a fresh event loop."""
    return asyncio.run(coro)


def _store(cfg: Config) -> Any:
    """Return a unit of work for the test database."""
    return UnitOfWork(create_async_engine(cfg.storage.database_url))


async def _sponsors(repo: Any) -> dict[int, dict[str, Any]]:
    """Return the final sponsor rows, keyed by sponsor id."""
    return {row["sponsor_id"]: row for row in await repo.final_rows("sponsor")}


# ---------------------------------------------------------------------------
# Detection.
# ---------------------------------------------------------------------------


def test_detection_reports_one_contest_per_claimed_angel() -> None:
    """Two sponsors claiming one angel make one contest group."""
    sponsors = [
        {
            "zite_record_id": "s1",
            "email": "walmart@example.com",
            "prior_assigned_angel_ids": "1A",
            "create": "2026-01-01T10:00:00Z",
        },
        {
            "zite_record_id": "s2",
            "email": "real@example.com",
            "prior_assigned_angel_ids": "1A",
            "create": "2026-02-01T10:00:00Z",
        },
    ]
    rows = detect_sponsor_claim_contests(sponsors, [{"angel_id": "1A"}])
    assert {row["group_key"] for row in rows} == {"1A"}
    assert {row["sheet"] for row in rows} == {"sponsor_angel_claims"}
    assert {row["zite_record_id"] for row in rows} == {"s1", "s2"}
    assert all(
        row["conflict_columns"] == "prior_assigned_angel_ids" for row in rows
    )


def test_detection_suggests_the_later_submission_as_survivor() -> None:
    """The generic placeholder was created first, so it is not suggested."""
    sponsors = [
        {
            "zite_record_id": "s1",
            "email": "walmart@example.com",
            "prior_assigned_angel_ids": "1A",
            "create": "2026-01-01T10:00:00Z",
        },
        {
            "zite_record_id": "s2",
            "email": "real@example.com",
            "prior_assigned_angel_ids": "1A",
            "create": "2026-02-01T10:00:00Z",
        },
    ]
    rows = detect_sponsor_claim_contests(sponsors, [{"angel_id": "1A"}])
    survivors = {row["zite_record_id"] for row in rows if row["survivor"]}
    assert survivors == {"s2"}


def test_detection_ignores_a_claim_nobody_shares() -> None:
    """One sponsor claiming an angel is not a contest."""
    sponsors = [
        {
            "zite_record_id": "s1",
            "email": "solo@example.com",
            "prior_assigned_angel_ids": "1A",
            "create": "2026-01-01T10:00:00Z",
        },
    ]
    assert detect_sponsor_claim_contests(sponsors, [{"angel_id": "1A"}]) == []


def test_detection_ignores_a_claim_on_an_unknown_angel() -> None:
    """Two sponsors claiming an angel no child has is not a contest."""
    sponsors = [
        {
            "zite_record_id": "s1",
            "email": "one@example.com",
            "prior_assigned_angel_ids": "9Z",
            "create": "2026-01-01T10:00:00Z",
        },
        {
            "zite_record_id": "s2",
            "email": "two@example.com",
            "prior_assigned_angel_ids": "9Z",
            "create": "2026-02-01T10:00:00Z",
        },
    ]
    assert detect_sponsor_claim_contests(sponsors, [{"angel_id": "1A"}]) == []


def test_detection_leaves_same_email_claimants_to_the_email_group() -> None:
    """The same person twice is one email duplicate, not an angel contest."""
    sponsors = [
        {
            "zite_record_id": "s1",
            "email": "same@example.com",
            "prior_assigned_angel_ids": "1A",
            "create": "2026-01-01T10:00:00Z",
        },
        {
            "zite_record_id": "s2",
            "email": "same@example.com",
            "prior_assigned_angel_ids": "1A",
            "create": "2026-02-01T10:00:00Z",
        },
    ]
    assert detect_sponsor_claim_contests(sponsors, [{"angel_id": "1A"}]) == []


def test_detection_reads_every_claimed_angel_in_the_list() -> None:
    """A sponsor may claim several angels; each contested one is a group."""
    sponsors = [
        {
            "zite_record_id": "s1",
            "email": "one@example.com",
            "prior_assigned_angel_ids": "1A, 1B",
            "create": "2026-01-01T10:00:00Z",
        },
        {
            "zite_record_id": "s2",
            "email": "two@example.com",
            "prior_assigned_angel_ids": "1b 2A",
            "create": "2026-02-01T10:00:00Z",
        },
    ]
    rows = detect_sponsor_claim_contests(
        sponsors, [{"angel_id": "1A"}, {"angel_id": "1B"}, {"angel_id": "2A"}]
    )
    # "1A" and "2A" are claimed once each; only "1B" is contested.
    assert {row["group_key"] for row in rows} == {"1B"}


def test_detection_groups_three_claimants_together() -> None:
    """Three sponsors claiming one angel make one group of three."""
    sponsors = [
        {
            "zite_record_id": f"s{index}",
            "email": f"one{index}@example.com",
            "prior_assigned_angel_ids": "1A",
            "create": f"2026-01-0{index}T10:00:00Z",
        }
        for index in (1, 2, 3)
    ]
    rows = detect_sponsor_claim_contests(sponsors, [{"angel_id": "1A"}])
    assert len(rows) == 3
    assert {row["group_key"] for row in rows} == {"1A"}


# ---------------------------------------------------------------------------
# The whole path, from the Zite column to the duplicate log.
# ---------------------------------------------------------------------------

#: One Zite sponsor row, with the live form's column labels.
ZITE_SPONSOR = {
    "id": "rec_s1",
    "createdAt": "2026-01-01T10:00:00Z",
    "updatedAt": "2026-01-03T10:00:00Z",
    "Email": "sponsor@example.com",
    "I Accept Data Disclaimer": True,
    "Affiliation": "Individual",
    "Organization Name": None,
    "Assigned Angel ID Code": None,
    "First Name": "mary",
    "Last Name": "sponsor",
    "Phone Number": "1 (906) 555-1234",
    "Number of Angles to Sponsor": "1",
    "Age Preference": None,
    "Gender Preference": "Any Gender",
    "Gift Drop-Off Location Preference": "Hancock",
    "Source": "web",
    "Updated time": "2026-01-02T10:00:00Z",
}


def test_the_zite_column_reaches_the_duplicate_log(cfg: Config) -> None:
    """The live form's "Assigned Angel ID Code" becomes the contest."""
    frame = pl.DataFrame(
        [
            {
                **ZITE_SPONSOR,
                "id": "rec_s1",
                "Email": "walmart@example.com",
                "Affiliation": "Walmart",
                "Assigned Angel ID Code": "1A",
            },
            {
                **ZITE_SPONSOR,
                "id": "rec_s2",
                "createdAt": "2026-02-01T10:00:00Z",
                "Email": "claimant@example.com",
                "Assigned Angel ID Code": "1A",
            },
        ]
    )
    raw_sponsors = import_table(frame, "sponsor", cfg)
    assert [row["prior_assigned_angel_ids"] for row in raw_sponsors] == [
        "1A",
        "1A",
    ]
    raw = {
        "sponsor": raw_sponsors,
        "family": [
            {
                "zite_record_id": "rec_f1",
                "zite_created_at": "2026-01-05T10:00:00Z",
                "created": "2026-01-05T10:00:00Z",
                "primary_email": "fam@example.com",
                "first_name": "jane",
            }
        ],
        "child": [
            {
                "zite_record_id": "c1",
                "zite_created_at": "2026-02-01T09:00:00+00:00",
                "created": "2026-02-01T09:00:00+00:00",
                "first_name": "kiddo",
                "primary_email": "fam@example.com",
                "dob": "2018-05-10",
            }
        ],
    }
    preparation = prepare_rows(raw, cfg)
    contests = [
        row
        for row in preparation.duplicate_log
        if row["sheet"] == "sponsor_angel_claims"
    ]
    assert {row["group_key"] for row in contests} == {"1A"}
    assert {row["zite_record_id"] for row in contests} == {"rec_s1", "rec_s2"}
    # The record is what the detail page shows for each claimant.
    record = json.loads(contests[0]["record"])
    assert record["prior_assigned_angel_ids"] == "1A"


# ---------------------------------------------------------------------------
# The Duplications section.
# ---------------------------------------------------------------------------


def test_contest_reaches_the_duplications_list(tmp_path: Path) -> None:
    """The contest is listed as a sponsor group of its own kind."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_contest(repo)
                groups = await db_duplications.list_groups(repo)
            assert len(groups) == 1
            group = groups[0]
            assert group["kind"] == "angel_claim_conflict"
            assert group["entity"] == "sponsor"
            assert group["sheet"] == "sponsor_angel_claims"
            assert group["group_key"] == "1A"
            assert group["member_count"] == 2
            assert group["status"] == "pending"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_contest_detail_carries_its_kind(tmp_path: Path) -> None:
    """The group detail names the kind and the contested angel id."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_contest(repo)
                groups = await db_duplications.list_groups(repo)
                detail = await db_duplications.group_detail(
                    repo, groups[0]["id"]
                )
            assert detail is not None
            assert detail["kind"] == "angel_claim_conflict"
            assert detail["group_key"] == "1A"
            assert [
                member["zite_record_id"] for member in detail["members"]
            ] == [
                "rec_s1",
                "rec_s2",
            ]
        finally:
            await uow.engine.dispose()

    _run(main())


def test_kind_filter_selects_the_contest(tmp_path: Path) -> None:
    """The kind filter picks the contests and leaves the rest out."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_contest(repo)
                groups = await db_duplications.list_groups(repo)
                contests = db_duplications.filter_groups(
                    groups, kind="angel_claim_conflict"
                )
                others = db_duplications.filter_groups(
                    groups, kind="duplicate_submission"
                )
            assert [group["group_key"] for group in contests] == ["1A"]
            assert others == []
        finally:
            await uow.engine.dispose()

    _run(main())


def test_kind_filter_keeps_the_neighbours_inside_the_filter(
    tmp_path: Path,
) -> None:
    """Next and previous stay inside the filtered list."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_contest(repo)
                groups = await db_duplications.list_groups(repo)
                previous, following = await db_duplications.group_neighbours(
                    repo, groups[0]["id"], kind="duplicate_submission"
                )
            assert previous is None
            assert following is None
        finally:
            await uow.engine.dispose()

    _run(main())


def test_ignoring_the_loser_hands_the_angel_to_the_survivor(
    tmp_path: Path,
) -> None:
    """Ignoring the generic placeholder leaves the claimant the only claim."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_contest(repo)
                groups = await db_duplications.list_groups(repo)
                await db_duplications.decide(
                    repo,
                    cfg,
                    groups[0]["id"],
                    "rec_s1",
                    "ignore",
                    "auditor",
                    confirm=True,
                )
                sponsors = await _sponsors(repo)
                pool = [
                    row for row in sponsors.values() if not row.get("ignore")
                ]
                plans, _ = reconcile_pre_assignments(pool, [child_row("c1")])
            assert sponsors[1]["ignore"] is True
            assert sponsors[1]["sponsor_id"] == 1
            assert sponsors[2]["ignore"] is False
            assert sponsors[2]["prior_assigned_angel_ids"] == "1A"
            assert list(plans) == [2]
            assert plans[2].assigned_angel_ids == ["1A"]
        finally:
            await uow.engine.dispose()

    _run(main())


def test_ignoring_the_loser_never_renumbers_a_sponsor(tmp_path: Path) -> None:
    """The loser's sponsor id survives the merge untouched."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_contest(repo)
                groups = await db_duplications.list_groups(repo)
                await db_duplications.decide(
                    repo,
                    cfg,
                    groups[0]["id"],
                    "rec_s1",
                    "ignore",
                    "auditor",
                    confirm=True,
                )
                sponsors = await _sponsors(repo)
            assert sorted(sponsors) == [1, 2]
            assert sponsors[1]["zite_record_id"] == "rec_s1"
            assert sponsors[2]["zite_record_id"] == "rec_s2"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_marking_the_contest_audited_keeps_both_sponsor_ids(
    tmp_path: Path,
) -> None:
    """Auditing the group leaves both rows, and both ids, in place."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_contest(repo)
                groups = await db_duplications.list_groups(repo)
                await db_duplications.decide(
                    repo,
                    cfg,
                    groups[0]["id"],
                    "rec_s1",
                    "ignore",
                    "auditor",
                    confirm=True,
                )
                detail = await db_duplications.mark_audited(
                    repo, cfg, groups[0]["id"], "auditor"
                )
                sponsors = await _sponsors(repo)
            assert detail["status"] == "audited"
            assert sorted(sponsors) == [1, 2]
            assert sponsors[1]["ignore"] is True
            assert sponsors[2]["ignore"] is False
        finally:
            await uow.engine.dispose()

    _run(main())


def test_releasing_a_contest_member_never_renumbers_a_sponsor(
    tmp_path: Path,
) -> None:
    """The other decision clears the flag and keeps both ids as they are."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_contest(repo)
                groups = await db_duplications.list_groups(repo)
                await db_duplications.decide(
                    repo,
                    cfg,
                    groups[0]["id"],
                    "rec_s1",
                    "not_duplicate",
                    "auditor",
                    confirm=True,
                )
                sponsors = await _sponsors(repo)
                trail = await repo.duplication_audit_rows()
            assert sorted(sponsors) == [1, 2]
            assert sponsors[1]["sponsor_id"] == 1
            assert sponsors[1]["zite_record_id"] == "rec_s1"
            assert sponsors[2]["sponsor_id"] == 2
            assert trail[0]["assigned_id"] == "1"
        finally:
            await uow.engine.dispose()

    _run(main())


# ---------------------------------------------------------------------------
# The id invariants the resolution must not break.
# ---------------------------------------------------------------------------


def test_an_ignored_family_never_loses_its_family_id(tmp_path: Path) -> None:
    """A released family submission never takes an ignored family's id."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await repo.replace_raw_snapshot(
                    "migrate",
                    {
                        "family": [
                            {
                                "zite_record_id": "rec_f1",
                                "created": "2026-01-05T10:00:00Z",
                                "primary_email": "fam@example.com",
                                "first_name": "Jane",
                                "last_name": "Doe",
                            }
                        ]
                    },
                    "migration",
                )
                await repo.commit_finals(
                    "migrate",
                    "family",
                    [
                        family_row(1, zite_record_id="rec_f1"),
                        family_row(
                            2,
                            zite_record_id="rec_f2",
                            primary_email="other@example.com",
                            ignore=True,
                            status="ignored",
                        ),
                    ],
                    "migration",
                )
                await repo.replace_screen_diagnostics(
                    [
                        {
                            "sheet": "family_submissions",
                            "group_key": "fam@example.com",
                            "zite_record_id": "rec_f1",
                            "created": "2026-01-05T10:00:00Z",
                            "survivor": True,
                            "conflict_columns": "city",
                            "conflict_details": "city: survivor 'Hancock' vs []",
                            "record": json.dumps(
                                {
                                    "zite_record_id": "rec_f1",
                                    "created": "2026-01-05T10:00:00Z",
                                    "primary_email": "fam@example.com",
                                    "first_name": "Jane",
                                },
                                sort_keys=True,
                            ),
                        }
                    ],
                    [],
                    "screen",
                )
                groups = await db_duplications.list_groups(repo)
                await db_duplications.decide(
                    repo,
                    cfg,
                    groups[0]["id"],
                    "rec_f1",
                    "not_duplicate",
                    "auditor",
                    confirm=True,
                )
                families = {
                    row["family_id"]: row
                    for row in await repo.final_rows("family")
                }
            assert sorted(families) == [1, 2, 3]
            assert families[2]["ignore"] is True
            assert families[2]["zite_record_id"] == "rec_f2"
            assert families[3]["zite_record_id"] == "rec_f1"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_an_ignored_child_never_loses_its_angel_id(tmp_path: Path) -> None:
    """A released duplicate never takes the letter an ignored child holds."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await repo.commit_finals(
                    "migrate",
                    "child",
                    [
                        child_row(
                            "c1", angel_id="1A", potential_duplicate=True
                        ),
                        child_row(
                            "c1b", angel_id="1A", potential_duplicate=True
                        ),
                        child_row(
                            "c_ignored",
                            angel_id="1B",
                            ignore=True,
                            status="ignored",
                        ),
                    ],
                    "migration",
                )
                await repo.commit_finals(
                    "migrate", "family", [family_row(1)], "migration"
                )
                await repo.replace_screen_diagnostics(
                    [
                        {
                            "sheet": "child_submissions",
                            "group_key": "fam@example.com|Kiddo",
                            "zite_record_id": "c1",
                            "created": "2026-02-01T09:00:00+00:00",
                            "survivor": False,
                            "conflict_columns": "dob",
                            "conflict_details": "dob: survivor 'x' vs ['y']",
                            "record": json.dumps(
                                {"zite_record_id": "c1"}, sort_keys=True
                            ),
                        },
                        {
                            "sheet": "child_submissions",
                            "group_key": "fam@example.com|Kiddo",
                            "zite_record_id": "c1b",
                            "created": "2026-02-02T09:00:00+00:00",
                            "survivor": True,
                            "conflict_columns": "dob",
                            "conflict_details": "dob: survivor 'x' vs ['y']",
                            "record": json.dumps(
                                {"zite_record_id": "c1b"}, sort_keys=True
                            ),
                        },
                    ],
                    [],
                    "screen",
                )
                groups = await db_duplications.list_groups(repo)
                await db_duplications.decide(
                    repo,
                    cfg,
                    groups[0]["id"],
                    "c1",
                    "not_duplicate",
                    "auditor",
                    confirm=True,
                )
                children = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
            assert children["c1"]["angel_id"] == "1C"
            assert children["c_ignored"]["angel_id"] == "1B"
            assert children["c_ignored"]["ignore"] is True
            assert children["c1b"]["angel_id"] == "1A"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_released_angel_letters_do_not_skip(tmp_path: Path) -> None:
    """A family's letters read A, B, C; a released member fills the next."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await repo.commit_finals(
                    "migrate",
                    "child",
                    [
                        child_row(
                            "c1", angel_id="1A", potential_duplicate=True
                        ),
                        child_row(
                            "c1b", angel_id="1A", potential_duplicate=True
                        ),
                        child_row(
                            "c2", angel_id="1B", potential_duplicate=False
                        ),
                    ],
                    "migration",
                )
                await repo.commit_finals(
                    "migrate", "family", [family_row(1)], "migration"
                )
                await repo.replace_screen_diagnostics(
                    [
                        {
                            "sheet": "child_submissions",
                            "group_key": "fam@example.com|Kiddo",
                            "zite_record_id": "c1",
                            "created": "2026-02-01T09:00:00+00:00",
                            "survivor": False,
                            "conflict_columns": "dob",
                            "conflict_details": "dob: survivor 'x' vs ['y']",
                            "record": json.dumps(
                                {"zite_record_id": "c1"}, sort_keys=True
                            ),
                        },
                        {
                            "sheet": "child_submissions",
                            "group_key": "fam@example.com|Kiddo",
                            "zite_record_id": "c1b",
                            "created": "2026-02-02T09:00:00+00:00",
                            "survivor": True,
                            "conflict_columns": "dob",
                            "conflict_details": "dob: survivor 'x' vs ['y']",
                            "record": json.dumps(
                                {"zite_record_id": "c1b"}, sort_keys=True
                            ),
                        },
                    ],
                    [],
                    "screen",
                )
                groups = await db_duplications.list_groups(repo)
                await db_duplications.decide(
                    repo,
                    cfg,
                    groups[0]["id"],
                    "c1",
                    "not_duplicate",
                    "auditor",
                    confirm=True,
                )
                children = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
            letters = sorted(
                {
                    row["angel_id"]
                    for row in children.values()
                    if row["angel_id"]
                }
            )
            assert letters == ["1A", "1B", "1C"]
            assert children["c1"]["angel_id"] == "1C"
        finally:
            await uow.engine.dispose()

    _run(main())
