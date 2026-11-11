"""Module 6 -- statistics report generation.

Reads the post-match state (``final_child`` joined with ``final_family``
and ``final_sponsor``) from the pipeline workbook and renders a dated
report as both HTML and PDF into ``report.output_dir``:

1. A stacked bar chart of children (angels) added per day per pickup
   site, spanning every calendar day from the earliest child
   ``created`` timestamp through the generation day, with no bar on
   days without admissions and one color per pickup location (blue
   and green, cycled for additional locations).
2. A per-pickup-location table (males, females, non-binary, families,
   sponsors, sponsored angels) with an all-locations totals row.
   Children whose family has no usable pickup location fall into an
   ``Unknown`` bucket, and unmatched family ids are tolerated (left
   join) rather than fatal -- the report describes the data as it is.
3. A combined figure of the gender distribution and the age
   (``age_at_xmas``) distribution of all children.

Both files are named ``<filename_prefix>_<YYYY-MM-DD>.<ext>`` (the
generation day) and written together; a PDF rendering failure raises
:class:`StatsError` after the HTML file is already on disk.
"""

import base64
import io
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402 - must follow backend choice
import polars as pl
from jinja2 import Environment
from xhtml2pdf import pisa

from ccat.config import Config
from ccat.io import read_sheet

logger = logging.getLogger(__name__)

#: ``final_family`` columns joined onto each child row.
FAMILY_JOIN_COLUMNS = (
    "family_id",
    "gift_pickup_location",
    "assigned_sponsor_id",
)

#: ``final_child`` columns the report cannot work without.
CHILD_REQUIRED_COLUMNS = (
    "zite_record_id",
    "family_id",
    "created",
    "gender",
    "age_at_xmas",
)

#: Bucket label for children without a usable pickup location.
UNKNOWN_LOCATION = "Unknown"

#: Display labels for the known genders, in table-column order;
#: anything else (or null) is reported under ``Unspecified``.
REPORT_GENDERS: tuple[tuple[str, str], ...] = (
    ("male", "Males"),
    ("female", "Females"),
    ("non-binary", "Non-binary"),
)
UNSPECIFIED_GENDER_KEY = "unspecified"
UNSPECIFIED_GENDER: tuple[tuple[str, str], ...] = (
    (UNSPECIFIED_GENDER_KEY, "Unspecified"),
)

#: Matplotlib colors backing the per-location bar colors (blue, green),
#: cycled for locations beyond the first two.
LOCATION_COLORS = ("#1f77b4", "#2ca02c")

#: Plot size (inches) and resolution for both figures.
FIGSIZE = (10.0, 4.0)
DPI = 150


class StatsError(Exception):
    """Raised when statistics report generation cannot complete."""


def _normalize_location(value: Any) -> str:
    """Coerce one pickup location cell to a report label.

    Args:
        value: Raw ``gift_pickup_location`` cell (may be null/blank).

    Returns:
        The stripped label, or ``UNKNOWN_LOCATION`` when unusable.
    """
    if value is None:
        return UNKNOWN_LOCATION
    text = str(value).strip()
    return text if text else UNKNOWN_LOCATION


def _normalize_gender(value: Any) -> str:
    """Coerce one gender cell to a lowercase report key.

    Args:
        value: Raw ``gender`` cell (may be null or any casing).

    Returns:
        One of ``male``/``female``/``non-binary``, or
        ``unspecified`` when null or unrecognized.
    """
    if value is None:
        return UNSPECIFIED_GENDER_KEY
    key = str(value).strip().lower().replace(" ", "-")
    if key in ("nonbinary", "non-binary"):
        return "non-binary"
    if key in ("male", "female"):
        return key
    return UNSPECIFIED_GENDER_KEY


