"""DB-mode statistics report service (plan §9.6, decision D15).

Runs the legacy :mod:`ccat.stats` report against the DB-era
repositories: the pure calculation and rendering helpers are reused
verbatim; only the row sourcing differs.

- Default mode reads the repo's output-eligible finals (ignore/orphan
  filters via ``eligible_output_rows``); audited status is **not**
  required (legacy behavior).  Raw (``--raw``) mode prepares the repo's
  verbatim raw snapshot ephemerally with
  :func:`ccat.preparation.prepare_rows` — reconciled against the
  existing finals through its ``existing`` parameter, so stable IDs
  stay meaningful (this is *not* identity-independent) — and applies no
  output ignore/orphan filtering and no final assignments (sponsored
  counts are zero; desired-angel counts still use prepared sponsors).
- Timezone (D15): ``report.timezone`` — ``local`` keeps today's
  ``date.today()`` behavior; an IANA key dates the chart horizon, the
  generated date, and the filename via :func:`_report_today`.
- HTML is written first, then the PDF.  A PDF failure keeps the HTML
  and registers the artifacts with explicit partial state
  (``meta.complete=False``) instead of deleting anything (§9.6).
- The matplotlib figure helpers run inside a worker thread
  (``asyncio.to_thread``) so plotting state never touches the loop.

Artifacts are written under the artifact root ``reports/`` and
registered with kind ``stats_report``.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import polars as pl
from jinja2 import Environment

from ccat import stats as stats_module
from ccat.audit.store import build_final_frame, eligible_output_rows
from ccat.config import Config
from ccat.db.artifacts import artifact_root, register_file
from ccat.preparation import PreparationError, prepare_rows
from ccat.services.db_pipeline import _store
from ccat.services.results import StatsResult
from ccat.stats import StatsError

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_ENTITIES = ("sponsor", "family", "child")


def _report_today(cfg: Config) -> date:
    """Return the report generation day per ``report.timezone`` (D15).

    Args:
        cfg: Pipeline configuration.

    Returns:
        Today's date in the configured zone: server-local
        (``date.today()``) for ``local``, else the IANA-zone date.
    """
    key = cfg.report.timezone
    if key == "local":
        return date.today()
    return datetime.now(ZoneInfo(key)).date()


def _eligible_frame(entity: str, rows: "list[dict[str, Any]]") -> pl.DataFrame:
    """Build one typed frame from the entity's output-eligible rows.

    Args:
        entity: "child", "family", or "sponsor".
        rows: The complete final rows from the repository.

    Returns:
        The eligible population as a typed full-schema frame.
    """
    return build_final_frame(entity, eligible_output_rows(rows, entity))


def _load_enriched(
    cfg: Config,
    finals: "dict[str, list[dict[str, Any]]]",
    raw_rows: "dict[str, list[dict[str, Any]]] | None" = None,
    *,
    raw: bool = False,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, "list[str]"]:
    """Read/prepare the report populations and join them (§9.6).

    Port of the legacy ``_load_enriched`` over repository rows: the
    per-mode sourcing differs (repo finals vs ``prepare_rows`` over the
    raw snapshot reconciled against the finals), the joins/normalization
    are identical.

    Args:
        cfg: Pipeline configuration.
        finals: Complete final rows keyed by entity.
        raw_rows: Verbatim raw rows keyed by entity (raw mode only).
        raw: Prepare the raw rows ephemerally instead of reading
            eligible finals.

    Returns:
        A tuple of (enriched child frame with normalized
        location/gender columns, family frame with normalized
        location, sponsor frame or an empty stand-in, ordered location
        labels).

    Raises:
        StatsError: When a source is empty/unusable or preparation
            fails.
    """
    if raw:
        try:
            preparation = prepare_rows(raw_rows or {}, cfg, finals)
        except PreparationError as error:
            raise StatsError(str(error)) from error
        child = stats_module.prepared_frame(
            preparation.rows["child"], "child", cfg
        )
        family = stats_module.prepared_frame(
            preparation.rows["family"], "family", cfg
        )
        sponsor = stats_module.prepared_frame(
            preparation.rows["sponsor"], "sponsor", cfg
        )
        child_label = "prepared child rows"
        family_label = "prepared family rows"
        sponsor_label = "prepared sponsor rows"
        prerequisite = "acquire"
    else:
        child = _eligible_frame("child", finals["child"])
        family = _eligible_frame("family", finals["family"])
        sponsor = _eligible_frame("sponsor", finals["sponsor"])
        child_label = "final_child sheet"
        family_label = "final_family sheet"
        sponsor_label = "final_sponsor sheet"
        prerequisite = "audit"
    if child.is_empty():
        raise StatsError(
            f"{child_label}: missing or empty; run `ccat {prerequisite}` first"
        )
    missing = [
        column
        for column in stats_module.CHILD_REQUIRED_COLUMNS
        if column not in child.columns
    ]
    if missing:
        raise StatsError(
            f"{child_label} lacks column(s): {missing};"
            f" run `ccat {prerequisite}` first"
        )
    child = stats_module._coerce_created(
        child, "prepared child" if raw else "final_child"
    )
    if "assigned_sponsor_id" not in child.columns:
        if not raw:
            raise StatsError(
                f"{child_label} lacks column(s): ['assigned_sponsor_id'];"
                " run `ccat audit` first"
            )
        # Ephemeral prepared rows precede matchmaking assignments.
        child = child.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("assigned_sponsor_id")
        )
    family_prerequisite = "acquire" if raw else "match"
    if family.is_empty():
        raise StatsError(
            f"{family_label}: missing or empty;"
            f" run `ccat {family_prerequisite}` first"
        )
    missing_family = [
        column
        for column in stats_module.FAMILY_JOIN_COLUMNS
        if column not in family.columns
    ]
    if missing_family:
        raise StatsError(
            f"{family_label} lacks column(s): {missing_family}; run"
            f" `ccat {family_prerequisite}` first"
        )
    duplicated = family.group_by("family_id").len().filter(pl.col("len") > 1)
    if not duplicated.is_empty():
        logger.warning(
            "%s family_id is not unique; keeping the first row per family: %s",
            family_label,
            duplicated["family_id"].to_list(),
        )
        family = family.unique(subset="family_id", keep="first")
    family = family.with_columns(
        pl.col("gift_pickup_location")
        .fill_null("")
        .map_elements(stats_module._normalize_location, return_dtype=pl.String)
        .alias("gift_pickup_location"),
    )
    enriched = child.join(
        family.select(stats_module.FAMILY_JOIN_COLUMNS),
        on="family_id",
        how="left",
    ).with_columns(
        pl.col("gift_pickup_location")
        .fill_null("")
        .map_elements(stats_module._normalize_location, return_dtype=pl.String)
        .alias("gift_pickup_location"),
        pl.col("gender")
        .fill_null("")
        .map_elements(stats_module._normalize_gender, return_dtype=pl.String)
        .alias("gender_key"),
        pl.col("assigned_sponsor_id").cast(pl.Int64, strict=False),
    )
    enriched = enriched.sort(
        ["created", "zite_record_id"], nulls_last=True, maintain_order=True
    )
    sponsor = stats_module._load_sponsor(sponsor, sponsor_label)
    present = sorted(
        set(enriched["gift_pickup_location"].to_list())
        | set(family["gift_pickup_location"].to_list())
    )
    return enriched, family, sponsor, stats_module._location_order(cfg, present)


def _daily_admissions(
    enriched: pl.DataFrame,
    today: date,
    source: str = "final_child",
) -> "tuple[list[date], dict[str, list[int]]]":
    """Count children added per day per pickup location (§9.6).

    Port of the legacy calculation with the horizon end supplied by the
    caller (:func:`_report_today`) instead of a hardcoded
    ``date.today()``: every calendar day from the earliest child
    ``created`` date through ``today`` inclusive, zero-filled.

    Args:
        enriched: The enriched child frame (``created`` as Datetime).
        today: The report generation day (horizon end).
        source: Child source label used in errors.

    Returns:
        A tuple of (every calendar day on the horizon, mapping of
        location label to a per-day count aligned with that day list).

    Raises:
        StatsError: If no child has a usable ``created`` timestamp.
    """
    stamped = enriched.filter(pl.col("created").is_not_null())
    if stamped.is_empty():
        raise StatsError(
            f"{source}: no usable created timestamps for the daily"
            " admissions chart"
        )
    counts = (
        stamped.with_columns(
            pl.col("created").dt.truncate("1d").alias("day"),
        )
        .group_by(["day", "gift_pickup_location"])
        .len()
    )
    first = stamped.select(pl.col("created").dt.date().min()).item()
    end = today if today >= first else first
    days = [
        first + timedelta(days=offset)
        for offset in range((end - first).days + 1)
    ]
    index = {day: position for position, day in enumerate(days)}
    lookup = {
        (row["day"].date(), row["gift_pickup_location"]): row["len"]
        for row in counts.iter_rows(named=True)
    }
    locations = sorted(set(enriched["gift_pickup_location"].to_list()))
    per_location = {location: [0] * len(days) for location in locations}
    for (day, location), count in lookup.items():
        if location not in per_location:
            per_location[location] = [0] * len(days)
        per_location[location][index[day]] = count
    return days, per_location


def _render_html(
    cfg: Config,
    *,
    today: date,
    daily_image: str,
    distribution_image: str,
    table_rows: "list[dict[str, Any]]",
    gender_keys: "list[str]",
    gender_headers: "list[str]",
    families: int,
    sponsors: int,
) -> str:
    """Render the report HTML from the shared template (§9.6, D15).

    Port of the legacy renderer with the generated date supplied by the
    caller (:func:`_report_today`).

    Args:
        cfg: Pipeline configuration.
        today: The report generation day.
        daily_image: Base64 PNG data URI of the admissions chart.
        distribution_image: Base64 PNG data URI of the gender/age
            figure.
        table_rows: Per-location rows plus the totals row.
        gender_keys: Lowercase gender keys (column order).
        gender_headers: Display headers matching ``gender_keys``.
        families: Total family count.
        sponsors: Total sponsors with assigned angels.

    Returns:
        The complete HTML document.
    """
    environment = Environment(autoescape=True)
    template = environment.from_string(stats_module._REPORT_TEMPLATE)
    return template.render(
        title=f"CCAT {cfg.season} Season Report",
        generated_date=today.isoformat(),
        children=sum(
            row["genders"][key]
            for row in table_rows[:-1]
            for key in gender_keys
        ),
        families=families,
        sponsors=sponsors,
        daily_image=daily_image,
        daily_width=stats_module.FIGURE_WIDTH,
        daily_height=stats_module._fit_dimensions(
            daily_image, stats_module.FIGURE_WIDTH
        )[1],
        distribution_image=distribution_image,
        distribution_width=stats_module.FIGURE_WIDTH,
        distribution_height=stats_module._fit_dimensions(
            distribution_image, stats_module.FIGURE_WIDTH
        )[1],
        table_rows=[
            {**row, "total": row["label"] == "All locations"}
            for row in table_rows
        ],
        gender_keys=gender_keys,
        gender_headers=gender_headers,
    )


def _compute(
    cfg: Config,
    enriched: pl.DataFrame,
    family: pl.DataFrame,
    sponsor: pl.DataFrame,
    locations: "list[str]",
    today: date,
    source: str,
) -> str:
    """Run every calculation and render the report HTML (§9.6).

    Runs inside a worker thread: the matplotlib helpers are not
    thread-safe against the event loop (§5.3).

    Args:
        cfg: Pipeline configuration.
        enriched: The enriched child frame.
        family: The family frame (normalized location).
        sponsor: The sponsor frame (or empty stand-in).
        locations: Ordered location labels.
        today: The report generation day.
        source: Child source label used in errors.

    Returns:
        The complete HTML document.

    Raises:
        StatsError: When the sources cannot support the report.
    """
    days, per_location = _daily_admissions(enriched, today, source)
    colors = stats_module._location_colors(locations)
    gender_keys = [
        key
        for key, _header in stats_module.REPORT_GENDERS
        + stats_module.UNSPECIFIED_GENDER
        if enriched.filter(pl.col("gender_key") == key).height
    ]
    headers = dict(
        stats_module.REPORT_GENDERS + stats_module.UNSPECIFIED_GENDER
    )
    gender_headers = [headers[key] for key in gender_keys]
    table_rows = stats_module._location_table(
        enriched, family, sponsor, locations, gender_keys
    )
    gender_counts = {
        headers[key]: enriched.filter(pl.col("gender_key") == key).height
        for key in gender_keys
    }
    age_series = enriched["age_at_xmas"].drop_nulls().cast(pl.Int64)
    age_counts: dict[int, int] = {
        int(age): count
        for age, count in age_series.value_counts()
        .sort("age_at_xmas")
        .iter_rows()
    }
    daily_image = stats_module._daily_figure(days, per_location, colors)
    distribution_image = stats_module._gender_age_figure(
        gender_counts, age_counts
    )
    sponsors_count = enriched["assigned_sponsor_id"].drop_nulls().n_unique()
    return _render_html(
        cfg,
        today=today,
        daily_image=daily_image,
        distribution_image=distribution_image,
        table_rows=table_rows,
        gender_keys=gender_keys,
        gender_headers=gender_headers,
        families=family.height,
        sponsors=sponsors_count,
    )


async def stats(
    cfg: Config,
    actor: str,
    options: "dict[str, Any]",
    progress: "Callable[..., None]",
    cancel: "Callable[[], None]",
) -> StatsResult:
    """Generate the dated statistics report (HTML and PDF) (§9.6).

    Default mode reads the repo's eligible finals; ``raw`` prepares the
    raw snapshot ephemerally (no persistence, orphans included,
    assignments zero).  HTML is written first and kept on PDF failure,
    with the artifacts registered in an explicit partial state.

    Args:
        cfg: Pipeline configuration.
        actor: Requesting user/job identity.
        options: ``raw`` (ephemeral prepared mode).
        progress: Progress reporter.
        cancel: Cooperative cancel check.

    Returns:
        The typed result; ``pdf_written`` False means the HTML was
        kept as an explicit partial artifact (``meta.complete=False``).

    Raises:
        StatsError: When the sources are unusable.
        JobCancelRequested: When cancellation was requested.
    """
    raw = bool(options.get("raw"))
    async with _store(cfg, actor) as repo:
        raw_data = (
            {table: await repo.raw_rows(table) for table in _ENTITIES}
            if raw
            else None
        )
        finals = {entity: await repo.final_rows(entity) for entity in _ENTITIES}
    progress("prepare")
    enriched, family, sponsor, locations = await asyncio.to_thread(
        _load_enriched, cfg, finals, raw_data, raw=raw
    )
    today = _report_today(cfg)
    source = "prepared child rows" if raw else "final_child"
    html = await asyncio.to_thread(
        _compute, cfg, enriched, family, sponsor, locations, today, source
    )
    cancel()
    progress("render")
    root = artifact_root(cfg)
    out_dir = root / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{cfg.report.filename_prefix}_{today.isoformat()}"
    html_path = out_dir / f"{stem}.html"
    pdf_path = out_dir / f"{stem}.pdf"
    html_path.write_text(html, encoding="utf-8")
    logger.info("wrote report %s", html_path)
    html_ok = True
    pdf_ok = False
    cancel()
    try:
        await asyncio.to_thread(stats_module._render_pdf, html, pdf_path)
        pdf_ok = True
        logger.info("wrote report %s", pdf_path)
    except Exception as error:  # noqa: BLE001 - explicit partial state
        # §9.6: the HTML was written first and stays; the artifacts
        # record the partial state instead of deleting the report.
        logger.error("stats PDF rendering failed; HTML kept: %s", error)
    complete = html_ok and pdf_ok
    async with _store(cfg, actor) as repo:
        await register_file(
            repo,
            "stats_report",
            html_path,
            actor,
            meta={"complete": complete, "mode": "raw" if raw else "final"},
            root=root,
        )
        if pdf_ok:
            await register_file(
                repo,
                "stats_report",
                pdf_path,
                actor,
                meta={"complete": True},
                root=root,
            )
    return StatsResult(
        html_path=str(html_path),
        pdf_path=str(pdf_path) if pdf_ok else None,
        html_written=html_ok,
        pdf_written=pdf_ok,
    )


__all__ = ["_report_today", "stats"]
