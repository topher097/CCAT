"""Configuration models and loader for the CCAT pipeline.

All pipeline configuration lives in one JSON file (default
``config/ccat_2026.json``).  This module loads it once per module run into a
:class:`Config` instance and fails fast on unknown keys, missing required
keys, or invalid vocabulary references (see pipeline_architecture.md §3).
"""

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)

#: Allowed values for a column's ``type`` declaration (§3).
COLUMN_TYPES = frozenset(
    {
        "string",
        "name",
        "email",
        "phone_us",
        "int",
        "float",
        "bool",
        "date",
        "datetime",
        "enum",
    }
)

#: Tables configured in the workbook contract (§4).
TABLE_NAMES = ("sponsor", "family", "child")


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


class ColumnSpec(BaseModel):
    """Declarative spec of one imported column (§3).

    Attributes:
        type: Validation type applied after cleaning.
        rename: Source Zite column name mapped onto this key, when the
            auto-normalized name differs.
        default: Value substituted for null before validation.
        values: Inline enum value list, or the name of a vocabulary in
            ``config.vocabularies``.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    rename: str | None = None
    default: Any = None
    values: str | list[str] | None = None

    @field_validator("type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        """Reject column types outside the supported set."""
        if value not in COLUMN_TYPES:
            raise ValueError(f"unknown column type {value!r}")
        return value

    @property
    def has_default(self) -> bool:
        """Whether a ``default`` key was explicitly declared (even null)."""
        return "default" in self.model_fields_set


class TableSpec(BaseModel):
    """Column specs for one Zite table."""

    model_config = ConfigDict(extra="forbid")

    columns: dict[str, ColumnSpec]


class ZiteSpec(BaseModel):
    """Zite table references consumed by ``ccat.zite.get_table_data``."""

    model_config = ConfigDict(extra="forbid")

    sponsor: str
    family: str
    child: str


class AIScreeningSpec(BaseModel):
    """AI screening settings (module 2), used via the opencode SDK.

    Attributes:
        model: Model reference (``provider/model``) used for screening.
        system_prompt: The screening system prompt.
        temperature: Sampling temperature (0.0 = deterministic).
        max_workers: Concurrent LLM requests during batch screening.
            One opencode session is created per worker thread.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    system_prompt: str
    temperature: float = 0.0
    max_workers: int = 4

    @field_validator("max_workers")
    @classmethod
    def _check_max_workers(cls, value: int) -> int:
        """Require a positive worker count."""
        if value <= 0:
            raise ValueError("ai_screening.max_workers must be positive")
        return value


class AuditSpec(BaseModel):
    """Local audit server settings (module 3)."""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8642


class AcquireSpec(BaseModel):
    """Data acquisition settings (module 1)."""

    model_config = ConfigDict(extra="forbid")

    #: Same-family children born at most this many days apart are
    #: recorded in ``dob_conflict_log`` (§5.6); 274 days is ~9 months.
    sibling_dob_max_gap_days: int = 274

    @field_validator("sibling_dob_max_gap_days")
    @classmethod
    def _check_gap_days(cls, value: int) -> int:
        """Require a positive day threshold."""
        if value <= 0:
            raise ValueError("sibling_dob_max_gap_days must be positive")
        return value


class MatchmakingSpec(BaseModel):
    """Lexicographic tier weights (module 4, §8.3).

    Weights are sized so no lower tier can outweigh a higher one.
    """

    model_config = ConfigDict(extra="forbid")

    location_weight: int = 1000
    age_weight: int = 100
    gender_weight: int = 1