def _location_order(cfg: Config, present: list[str]) -> list[str]:
    """Order pickup locations for charts and tables.

    Known vocabulary locations come first (config order), then any
    extra observed labels sorted alphabetically, with ``Unknown``
    always last.

    Args:
        cfg: The pipeline configuration.
        present: Location labels observed in the data.

    Returns:
        The ordered location labels (every vocabulary location is
        kept even when absent, so the table shows complete zeros).
    """
    known = list(cfg.enum_values("family", "gift_pickup_location"))
    extras = sorted(set(present) - set(known) - {UNKNOWN_LOCATION})
    ordered = known + extras
    if UNKNOWN_LOCATION in present and UNKNOWN_LOCATION not in ordered:
        ordered.append(UNKNOWN_LOCATION)
    return ordered


def _coerce_created(frame: pl.DataFrame) -> pl.DataFrame:
    """Coerce the ``created`` column to Datetime in place.

    Args:
        frame: The ``final_child`` frame.

    Returns:
        The frame with ``created`` as Datetime (nulls where a string
        cell cannot be parsed).

    Raises:
        StatsError: If the column cannot be coerced at all.
    """
    dtype = frame.schema["created"]
    if dtype == pl.Datetime:
        return frame
    try:
        if dtype == pl.String:
            parsed = frame.with_columns(
                pl.col("created").str.to_datetime(strict=False)
            )
            if parsed["created"].null_count() == frame.height:
                # Offset-bearing ISO strings need an explicit zone.
                parsed = frame.with_columns(
                    pl.col("created").str.to_datetime(
                        strict=False, time_zone="UTC"
                    )
                )
            return parsed
        if dtype == pl.Date:
            return frame.with_columns(pl.col("created").cast(pl.Datetime))
        return frame.with_columns(pl.col("created").cast(pl.Datetime))
    except Exception as error:  # noqa: BLE001 - cast raises many types
        raise StatsError(
            f"final_child.created has unusable type {dtype}: {error}"
        ) from error


def _load_enriched(
    cfg: Config,
) -> tuple[pl.DataFrame, pl.DataFrame, list[str]]:
    """Read and join the final sheets for the report.

    Args:
        cfg: The pipeline configuration.

    Returns:
        A tuple of (enriched child frame -- ``final_child`` plus
        ``gift_pickup_location`` and ``assigned_sponsor_id`` joined
        from ``final_family`` with normalized location/gender columns,
        family frame with a normalized location column, ordered
        location labels).

    Raises:
        StatsError: If ``final_child``/``final_family`` is missing or
            empty, or lacks the required columns.
    """
    child = read_sheet(cfg.excel_file, "final_child")
    if child is None or child.is_empty():
        raise StatsError(
            "final_child sheet is missing or empty; run `ccat audit` first"
        )
    missing = [
        column
        for column in CHILD_REQUIRED_COLUMNS
        if column not in child.columns
    ]
    if missing:
        raise StatsError(
            f"final_child lacks column(s): {missing}; run `ccat audit`"
            " and `ccat match` first"
        )
    child = _coerce_created(child)
    family = read_sheet(cfg.excel_file, "final_family")
    if family is None or family.is_empty():
        raise StatsError(
            "final_family sheet is missing or empty; run `ccat match` first"
        )
    missing_family = [
        column for column in FAMILY_JOIN_COLUMNS if column not in family.columns
    ]
    if missing_family:
        raise StatsError(
            f"final_family lacks column(s): {missing_family}; run"
            " `ccat match` first"
        )
    duplicated = family.group_by("family_id").len().filter(pl.col("len") > 1)
    if not duplicated.is_empty():
        logger.warning(
            "final_family family_id is not unique; keeping the first row"
            " per family: %s",
            duplicated["family_id"].to_list(),
        )
        family = family.unique(subset="family_id", keep="first")
    family = family.with_columns(
        pl.col("gift_pickup_location")
        .fill_null("")
        .map_elements(_normalize_location, return_dtype=pl.String)
        .alias("gift_pickup_location"),
        pl.col("assigned_sponsor_id").cast(pl.Int64, strict=False),
    )
    enriched = child.join(
        family.select(FAMILY_JOIN_COLUMNS),
        on="family_id",
        how="left",
    ).with_columns(
        pl.col("gift_pickup_location")
        .fill_null("")
        .map_elements(_normalize_location, return_dtype=pl.String)
        .alias("gift_pickup_location"),
        pl.col("gender")
        .fill_null("")
        .map_elements(_normalize_gender, return_dtype=pl.String)
        .alias("gender_key"),
        pl.col("assigned_sponsor_id").cast(pl.Int64, strict=False),
    )
    present = sorted(
        set(enriched["gift_pickup_location"].to_list())
        | set(family["gift_pickup_location"].to_list())
    )
    return enriched, family, _location_order(cfg, present)


