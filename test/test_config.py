"""Tests for config loading and validation (§3)."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from ccat.config import (
    ApiSpec,
    ColumnSpec,
    Config,
    ConfigError,
    ReportSpec,
    StorageSpec,
    WorkerSpec,
    load_config,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "ccat_2026.json"


def test_load_real_config() -> None:
    """The checked-in season config loads and validates."""
    config = load_config(CONFIG_PATH)
    assert config.season == "2026"
    assert config.xmas.isoformat() == "2026-12-25"
    assert set(config.tables) >= {"sponsor", "family", "child"}
    assert config.matchmaking.location_weight > config.matchmaking.age_weight
    assert config.acquire.sibling_dob_max_gap_days == 274


def test_acquire_spec_gap_days_must_be_positive() -> None:
    """A non-positive sibling dob gap threshold is rejected (§3)."""
    from ccat.config import AcquireSpec

    with pytest.raises(ValueError, match="must be positive"):
        AcquireSpec(sibling_dob_max_gap_days=0)


def test_contact_placeholder_configured() -> None:
    """The season config carries the placeholder settings (§13.2)."""
    from ccat.config import InfoSheetSpec

    config = load_config(CONFIG_PATH)
    assert config.info_sheet.contact_placeholder_length == 30
    assert config.info_sheet.contact_placeholder_affiliations == (
        "Organization",
        "Walmart",
    )
    plain = InfoSheetSpec(
        template_file="t.ott", output_dir="o", mappings={"a.b": "c"}
    )
    assert plain.contact_placeholder_length == 30
    assert plain.contact_placeholder_affiliations == ()
    with pytest.raises(ValueError, match="contact_placeholder_length"):
        InfoSheetSpec(
            template_file="t.ott",
            output_dir="o",
            contact_placeholder_length=0,
            mappings={"a.b": "c"},
        )
    with pytest.raises(ValueError, match="contact_placeholder_affiliations"):
        InfoSheetSpec(
            template_file="t.ott",
            output_dir="o",
            contact_placeholder_affiliations=["Organization", ""],
            mappings={"a.b": "c"},
        )


def test_enum_values_resolved_from_vocabularies() -> None:
    """Enum columns resolve their vocabulary by name."""
    config = load_config(CONFIG_PATH)
    assert config.enum_values("child", "gender") == [
        "Male",
        "Female",
        "Non-binary",
    ]
    assert "Michigan" in config.enum_values("family", "state")
    assert "Any Age" in config.enum_values("sponsor", "age_preference")


def test_system_prompt_is_verbatim_with_placeholder() -> None:
    """The Appendix B prompt is stored verbatim with the placeholder."""
    config = load_config(CONFIG_PATH)
    assert "Copper Country Angel Tree" in config.ai_screening.system_prompt
    assert "{{xmas_date}}" in config.ai_screening.system_prompt


def _write(tmp_path: Path, data: dict) -> Path:
    """Dump a config dict to a temp JSON file."""
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data))
    return path


def test_unknown_key_fails(tmp_path: Path) -> None:
    """Unknown top-level keys fail fast."""
    good = json.loads(CONFIG_PATH.read_text())
    good["surprise"] = True
    with pytest.raises(ConfigError, match="surprise"):
        load_config(_write(tmp_path, good))


def test_missing_required_key_fails(tmp_path: Path) -> None:
    """Missing required keys fail fast."""
    good = json.loads(CONFIG_PATH.read_text())
    del good["matchmaking"]
    with pytest.raises(ConfigError, match="matchmaking"):
        load_config(_write(tmp_path, good))


def test_bad_column_type_fails(tmp_path: Path) -> None:
    """Unknown column types are rejected."""
    good = json.loads(CONFIG_PATH.read_text())
    next(iter(good["tables"]["sponsor"]["columns"].values()))["type"] = "blob"
    with pytest.raises(ConfigError, match="column type"):
        load_config(_write(tmp_path, good))


def test_enum_without_values_fails(tmp_path: Path) -> None:
    """Enum columns must declare values."""
    good = json.loads(CONFIG_PATH.read_text())
    columns = good["tables"]["sponsor"]["columns"]
    columns["extra_flag"] = {"type": "enum"}
    with pytest.raises(ConfigError, match="requires values"):
        load_config(_write(tmp_path, good))


def test_unknown_vocabulary_reference_fails(tmp_path: Path) -> None:
    """Enum columns referencing an unknown vocabulary fail fast."""
    good = json.loads(CONFIG_PATH.read_text())
    good["tables"]["sponsor"]["columns"]["sponsor_type"]["values"] = "nope"
    with pytest.raises(ConfigError, match="unknown vocabulary"):
        load_config(_write(tmp_path, good))


def test_bad_xmas_date_fails(tmp_path: Path) -> None:
    """xmas_date must be MM/DD/YYYY."""
    good = json.loads(CONFIG_PATH.read_text())
    good["xmas_date"] = "2026-12-25"
    with pytest.raises(ConfigError, match="xmas_date"):
        load_config(_write(tmp_path, good))


def test_missing_table_section_fails(tmp_path: Path) -> None:
    """All three table sections are required."""
    good = json.loads(CONFIG_PATH.read_text())
    del good["tables"]["child"]
    with pytest.raises(ConfigError, match="child"):
        load_config(_write(tmp_path, good))


def test_config_direct_construction() -> None:
    """Config constructs directly with all required fields."""
    good = json.loads(CONFIG_PATH.read_text())
    config = Config(**good)
    assert config.audit.port == 8642


def test_has_default_semantics() -> None:
    """An explicit null default still marks the column nullable."""
    with_default = ColumnSpec(type="string", default=None)
    without_default = ColumnSpec(type="string")
    assert with_default.has_default is True
    assert without_default.has_default is False


def test_storage_api_worker_defaults_on_real_config() -> None:
    """The refactor sections load with defaults on the real config."""
    config = load_config(CONFIG_PATH)
    assert config.storage.mode == "workbook"
    assert config.storage.database_url == (
        "sqlite+aiosqlite:///./output/ccat.sqlite3"
    )
    assert config.api.host == "127.0.0.1"
    assert config.api.port == 8650
    assert config.api.auth_mode == "dev"
    assert config.api.session_secret_env == "CCAT_SESSION_SECRET"
    assert config.api.session_ttl_hours == 12
    assert config.api.dev_users == ("cmendres", "lmboswell")
    assert config.worker.poll_interval_seconds == 2.0
    assert config.worker.heartbeat_seconds == 30.0
    assert config.worker.lease_seconds == 120.0
    assert config.worker.max_attempts == 3
    assert config.worker.progress_throttle_seconds == 1.0
    assert config.report.timezone == "local"


def test_storage_mode_vocabulary() -> None:
    """Only the two storage modes are accepted."""
    assert StorageSpec().mode == "workbook"
    assert StorageSpec(mode="db").mode == "db"
    with pytest.raises(ValueError, match="mode"):
        StorageSpec.model_validate({"mode": "excel"})


@pytest.mark.parametrize(
    ("spec", "field", "bad"),
    [
        (ApiSpec, "port", 0),
        (ApiSpec, "port", 70000),
        (ApiSpec, "session_ttl_hours", 0),
        (WorkerSpec, "poll_interval_seconds", 0),
        (WorkerSpec, "heartbeat_seconds", -1),
        (WorkerSpec, "lease_seconds", 0),
        (WorkerSpec, "max_attempts", 0),
        (WorkerSpec, "progress_throttle_seconds", -0.5),
    ],
)
def test_api_worker_invalid_values_rejected(
    spec: type[BaseModel], field: str, bad: object
) -> None:
    """Out-of-range API/worker settings fail fast."""
    with pytest.raises(ValueError, match=field):
        spec.model_validate({field: bad})


def test_report_timezone_validation() -> None:
    """report.timezone is 'local' or a valid IANA key."""
    assert ReportSpec().timezone == "local"
    assert ReportSpec(timezone="America/Detroit").timezone == (
        "America/Detroit"
    )
    with pytest.raises(ValueError, match="timezone"):
        ReportSpec(timezone="Mars/Olympus_Mons")


def test_report_timezone_invalid_fails_config_load(tmp_path: Path) -> None:
    """A bad timezone in the config JSON surfaces as ConfigError."""
    good = json.loads(CONFIG_PATH.read_text())
    good["report"]["timezone"] = "Mars/Olympus_Mons"
    with pytest.raises(ConfigError, match="timezone"):
        load_config(_write(tmp_path, good))
