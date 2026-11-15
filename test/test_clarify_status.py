"""Tests for module 7 — contact-status sidecar store."""

import json
from pathlib import Path
from typing import Any

import pytest

from ccat.clarify.extract import ClarifyError
from ccat.clarify.status import (
    FamilyStatus,
    STATUS_FILE_NAME,
    load_statuses,
    status_path,
    store_statuses,
)
from ccat.config import Config


def configure(
    cfg: Config, tmp_path: Path, store: dict[str, Any] | None = None
) -> Config:
    """Redirect ``clarify.output_dir`` at a temp dir with optional store."""
    out_dir = tmp_path / "emails"
    out_dir.mkdir(parents=True, exist_ok=True)
    if store is not None:
        (out_dir / STATUS_FILE_NAME).write_text(json.dumps(store))
    return cfg.model_copy(
        update={
            "clarify": cfg.clarify.model_copy(
                update={"output_dir": str(out_dir)}
            )
        }
    )


class TestLoadStatuses:
    """``load_statuses`` defaults and parsing."""

    def test_empty_when_no_store(self, cfg: Config, tmp_path: Path) -> None:
        """With no sidecar on disk every family defaults to untouched."""
        statuses = load_statuses(configure(cfg, tmp_path))
        assert statuses == {}

    def test_round_trip(self, cfg: Config, tmp_path: Path) -> None:
        """store_statuses then load_statuses returns the same map."""
        cfg = configure(cfg, tmp_path)
        statuses = {
            1: FamilyStatus(
                contacted=True,
                contacted_at="2026-11-14T20:00:00",
                replied=False,
            ),
            2: FamilyStatus(contacted=True, replied=True),
        }
        store_statuses(cfg, statuses)
        assert load_statuses(cfg) == statuses

    def test_defaults_for_fresh_entry(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """A stored entry with only ``contacted`` fills the defaults."""
        cfg = configure(cfg, tmp_path, store={"3": {"contacted": True}})
        status = load_statuses(cfg)[3]
        assert status.contacted is True
        assert status.contacted_at is None
        assert status.replied is False

    def test_invalid_json_raises(self, cfg: Config, tmp_path: Path) -> None:
        """Corrupt JSON fails with ClarifyError."""
        out_dir = tmp_path / "emails"
        out_dir.mkdir(parents=True)
        (out_dir / STATUS_FILE_NAME).write_text("{ not json")
        cfg = cfg.model_copy(
            update={
                "clarify": cfg.clarify.model_copy(
                    update={"output_dir": str(out_dir)}
                )
            }
        )
        with pytest.raises(ClarifyError, match="unreadable"):
            load_statuses(cfg)

    def test_non_integer_key_raises(self, cfg: Config, tmp_path: Path) -> None:
        """A non-integer family key fails with ClarifyError."""
        cfg = configure(
            cfg,
            tmp_path,
            store={
                "abc": {
                    "contacted": True,
                    "contacted_at": None,
                    "replied": False,
                }
            },
        )
        with pytest.raises(ClarifyError, match="non-integer"):
            load_statuses(cfg)

    def test_invalid_entry_raises(self, cfg: Config, tmp_path: Path) -> None:
        """Entry-level violations (extra keys) fail with ClarifyError."""
        cfg = configure(cfg, tmp_path, store={"1": {"surprise": True}})
        with pytest.raises(ClarifyError, match="invalid"):
            load_statuses(cfg)

    def test_status_path(self, cfg: Config, tmp_path: Path) -> None:
        """The store lives inside ``clarify.output_dir``."""
        cfg = configure(cfg, tmp_path)
        assert status_path(cfg) == (tmp_path / "emails" / STATUS_FILE_NAME)
        assert status_path(cfg).name == STATUS_FILE_NAME