def _daily_admissions(
    enriched: pl.DataFrame,
) -> tuple[list[date], dict[str, list[int]]]:
    """Count children added per day per pickup location.

    Args:
        enriched: The enriched child frame (``created`` as Datetime).

    Returns:
        A tuple of (every calendar day from the earliest ``created``
        date through the generation day inclusive, mapping of location
        label to a per-day count aligned with that day list; days with
        no admissions carry a zero count).

    Raises:
        StatsError: If no child has a usable ``created`` timestamp.
    """
    stamped = enriched.filter(pl.col("created").is_not_null())
    if stamped.is_empty():
        raise StatsError(
            "final_child has no usable created timestamps for the daily"
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
    end = date.today() if (_today := date.today()) >= first else first
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


def _location_table(
    enriched: pl.DataFrame,
    family: pl.DataFrame,
    locations: list[str],
    gender_keys: list[str],
) -> list[dict[str, Any]]:
    """Build the per-location statistics rows plus a totals row.

    Args:
        enriched: The enriched child frame.
        family: The final_family frame (normalized location).
        locations: Ordered location labels.
        gender_keys: Ordered lowercase gender keys for the columns.

    Returns:
        Rows of ``{"label", "genders": {key: count}, "families",
        "sponsors", "sponsored"}``; the last row is the all-locations
        total.
    """
    family_counts = (
        family.group_by("gift_pickup_location")
        .agg(pl.col("family_id").n_unique().alias("families"))
        .to_dict(as_series=False)
    )
    families_at = dict(
        zip(
            family_counts["gift_pickup_location"],
            family_counts["families"],
            strict=False,
        )
    )
    rows: list[dict[str, Any]] = []
    totals = {
        "genders": {key: 0 for key in gender_keys},
        "families": family["family_id"].n_unique(),
        "sponsors": enriched["assigned_sponsor_id"].drop_nulls().n_unique(),
        "sponsored": enriched.filter(
            pl.col("assigned_sponsor_id").is_not_null()
        ).height,
    }
    for location in locations:
        here = enriched.filter(pl.col("gift_pickup_location") == location)
        genders = {
            key: here.filter(pl.col("gender_key") == key).height
            for key in gender_keys
        }
        sponsors = here["assigned_sponsor_id"].drop_nulls().n_unique()
        sponsored = here.filter(
            pl.col("assigned_sponsor_id").is_not_null()
        ).height
        row = {
            "label": location,
            "genders": genders,
            "families": families_at.get(location, 0),
            "sponsors": sponsors,
            "sponsored": sponsored,
        }
        rows.append(row)
        for key in gender_keys:
            totals["genders"][key] += genders[key]
    rows.append({"label": "All locations", **totals})
    return rows


def _figure_to_png(fig: plt.Figure) -> str:
    """Render one matplotlib figure as a base64 PNG data URI.

    Args:
        fig: The figure to render; it is closed afterwards.

    Returns:
        A ``data:image/png;base64,...`` URI usable in HTML and PDF.
    """
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    return (
        "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    )


def _daily_figure(
    days: list[date],
    per_location: dict[str, list[int]],
    colors: dict[str, Any],
) -> str:
    """Render the stacked per-day admissions bar chart.

    Args:
        days: Every calendar day on the x axis.
        per_location: Location label -> per-day counts.
        colors: Location label -> bar color.

    Returns:
        The figure as a base64 PNG data URI.
    """
    fig, ax = plt.subplots(figsize=FIGSIZE)
    x = range(len(days))
    bottom = [0] * len(days)
    labeled: set[str] = set()
    for location in sorted(per_location):
        values = per_location[location]
        for position, value in enumerate(values):
            if not value:  # no bar for a day without admissions
                continue
            ax.bar(
                position,
                value,
                bottom=bottom[position],
                color=colors.get(location),
                edgecolor="white",
                linewidth=0.3,
                label=location if location not in labeled else "_nolegend_",
            )
            labeled.add(location)
        bottom = [
            base + value for base, value in zip(bottom, values, strict=False)
        ]
    stride = max(1, len(days) // 15)
    ticks = list(x)[::stride]
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [days[position].isoformat() for position in ticks],
        rotation=45,
        ha="right",
        fontsize=8,
    )
    ax.set_ylabel("Children added")
    ax.set_title("Angels (children) added per day per pickup site")
    ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    return _figure_to_png(fig)


def _location_colors(locations: list[str]) -> dict[str, Any]:
    """Map each pickup location to its bar color.

    Args:
        locations: Ordered location labels.

    Returns:
        Location label -> color; blue and green, cycled for locations
        beyond the first two.
    """
    return {
        location: LOCATION_COLORS[index % len(LOCATION_COLORS)]
        for index, location in enumerate(locations)
    }


def _gender_age_figure(
    gender_counts: dict[str, int], age_counts: dict[int, int]
) -> str:
    """Render the combined gender and age distribution figure.

    Args:
        gender_counts: Display label -> child count.
        age_counts: Age at Christmas -> child count.

    Returns:
        The figure as a base64 PNG data URI.
    """
    fig, (gender_ax, age_ax) = plt.subplots(1, 2, figsize=FIGSIZE)
    gender_ax.bar(
        list(gender_counts), list(gender_counts.values()), color="#4c72b0"
    )
    gender_ax.set_title("Gender distribution")
    gender_ax.set_ylabel("Children")
    for label in gender_ax.get_xticklabels():
        label.set_rotation(20)
        label.set_ha("right")
    ages = sorted(age_counts)
    age_ax.bar(
        [str(age) for age in ages],
        [age_counts[age] for age in ages],
        color="#55a868",
    )
    age_ax.set_title("Age distribution (at Christmas)")
    age_ax.set_xlabel("Age")
    age_ax.set_ylabel("Children")
    fig.tight_layout()
    return _figure_to_png(fig)


_REPORT_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>{{ title }}</title>
<style>
@page { size: a4 portrait; margin: 1.8cm; }
body { font-family: Helvetica, sans-serif; font-size: 10pt; }
h1 { font-size: 16pt; margin-bottom: 2pt; }
h2 { font-size: 12pt; margin-top: 14pt; }
p.subtitle { color: #555555; font-size: 9pt; margin-top: 0pt; }
table { border: 1px solid #999999; margin-top: 6pt; }
td, th { border: 1px solid #999999; padding: 4pt 6pt; font-size: 9pt; }
th { background-color: #eeeeee; }
td.number, th.number { text-align: right; }
tr.total td { font-weight: bold; background-color: #f5f5f5; }
img { width: 100%; }
</style>
</head>
<body>
<h1>{{ title }}</h1>
<p class="subtitle">Generated {{ generated_date }} &mdash; {{ children }}
children, {{ families }} families, {{ sponsors }} sponsors with
assigned angels.</p>

<h2>Angels added per day per pickup site</h2>
<p><img src="{{ daily_image }}"/></p>

<h2>Per pickup location</h2>
<table>
<tr>
  <th>Pickup location</th>
  {% for header in gender_headers %}<th class="number">{{ header
  }}</th>{% endfor %}
  <th class="number">Families</th>
  <th class="number">Sponsors</th>
  <th class="number">Sponsored angels</th>
</tr>
{% for row in table_rows %}
<tr{% if row.total %} class="total"{% endif %}>
  <td>{{ row.label }}</td>
  {% for key in gender_keys %}<td class="number">{{ row.genders[key]
  }}</td>{% endfor %}
  <td class="number">{{ row.families }}</td>
  <td class="number">{{ row.sponsors }}</td>
  <td class="number">{{ row.sponsored }}</td>
</tr>
{% endfor %}
</table>

<h2>Gender and age distribution (all children)</h2>
<p><img src="{{ distribution_image }}"/></p>
</body>
</html>
"""


def _render_html(
    cfg: Config,
    *,
    daily_image: str,
    distribution_image: str,
    table_rows: list[dict[str, Any]],
    gender_keys: list[str],
    gender_headers: list[str],
    families: int,
    sponsors: int,
) -> str:
    """Render the report HTML from the Jinja template.

    Args:
        cfg: The pipeline configuration.
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
    template = environment.from_string(_REPORT_TEMPLATE)
    return template.render(
        title=f"CCAT {cfg.season} Season Report",
        generated_date=date.today().isoformat(),
        children=sum(
            row["genders"][key]
            for row in table_rows[:-1]
            for key in gender_keys
        ),
        families=families,
        sponsors=sponsors,
        daily_image=daily_image,
        distribution_image=distribution_image,
        table_rows=[
            {**row, "total": row["label"] == "All locations"}
            for row in table_rows
        ],
        gender_keys=gender_keys,
        gender_headers=gender_headers,
    )


def _render_pdf(html: str, pdf_path: Path) -> None:
    """Render the report HTML into a PDF file.

    Args:
        html: The complete HTML document.
        pdf_path: Destination PDF path (parent must exist).

    Raises:
        StatsError: If the rendering fails or produces no output.
    """
    with pdf_path.open("wb") as destination:
        status = pisa.CreatePDF(html, dest=destination, encoding="utf-8")
    if status.err:
        raise StatsError(f"xhtml2pdf reported {status.err} error(s)")
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        raise StatsError("xhtml2pdf produced no PDF output")


def run(cfg: Config) -> None:
    """Generate the dated statistics report (HTML and PDF).

    Reads the final sheets, computes the daily admissions, the
    per-location table, and the gender/age distributions, then writes
    ``<filename_prefix>_<YYYY-MM-DD>.html`` and ``.pdf`` into
    ``report.output_dir``.

    Args:
        cfg: The pipeline configuration.

    Raises:
        StatsError: If the sheets are unusable or rendering fails.
    """
    spec = cfg.report
    out_dir = Path(spec.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    enriched, family, locations = _load_enriched(cfg)
    days, per_location = _daily_admissions(enriched)
    colors = _location_colors(locations)
    gender_keys = [
        key
        for key, _header in REPORT_GENDERS + UNSPECIFIED_GENDER
        if enriched.filter(pl.col("gender_key") == key).height
    ]
    headers = dict(REPORT_GENDERS + UNSPECIFIED_GENDER)
    gender_headers = [headers[key] for key in gender_keys]
    table_rows = _location_table(enriched, family, locations, gender_keys)
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
    daily_image = _daily_figure(days, per_location, colors)
    distribution_image = _gender_age_figure(gender_counts, age_counts)
    sponsors = enriched["assigned_sponsor_id"].drop_nulls().n_unique()
    html = _render_html(
        cfg,
        daily_image=daily_image,
        distribution_image=distribution_image,
        table_rows=table_rows,
        gender_keys=gender_keys,
        gender_headers=gender_headers,
        families=family.height,
        sponsors=sponsors,
    )
    stem = f"{spec.filename_prefix}_{date.today().isoformat()}"
    html_path = out_dir / f"{stem}.html"
    pdf_path = out_dir / f"{stem}.pdf"
    html_path.write_text(html, encoding="utf-8")
    logger.info("wrote report %s", html_path)
    _render_pdf(html, pdf_path)
    logger.info("wrote report %s", pdf_path)
