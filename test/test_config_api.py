"""Admin configuration API tests (``/api/v1/config``).

File-backed SQLite per test; ``httpx`` AsyncClient over ASGITransport via
the :class:`Client` helper in :mod:`test_api`.  Each app is started with
a copy of the real season config redirected into ``tmp_path`` so the
file on disk and the in-memory configuration agree.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_api import Client

from ccat.api import create_app
from ccat.config import Config, load_config
from ccat.db.engine import create_sync_engine, run_migrations
from ccat.db.seed import seed_bootstrap

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "ccat_2026.json"


@dataclass
class Setup:
    """One migrated app and the configuration file it was started with."""

    app: Any
    cfg: Config
    config_file: Path


def _config_tree(tmp_path: Path) -> dict[str, Any]:
    """Copy the real season config and redirect every path into tmp_path.

    Args:
        tmp_path: Per-test temporary directory.

    Returns:
        The editable raw configuration mapping.
    """
    data: dict[str, Any] = json.loads(CONFIG_PATH.read_text())
    data["storage"] = {
        "mode": "db",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'ccat.sqlite3'}",
        "artifact_root": str(tmp_path / "artifacts"),
    }
    data["excel_file"] = str(tmp_path / "wb.xlsx")
    data["log_file"] = str(tmp_path / "ccat.log")
    data["match_report_csv"] = str(tmp_path / "report.csv")
    return data


@pytest.fixture
async def setup(tmp_path: Path) -> AsyncIterator[Setup]:
    """One migrated, seeded app started with a tmp config file.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The app, its validated config, and the config file path.
    """
    data = _config_tree(tmp_path)
    config_file = tmp_path / "ccat.json"
    config_file.write_text(json.dumps(data, indent=2))
    cfg = load_config(config_file)
    run_migrations(cfg.storage.database_url)
    seed_bootstrap(
        create_sync_engine(
            cfg.storage.database_url.replace("sqlite+aiosqlite", "sqlite")
        )
    )
    application = create_app(cfg, config_path=str(config_file))
    try:
        yield Setup(application, cfg, config_file)
    finally:
        # End the aiosqlite worker thread with the test's event loop.
        await application.state.uow.engine.dispose()


async def test_get_returns_effective_tree_and_path(setup: Setup) -> None:
    """GET merges the file's values over the model defaults.

    Args:
        setup: The migrated app fixture.
    """
    client = Client(setup.app)
    try:
        response = await client.client.get("/api/v1/config")
    finally:
        await client.close()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["path"] == str(setup.config_file)
    tree = body["config"]
    on_disk = json.loads(setup.config_file.read_text())
    # the file's values win, returned verbatim
    assert tree["season"] == on_disk["season"]
    assert tree["xmas_date"] == on_disk["xmas_date"]
    assert tree["vocabularies"] == on_disk["vocabularies"]
    assert tree["tables"]["sponsor"]["columns"]["email"] == {"type": "email"}
    # settings the file omits are filled in from the defaults
    assert tree["api"]["port"] == 8650
    assert tree["storage"]["mode"] == "db"
    assert tree["worker"]["max_attempts"] == 3
    assert tree["report"]["timezone"] == "local"
    assert tree["email_check"]["workers"] == 4
    assert tree["screening"]["sibling_dob_max_gap_days"] == 274
    # the acquire-stage DOB check is gone; screening owns it
    assert "acquire" not in tree


async def test_get_forbidden_for_non_admin(setup: Setup) -> None:
    """A coordinator/auditor account cannot read the config.

    Args:
        setup: The migrated app fixture.
    """
    client = Client(setup.app)
    await client.login("lmboswell")
    try:
        response = await client.client.get("/api/v1/config")
    finally:
        await client.close()
    assert response.status_code == 403, response.text


async def test_put_valid_tree_persists_and_reloads(setup: Setup) -> None:
    """A valid edit is written to disk and reloaded into the app.

    Args:
        setup: The migrated app fixture.
    """
    client = Client(setup.app)
    await client.login("cmendres")
    try:
        current = await client.client.get("/api/v1/config")
        edited = current.json()["config"]
        edited["report"]["filename_prefix"] = "season_report"
        response = await client.client.put(
            "/api/v1/config", json=edited, headers=client.headers()
        )
    finally:
        await client.close()
    assert response.status_code == 200, response.text
    assert response.json()["config"] == edited
    on_disk = json.loads(setup.config_file.read_text())
    assert on_disk["report"]["filename_prefix"] == "season_report"
    assert setup.app.state.cfg.report.filename_prefix == "season_report"


async def test_put_invalid_tree_is_422_and_leaves_file(setup: Setup) -> None:
    """A contract violation is 422 and never touches the file.

    Args:
        setup: The migrated app fixture.
    """
    before = setup.config_file.read_bytes()
    invalid = json.loads(before)
    invalid["api"] = {"port": 99999}
    client = Client(setup.app)
    await client.login("cmendres")
    try:
        response = await client.client.put(
            "/api/v1/config", json=invalid, headers=client.headers()
        )
    finally:
        await client.close()
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_config"
    assert detail["errors"]
    assert all({"path", "message"} <= set(entry) for entry in detail["errors"])
    assert any(entry["path"] == "api.port" for entry in detail["errors"])
    assert setup.config_file.read_bytes() == before


async def test_put_without_csrf_header_is_403(setup: Setup) -> None:
    """A real session without the CSRF header is refused.

    Args:
        setup: The migrated app fixture.
    """
    edited = json.loads(setup.config_file.read_text())
    client = Client(setup.app)
    await client.login("cmendres")
    try:
        response = await client.client.put(
            "/api/v1/config", json=edited, headers={}
        )
    finally:
        await client.close()
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "csrf"


async def test_put_database_url_swaps_engine(
    setup: Setup, tmp_path: Path
) -> None:
    """A changed database URL rebuilds the app's engine.

    Args:
        setup: The migrated app fixture.
        tmp_path: Per-test temporary directory.
    """
    new_url = f"sqlite+aiosqlite:///{tmp_path / 'other.sqlite3'}"
    edited = json.loads(setup.config_file.read_text())
    edited["storage"]["database_url"] = new_url
    client = Client(setup.app)
    await client.login("cmendres")
    try:
        response = await client.client.put(
            "/api/v1/config", json=edited, headers=client.headers()
        )
    finally:
        await client.close()
    assert response.status_code == 200, response.text
    assert str(setup.app.state.uow.engine.url) == new_url
    assert setup.app.state.cfg.storage.database_url == new_url


async def test_get_without_config_path_is_500(setup: Setup) -> None:
    """An app started without a config path reports it as unavailable.

    Args:
        setup: The migrated app fixture.
    """
    app = create_app(setup.cfg)
    client = Client(app)
    try:
        response = await client.client.get("/api/v1/config")
    finally:
        await client.close()
    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "config_unavailable"


async def test_run_settings_get_is_seeded_for_coordinators(
    setup: Setup,
) -> None:
    """GET returns the effective flags, weights, and editable flag.

    Args:
        setup: The migrated app fixture.
    """
    client = Client(setup.app)
    await client.login("lmboswell")
    try:
        match = await client.client.get("/api/v1/runs/match/settings")
        generate = await client.client.get("/api/v1/runs/generate/settings")
    finally:
        await client.close()
    assert match.status_code == 200, match.text
    body = match.json()
    assert body["command"] == "match"
    assert body["run"] == {"test": False, "skip_match": False}
    assert body["weights"] == {
        "location_weight": 1000,
        "age_weight": 100,
        "gender_weight": 1,
    }
    assert body["config_path"] == str(setup.config_file)
    assert body["editable"] is True
    assert "weights" not in generate.json()


async def test_run_settings_patch_persists_only_the_section(
    setup: Setup,
) -> None:
    """A coordinator's PATCH writes the flags and reloads the app.

    Args:
        setup: The migrated app fixture.
    """
    client = Client(setup.app)
    await client.login("lmboswell")
    try:
        response = await client.client.patch(
            "/api/v1/runs/generate/settings",
            json={"test": True, "overwrite": True, "max_workers": 2},
            headers=client.headers(),
        )
    finally:
        await client.close()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved"] is True
    assert body["run"] == {
        "test": True,
        "overwrite": True,
        "max_workers": 2,
    }
    on_disk = json.loads(setup.config_file.read_text())
    assert on_disk["generate"]["run"] == {
        "test": True,
        "overwrite": True,
        "max_workers": 2,
    }
    assert on_disk["matchmaking"]["run"] == {
        "test": False,
        "skip_match": False,
    }
    assert setup.app.state.cfg.generate.run.max_workers == 2


async def test_screen_run_settings_get_returns_the_ai_flag(
    setup: Setup,
) -> None:
    """GET returns the global AI screening flag and the editable flag.

    Args:
        setup: The migrated app fixture.
    """
    client = Client(setup.app)
    await client.login("lmboswell")
    try:
        response = await client.client.get("/api/v1/runs/screen/settings")
    finally:
        await client.close()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["command"] == "screen"
    assert body["run"] == {"overwrite_new": False}
    assert body["editable"] is True


async def test_screen_run_settings_patch_writes_only_the_flag(
    setup: Setup,
) -> None:
    """A coordinator's PATCH writes the flag and keeps the rest of the section.

    Args:
        setup: The migrated app fixture.
    """
    client = Client(setup.app)
    await client.login("lmboswell")
    try:
        response = await client.client.patch(
            "/api/v1/runs/screen/settings",
            json={"overwrite_new": True},
            headers=client.headers(),
        )
    finally:
        await client.close()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved"] is True
    assert body["run"] == {"overwrite_new": True}
    on_disk = json.loads(setup.config_file.read_text())
    assert on_disk["ai_screening"]["overwrite_new"] is True
    # The rest of the section survives the write.
    assert on_disk["ai_screening"]["model"] == setup.cfg.ai_screening.model
    assert (
        on_disk["ai_screening"]["system_prompt"]
        == setup.cfg.ai_screening.system_prompt
    )
    assert setup.app.state.cfg.ai_screening.overwrite_new is True


async def test_screen_run_settings_patch_rejects_extra_keys(
    setup: Setup,
) -> None:
    """A key outside the flag is 422 and the file stays untouched.

    Args:
        setup: The migrated app fixture.
    """
    before = setup.config_file.read_bytes()
    client = Client(setup.app)
    await client.login("lmboswell")
    try:
        response = await client.client.patch(
            "/api/v1/runs/screen/settings",
            json={"overwrite_new": True, "model": "other/model"},
            headers=client.headers(),
        )
    finally:
        await client.close()
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "invalid_input"
    assert setup.config_file.read_bytes() == before


async def test_run_settings_patch_rejects_extra_keys_and_bad_trees(
    setup: Setup,
) -> None:
    """A key outside the flags is 422, and so is an impossible tree.

    Args:
        setup: The migrated app fixture.
    """
    before = setup.config_file.read_bytes()
    client = Client(setup.app)
    await client.login("lmboswell")
    try:
        extra = await client.client.patch(
            "/api/v1/runs/match/settings",
            json={"test": False, "location_weight": 5},
            headers=client.headers(),
        )
        impossible = await client.client.patch(
            "/api/v1/runs/match/settings",
            json={"test": True, "skip_match": True},
            headers=client.headers(),
        )
    finally:
        await client.close()
    assert extra.status_code == 422, extra.text
    assert extra.json()["detail"]["code"] == "invalid_input"
    assert impossible.status_code == 422, impossible.text
    assert impossible.json()["detail"]["code"] == "invalid_config"
    assert setup.config_file.read_bytes() == before