class InfoSheetSpec(BaseModel):
    """Child info sheet generation settings (module 5).

    Attributes:
        template_file: Path to the py3o LibreOffice ``.ott`` template.
        output_dir: Directory receiving one ``<angel_id>.pdf`` per child.
        soffice_binary: LibreOffice binary used for the ODT -> PDF
            conversion.
        mappings: py3o template variable (dotted, e.g.
            ``"entry.colors"``) mapped onto the column key of the
            enriched ``final_child`` row (see module 5).
    """

    model_config = ConfigDict(extra="forbid")

    template_file: str
    output_dir: str
    soffice_binary: str = "soffice"
    mappings: dict[str, str]

    @model_validator(mode="after")
    def _check_mappings(self) -> "InfoSheetSpec":
        """Require non-empty, well-formed variable mappings."""
        if not self.mappings:
            raise ValueError("info_sheet.mappings must not be empty")
        for variable, column in self.mappings.items():
            parts = variable.split(".")
            if len(parts) < 2 or not all(parts):
                raise ValueError(
                    f"info_sheet.mappings: variable {variable!r} must be a"
                    " dotted py3o path with a namespace (e.g. 'entry.colors')"
                )
            if not column:
                raise ValueError(
                    f"info_sheet.mappings: {variable!r} maps to an empty column"
                )
        return self


class ReportSpec(BaseModel):
    """Statistics report settings (module 6).

    Attributes:
        output_dir: Directory receiving the dated ``<prefix>_<date>.html``
            and ``.pdf`` report files.
        filename_prefix: Report filename prefix; the current date is
            appended (e.g. ``report_2026-11-10``).
    """

    model_config = ConfigDict(extra="forbid")

    output_dir: str = "output/reports"
    filename_prefix: str = "report"


class Config(BaseModel):
    """Root configuration model for one pipeline run."""

    model_config = ConfigDict(extra="forbid")

    season: str
    xmas_date: str
    excel_file: str
    zite: ZiteSpec
    tables: dict[str, TableSpec]
    vocabularies: dict[str, list[str]]
    location_groups: dict[str, list[str]]
    acquire: AcquireSpec
    ai_screening: AIScreeningSpec
    audit: AuditSpec
    matchmaking: MatchmakingSpec
    info_sheet: InfoSheetSpec
    report: ReportSpec = ReportSpec()
    log_file: str
    match_report_csv: str

    @field_validator("xmas_date")
    @classmethod
    def _check_xmas_date(cls, value: str) -> str:
        """Require the documented MM/DD/YYYY format."""
        try:
            datetime.strptime(value, "%m/%d/%Y")
        except ValueError as error:
            raise ValueError(
                f"xmas_date must be MM/DD/YYYY, got {value!r}"
            ) from error
        return value

    @model_validator(mode="after")
    def _check_tables_and_vocabs(self) -> "Config":
        """Validate table set and enum vocabulary references."""
        for name in TABLE_NAMES:
            if name not in self.tables:
                raise ValueError(f"missing tables.{name} section")
        for table_name, table in self.tables.items():
            for key, spec in table.columns.items():
                if spec.type == "enum":
                    if spec.values is None:
                        raise ValueError(
                            f"tables.{table_name}.columns.{key}: enum column"
                            " requires values"
                        )
                    if isinstance(spec.values, str) and spec.values not in (
                        self.vocabularies
                    ):
                        raise ValueError(
                            f"tables.{table_name}.columns.{key}: unknown"
                            f" vocabulary {spec.values!r}"
                        )
        return self

    @property
    def xmas(self) -> date:
        """The configured Christmas date as a ``date``."""
        return datetime.strptime(self.xmas_date, "%m/%d/%Y").date()

    def enum_values(self, table_name: str, column_key: str) -> list[str]:
        """Resolve the allowed values for one enum column.

        Args:
            table_name: Table containing the column.
            column_key: The lower snake_case column key.

        Returns:
            The exact vocabulary labels allowed for the column.

        Raises:
            ConfigError: If the column is not an enum column.
        """
        spec = self.tables[table_name].columns[column_key]
        if spec.type != "enum" or spec.values is None:
            raise ConfigError(
                f"{table_name}.{column_key} is not an enum column"
            )
        if isinstance(spec.values, str):
            return self.vocabularies[spec.values]
        return spec.values


def load_config(path: str | Path) -> Config:
    """Load and validate the pipeline configuration.

    Args:
        path: Path to the JSON configuration file.

    Returns:
        The validated :class:`Config`.

    Raises:
        ConfigError: If the file is missing, unparseable, or violates the
            configuration contract.
    """
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ConfigError(
            f"config file is not valid JSON: {path}: {error}"
        ) from error
    try:
        return Config(**data)
    except ValueError as error:
        raise ConfigError(
            f"invalid configuration in {path}: {error}"
        ) from error
