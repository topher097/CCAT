"""Tests for config loading and validation (§3)."""

import json
from pathlib import Path

import pytest

from ccat.config import ColumnSpec, Config, ConfigError, load_config

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
