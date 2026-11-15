"""Module 7 — per-family clarification contact status.

The clarifications CSV is rebuilt from the workbook on every
``ccat clarify`` / ``ccat clarify-export`` run, so contact state
(contacted, when, replied) cannot live in the CSV alone: it would be
wiped by every refresh.  Instead it lives in a small JSON sidecar
inside ``clarify.output_dir`` and is merged into the CSV columns
``contacted``, ``contacted_at``, and ``replied`` on every write.
"""

import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ccat.clarify.extract import ClarifyError
from ccat.config import Config

logger = logging.getLogger(__name__)

#: JSON-sidecar filename inside ``clarify.output_dir``.
STATUS_FILE_NAME = "clarification_status.json"

#: CSV column holding the contacted flag.
CONTACTED_COLUMN = "contacted"

#: CSV column holding the contacted-at timestamp (ISO 8601, local time).
CONTACTED_AT_COLUMN = "contacted_at"

#: CSV column holding the reply-received flag.
REPLIED_COLUMN = "replied"


class FamilyStatus(BaseModel):
    """One family's contact-status entry.

    Attributes:
        contacted: Whether the clarification email was sent to the
            family.
        contacted_at: When the operator last pressed "Mark
            contacted" (ISO 8601 local time).
        replied: Whether the family replied.
    """

    model_config = ConfigDict(extra="forbid")

    contacted: bool = False
    contacted_at: str | None = None
    replied: bool = False


def status_path(cfg: Config) -> Path:
    """Return the JSON status-store path.

    Args:
        cfg: Pipeline configuration.

    Returns:
        ``clarify.output_dir/clarification_status.json``.
    """
    return Path(cfg.clarify.output_dir) / STATUS_FILE_NAME


def load_statuses(cfg: Config) -> dict[int, FamilyStatus]:
    """Load every family's contact status.

    Args:
        cfg: Pipeline configuration.

    Returns:
        Mapping of family_id to status; families absent from the
        store default to untouched (:attr:`FamilyStatus` defaults).

    Raises:
        ClarifyError: If the store exists but is invalid JSON or has
            non-integer keys.
    """
    store = status_path(cfg)
    if not store.is_file():
        return {}
    try:
        raw = json.loads(store.read_text())
    except (json.JSONDecodeError, OSError) as error:
        raise ClarifyError(
            f"contact-status store {store} is unreadable: {error}"
        ) from error
    statuses: dict[int, FamilyStatus] = {}
    for key, value in raw.items():
        try:
            family_id = int(key)
        except (TypeError, ValueError) as error:
            raise ClarifyError(
                f"contact-status store {store} has a non-integer family"
                f" key {key!r}"
            ) from error
        try:
            statuses[family_id] = FamilyStatus(**value)
        except ValueError as error:
            raise ClarifyError(
                f"contact-status entry for family {key} in {store} is"
                f" invalid: {error}"
            ) from error
    return statuses


def store_statuses(cfg: Config, statuses: dict[int, FamilyStatus]) -> None:
    """Persist every family's contact status atomically.

    Args:
        cfg: Pipeline configuration.
        statuses: Mapping of family_id to status.

    Raises:
        ClarifyError: If the store cannot be written.
    """
    store = status_path(cfg)
    store.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            str(family_id): status.model_dump()
            for family_id, status in sorted(statuses.items())
        },
        indent=2,
    )
    tmp_path = store.parent / f".{store.name}.tmp"
    try:
        tmp_path.write_text(payload)
        os.replace(tmp_path, store)
    except OSError as error:
        raise ClarifyError(
            f"could not write contact-status store {store}: {error}"
        ) from error
    logger.debug("stored contact status for %d family(ies)", len(statuses))
