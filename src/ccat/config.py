"""Configuration models and loader for the CCAT pipeline.

All pipeline configuration lives in one JSON file (default
``config/ccat_2026.json``).  This module loads it once per module run into a
:class:`Config` instance and fails fast on unknown keys, missing required
keys, or invalid vocabulary references (see pipeline_architecture.md §3).
"""

import json
import logging
import tempfile
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)

#: Configuration keys kept only for the legacy workbook commands.  The
#: configuration files do not carry them, and the settings tree must not
#: write them back.
DEPRECATED_CONFIG_KEYS = ("excel_file",)

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
    """Raised when the configuration file is missing or invalid.

    Attributes:
        errors: Structured contract violations (``path``/``message``
            pairs) when the failure came from the configuration
            contract; empty for file-level problems.
    """

    def __init__(
        self, message: str, errors: list[dict[str, str]] | None = None
    ) -> None:
        """Initialize the error.

        Args:
            message: Human-readable failure description.
            errors: Structured contract violations, when known.
        """
        super().__init__(message)
        self.errors = errors or []


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


class ClarifySpec(BaseModel):
    """Clarification email tool settings (module 7).

    Attributes:
        model: Model reference (``provider/model``) writing the email
            prose, via the opencode SDK (same pattern as module 2).
        system_prompt: Instructions for the email-writing model; the
            family record JSON is appended at send time.
        email_template_html_file: Jinja2 HTML template wrapping the
            model's prose fragments in paste-safe inline-CSS markup.
        output_dir: Directory receiving one ``family_<id>.html`` draft
            and its ``family_<id>.json`` companion (the editable
            subject/fragment data) per generated email.
        clarifications_csv: CSV path refreshed from the workbook on
            every ``ccat clarify-export`` run; it carries a
            ``clarification_email_file`` column referencing each
            family's saved draft when one exists.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    system_prompt: str
    email_template_html_file: str = "data/clarification_email_2026.html"
    output_dir: str = "output/clarify_emails"
    clarifications_csv: str = "output/clarifications.csv"


class ScreeningSpec(BaseModel):
    """Pre-audit screening settings (module 2, non-AI checks)."""

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


class EmailCheckSpec(BaseModel):
    """ZeroBounce email deliverability check settings (module 2).

    Attributes:
        enabled: Run the bulk ZeroBounce check during screening.
        workers: Fallback per-email checks run on this many worker
            threads (the bulk batch path uses one call per 100 emails).
        delay: Retained for sequential pacing; the bulk and threaded
            paths do not sleep between calls.
        api_key_env: Environment variable holding the ZeroBounce key.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    workers: int = 4
    delay: float = 0.25
    api_key_env: str = "ZEROBOUNCE_API_KEY"

    @field_validator("workers")
    @classmethod
    def _check_workers(cls, value: int) -> int:
        """Require at least one worker thread."""
        if value < 1:
            raise ValueError("workers must be positive")
        return value


