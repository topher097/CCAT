"""Shared fixtures for the CCAT pipeline test suite."""

from pathlib import Path

import pytest

from ccat.config import AIScreeningSpec, Config, load_config

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "ccat_2026.json"


@pytest.fixture(autouse=True)
def _opencode_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the SDK at a dummy URL so tests never spawn a real server.

    Screening tests monkeypatch ``opencode.Opencode``; setting the env
    var keeps ``_screen_targets`` on the connect (not embedded-server)
    path.
    """
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")


def load_test_cfg(**updates: object) -> Config:
    """Load the real season config with path overrides for tests.

    Args:
        updates: Fields overridden on the loaded config.

    Returns:
        A Config instance pointing at temporary paths.
    """
    config = load_config(CONFIG_PATH)
    return config.model_copy(update=updates)


@pytest.fixture(autouse=True)
def _pin_default_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests on the season config.

    The user's ``.envrc`` exports ``DEFAULT_CONFIG`` pointing at the
    season config; the test suite builds its fixtures from that
    config's table specs, so tests pin the variable to the repo
    fallback regardless of the environment they run in.
    """
    monkeypatch.setenv("DEFAULT_CONFIG", str(CONFIG_PATH))


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Config with all file paths redirected into a temp directory."""
    return load_test_cfg(
        excel_file=str(tmp_path / "wb.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
    )


def load_output_cfg(tmp_path: Path) -> Config:
    """Config redirecting every generated-output path into ``tmp_path``.

    Like :func:`load_test_cfg` but also redirects the clarify,
    info-sheet, and sponsor-packet output directories, so no test ever
    writes into the real ``output/`` tree.
    """
    base = load_test_cfg(
        excel_file=str(tmp_path / "wb.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
    )
    return base.model_copy(
        update={
            "clarify": base.clarify.model_copy(
                update={
                    "output_dir": str(tmp_path / "clarify_emails"),
                    "clarifications_csv": str(tmp_path / "clarifications.csv"),
                }
            ),
            "info_sheet": base.info_sheet.model_copy(
                update={"output_dir": str(tmp_path / "child_info_sheets")}
            ),
            "sponsor_packets": base.sponsor_packets.model_copy(
                update={"output_dir": str(tmp_path / "sponsor_packets")}
            ),
        }
    )


@pytest.fixture
def screen_cfg(tmp_path: Path) -> Config:
    """Config with a usable ai_screening model for screening runs."""
    base = load_test_cfg(
        excel_file=str(tmp_path / "wb.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
    )
    return base.model_copy(
        update={
            "ai_screening": AIScreeningSpec(
                model="test/model",
                system_prompt=base.ai_screening.system_prompt,
                temperature=0.0,
            )
        }
    )
