"""Admin configuration API tests (``/api/v1/config``).

File-backed SQLite per test; ``httpx`` AsyncClient over ASGITransport via
the :class:`Client` helper in :mod:`test_api`.  Each app is started with
a copy of the real season config redirected into ``tmp_path`` so the
file on disk and the in-memory configuration agree.
"""

import json
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
def setup(tmp_path: Path) -> Setup:
    """One migrated, seeded app started with a tmp config file.

    Args:
        tmp_path: Per-test temporary directory.

    Returns:
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
    return Setup(
        create_app(cfg, config_path=str(config_file)), cfg, config_file
    )


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
    assert tree["acquire"]["sibling_dob_max_gap_days"] == 274


async def test_get_drops_unknown_file_keys(setup: Setup) -> None:
    """A key the contract does not know is dropped, the rest survives.

    Args:
        setup: The migrated app fixture.
    """
    data = json.loads(setup.config_file.read_text())
    data["not_a_real_setting"] = 1
    setup.config_file.write_text(json.dumps(data, indent=2))
    client = Client(setup.app)
    try:
        response = await client.client.get("/api/v1/config")
    finally:
        await client.close()
    assert response.status_code == 200, response.text
    tree = response.json()["config"]
    assert "not_a_real_setting" not in tree
    assert tree["season"] == data["season"]
    assert tree["tables"]["sponsor"]["columns"]["email"] == {"type": "email"}


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