class AddressCheckSpec(BaseModel):
    """Nominatim family-address validation settings (module 2).

    Attributes:
        enabled: Run the address check during screening.
        base_url: Nominatim root URL (self-hosted instances work).
        delay: Seconds between requests; the OSM usage policy caps
            Nominatim at 1 request per second.
        user_agent: Identifying User-Agent header (required by the
            OSM usage policy).
        timeout: Per-request timeout in seconds.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    base_url: str = "https://nominatim.openstreetmap.org"
    delay: float = 1.1
    user_agent: str = "ccat-angel-tree/1.0 (family address validation)"
    timeout: float = 30.0


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
        output_dir: Directory receiving one ``AT<season>_<angel_id>.pdf``
            per sponsored child.
        soffice_binary: LibreOffice binary used for the ODT -> PDF
            conversion.
        mappings: py3o template variable (dotted, e.g.
            ``"entry.colors"``) mapped onto the column key of the
            enriched ``final_child`` row (see module 5).
        contact_placeholder_length: Number of underscore characters in
            the hand-fill placeholder rendered for missing sponsor
            contact info (§13.2).
        contact_placeholder_affiliations: Sponsor affiliations whose
            missing contact info renders the placeholder (§13.2);
            other affiliations render blank.
        max_workers: Concurrent sheet generations (py3o render +
            LibreOffice conversion); each worker thread gets its own
            temporary LibreOffice profile.  Values above the number of
            pending children are clamped.
        conversion_timeout_seconds: Hard ceiling for one headless
            LibreOffice conversion (DB-mode worker; the legacy module
            keeps its identical built-in constant).
    """

    model_config = ConfigDict(extra="forbid")

    template_file: str
    output_dir: str
    soffice_binary: str = "soffice"
    contact_placeholder_length: int = 30
    contact_placeholder_affiliations: tuple[str, ...] = ()
    mappings: dict[str, str]
    max_workers: int = 4
    conversion_timeout_seconds: float = 300.0

    @field_validator("max_workers")
    @classmethod
    def _check_max_workers(cls, value: int) -> int:
        """Require a positive worker count."""
        if value <= 0:
            raise ValueError("info_sheet.max_workers must be positive")
        return value

    @field_validator("conversion_timeout_seconds")
    @classmethod
    def _check_conversion_timeout(cls, value: float) -> float:
        """Require a positive conversion deadline."""
        if value <= 0:
            raise ValueError(
                "info_sheet.conversion_timeout_seconds must be positive"
            )
        return value

    @model_validator(mode="after")
    def _check_mappings(self) -> "InfoSheetSpec":
        """Require non-empty, well-formed variable mappings."""
        if self.contact_placeholder_length < 1:
            raise ValueError(
                "info_sheet.contact_placeholder_length must be at least 1"
            )
        if any(not label for label in self.contact_placeholder_affiliations):
            raise ValueError(
                "info_sheet.contact_placeholder_affiliations entries"
                " must be non-empty"
            )
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
        timezone: ``local`` (server local time, today's behavior) or an
            IANA zone key (e.g. ``America/Detroit``) used to date-stamp
            report filenames and content.
    """

    model_config = ConfigDict(extra="forbid")

    output_dir: str = "output/reports"
    filename_prefix: str = "report"
    timezone: str = "local"

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        """Require ``local`` or a resolvable IANA zone key."""
        if value == "local":
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError(
                f"report.timezone must be 'local' or a valid IANA zone"
                f" key, got {value!r}"
            ) from error
        return value


class SponsorPacketSpec(BaseModel):
    """Sponsor packet settings (module 5b).

    Attributes:
        email_template_html_file: Path to the Jinja2 HTML email
            template; rendered once per sponsor with assigned angels.
        instructions_file: PDF copied as-is into each sponsor's packet
            directory (the emailed sponsor instructions).
        output_dir: Root directory receiving one
            ``<sponsor_id>/`` packet directory per sponsor with
            assigned angels, holding the email draft
            (``<prefix>_<sponsor_id>.html``) and the combined PDF
            (``Sponsor <sponsor_id> - Child Information <season>.pdf``).
        filename_prefix: Prefix for the email draft filename inside
            each sponsor directory (e.g.
            ``sponsor_packet_<sponsor_id>.html``).
        bcc_emails: Addresses copied into every draft's BCC line
            (typically program coordinators).
    """

    model_config = ConfigDict(extra="forbid")

    email_template_html_file: str = "data/sponsor_packet_email_2026.html"
    instructions_file: str = "data/Sponsor Instruction 2026.pdf"
    output_dir: str = "output/sponsor_packets"
    filename_prefix: str = "sponsor_packet"
    bcc_emails: list[str] = []


class StorageSpec(BaseModel):
    """Active season-storage selection (refactor decision D12).

    Attributes:
        mode: Deprecated. ``workbook`` and ``db`` are accepted, but no
            code reads it: the API and worker always use the SQLite
            repositories.  The value only records what a config was
            written for.
        database_url: Async SQLAlchemy URL for the DB mode.  The sync
            URL used by Alembic and the CLI importer is derived in
            :mod:`ccat.db.engine`.
        artifact_root: Private server-side directory holding generated
            artifacts (plan §6.3); paths are registered relative to it
            and downloads are contained within it.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["workbook", "db"] = "db"
    database_url: str = "sqlite+aiosqlite:///./output/ccat.sqlite3"
    artifact_root: str = "output/artifacts"


class ApiSpec(BaseModel):
    """Web API server settings (refactor §5).

    Attributes:
        host: Bind address of the FastAPI server.
        port: Bind port of the FastAPI server.
        auth_mode: ``dev`` enables the no-password bootstrap accounts and
            the no-login auto identity (loopback development only);
            ``oidc`` defers to the real identity adapter.
        session_secret_env: Environment variable holding the cookie
            session signing secret.
        session_ttl_hours: Server-side session lifetime in hours.
        dev_users: No-password bootstrap usernames created in dev mode.
    """

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8650
    auth_mode: Literal["dev", "oidc"] = "dev"
    session_secret_env: str = "CCAT_SESSION_SECRET"
    session_ttl_hours: int = 12
    dev_users: tuple[str, ...] = ("cmendres", "lmboswell")

    @field_validator("port")
    @classmethod
    def _check_port(cls, value: int) -> int:
        """Require a bindable TCP port."""
        if not 1 <= value <= 65535:
            raise ValueError("api.port must be between 1 and 65535")
        return value

    @field_validator("session_ttl_hours")
    @classmethod
    def _check_ttl(cls, value: int) -> int:
        """Require a positive session lifetime."""
        if value <= 0:
            raise ValueError("api.session_ttl_hours must be positive")
        return value


class WorkerSpec(BaseModel):
    """Durable-job worker settings (refactor §6, decision D7).

    Attributes:
        poll_interval_seconds: Delay between job-table polls when idle.
        heartbeat_seconds: Worker heartbeat cadence on running jobs.
        lease_seconds: Job lease duration before recovery may reclaim.
        max_attempts: Default per-job attempt ceiling.
        progress_throttle_seconds: Minimum delay between persisted
            progress updates (zero disables throttling).
    """

    model_config = ConfigDict(extra="forbid")

    poll_interval_seconds: float = 2.0
    heartbeat_seconds: float = 30.0
    lease_seconds: float = 120.0
    max_attempts: int = 3
    progress_throttle_seconds: float = 1.0

    @field_validator(
        "poll_interval_seconds", "heartbeat_seconds", "lease_seconds"
    )
    @classmethod
    def _check_positive(cls, value: float) -> float:
        """Require a positive interval."""
        if value <= 0:
            raise ValueError("worker intervals must be positive")
        return value

    @field_validator("max_attempts")
    @classmethod
    def _check_attempts(cls, value: int) -> int:
        """Require a positive attempt ceiling."""
        if value <= 0:
            raise ValueError("worker.max_attempts must be positive")
        return value

    @field_validator("progress_throttle_seconds")
    @classmethod
    def _check_throttle(cls, value: float) -> float:
        """Require a non-negative throttle."""
        if value < 0:
            raise ValueError("worker.progress_throttle_seconds must be >= 0")
        return value


class Config(BaseModel):
    """Root configuration model for one pipeline run.

    Attributes:
        season: Season identifier.
        xmas_date: Christmas date, used to compute ``age_at_xmas``.
        excel_file: Deprecated. Path of the legacy Excel workbook.  The
            live store is the database; this path only reaches the
            workbook-era commands (``ccat acquire``/``screen``/``match``/
            ``generate``, ``ccat audit-migrate``) and the import/export
            commands.  Kept until the database-to-Excel export replaces
            the legacy path.
        zite: Zite table references.
        tables: Column schema per table.
        vocabularies: Allowed values per vocabulary.
        location_groups: Location groups for matching.
    """

    model_config = ConfigDict(extra="forbid")

    season: str
    xmas_date: str
    excel_file: str = ""
    zite: ZiteSpec
    tables: dict[str, TableSpec]
    vocabularies: dict[str, list[str]]
    location_groups: dict[str, list[str]]
    ai_screening: AIScreeningSpec
    screening: ScreeningSpec = ScreeningSpec()
    email_check: EmailCheckSpec = EmailCheckSpec()
    address_check: AddressCheckSpec = AddressCheckSpec()
    clarify: ClarifySpec
    matchmaking: MatchmakingSpec
    info_sheet: InfoSheetSpec
    report: ReportSpec = ReportSpec()
    sponsor_packets: SponsorPacketSpec = SponsorPacketSpec()
    storage: StorageSpec = StorageSpec()
    api: ApiSpec = ApiSpec()
    worker: WorkerSpec = WorkerSpec()
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


def load_config_data(path: str | Path) -> dict[str, Any]:
    """Read the raw configuration mapping without validating it.

    Args:
        path: Path to the JSON configuration file.

    Returns:
        The parsed mapping.

    Raises:
        ConfigError: If the file is missing, unparseable, or does not
            hold a JSON object.
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
    if not isinstance(data, dict):
        raise ConfigError(f"config file must hold a JSON object: {path}")
    return data


def _merge(model: Any, file: Any, dropped: list[str], path: str = "") -> Any:
    """Overlay a file's values onto the model's default tree.

    Args:
        model: The model's default value for this node.
        file: The file's value for this node.
        dropped: Collects the dotted paths of file keys the model does
            not know.
        path: Dotted path of this node (for the dropped list).

    Returns:
        The merged value.
    """
    if not isinstance(model, dict):
        return file
    source = file if isinstance(file, dict) else {}
    merged: dict[str, Any] = {}
    for key, value in model.items():
        child = f"{path}.{key}" if path else key
        if key in source:
            merged[key] = _merge(value, source[key], dropped, child)
        elif isinstance(value, dict):
            merged[key] = _merge(value, {}, dropped, child)
        elif value is not None:
            merged[key] = value
    for key in source:
        if key not in model:
            dropped.append(f"{path}.{key}" if path else key)
    return merged


def effective_config(
    path: str | Path, fallback: Config
) -> tuple[dict[str, Any], list[str]]:
    """Return the effective configuration tree of one configuration file.

    The file's own values always win; settings the file does not
    mention are filled in from the model, so every known setting is
    visible and editable.  A file that no longer validates falls back
    to the running configuration's defaults, which lets an admin repair
    a broken file through the settings UI.

    Args:
        path: Path to the JSON configuration file.
        fallback: The running configuration, used for the defaults when
            the file itself does not validate.

    Returns:
        The merged tree and the dotted paths of the file keys the
        contract does not know (they are not part of the merged tree).
    """
    data = load_config_data(path)
    try:
        cfg = Config(**data)
    except ValidationError:
        cfg = fallback
    dropped: list[str] = []
    merged = _merge(cfg.model_dump(mode="json"), data, dropped)
    for key in DEPRECATED_CONFIG_KEYS:
        if key not in data:
            merged.pop(key, None)
    return merged, dropped


def save_config(path: str | Path, data: Mapping[str, Any]) -> Config:
    """Validate a configuration mapping and write it to disk.

    The write is atomic (temp file in the same directory, then
    rename), so an interrupted save never leaves a half-written
    configuration behind.

    Args:
        path: Path to the JSON configuration file.
        data: The complete configuration mapping.

    Returns:
        The validated configuration.

    Raises:
        ConfigError: When the data violates the configuration contract
            or the file cannot be written.
    """
    try:
        cfg = Config(**dict(data))
    except ValidationError as error:
        errors = [
            {
                "path": ".".join(str(part) for part in item["loc"]),
                "message": item["msg"],
            }
            for item in error.errors()
        ]
        summary = "; ".join(
            f"{item['path']}: {item['message']}" for item in errors
        )
        raise ConfigError(
            f"invalid configuration: {summary}", errors
        ) from error
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            temp = Path(handle.name)
        temp.replace(target)
    except OSError as error:
        raise ConfigError(
            f"config file is not writable: {target}: {error}"
        ) from error
    return cfg


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
    data = load_config_data(path)
    try:
        return Config(**data)
    except ValueError as error:
        raise ConfigError(
            f"invalid configuration in {path}: {error}"
        ) from error
