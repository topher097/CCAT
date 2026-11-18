"""Bulk-validate emails in a CSV with ZeroBounce.

Reads a CSV, validates every unchecked email via the ZeroBounce API,
and rewrites the file in place with these columns appended:
zerobounce_status, zerobounce_sub_status, zerobounce_did_you_mean,
zerobounce_checked_at.

Idempotent: rows that already have a zerobounce_checked_at timestamp
are skipped, so re-running costs no extra credits.
"""

import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import fire

try:
    from ccat.engines.zerobounce_email_check import (
        OutOfCreditsError,
        validate_email,
    )
except ImportError:
    # ccat is not installed (e.g. run from a bare scripts/ checkout):
    # bootstrap the package source tree onto sys.path and retry.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from ccat.engines.zerobounce_email_check import (
        OutOfCreditsError,
        validate_email,
    )

NEW_COLUMNS = [
    "zerobounce_status",
    "zerobounce_sub_status",
    "zerobounce_did_you_mean",
    "zerobounce_checked_at",
]


def check_csv(
    csv_path: str,
    email_column: str = "email_on_record",
    api_key: str | None = None,
    delay: float = 0.25,
) -> int:
    """Validate every unchecked email in a CSV with ZeroBounce.

    Args:
        csv_path (str): Path to the CSV file. Rewritten in place with the
            result columns appended.
        email_column (str): Name of the column holding email addresses.
        api_key (str | None): ZeroBounce API key. Defaults to the
            ZEROBOUNCE_API_KEY environment variable.
        delay (float): Seconds to pause between validations, for rate
            safety.

    Returns:
        int: Number of rows newly checked in this run. Rows that failed
            with an API error are left unchecked so a later run retries
            them.

    Raises:
        OutOfCreditsError: The account ran out of validation credits.
    """
    path = Path(csv_path)
    with path.open(newline="", encoding="utf-8") as file:
        raw_rows = list(csv.reader(file))

    if not raw_rows:
        print(f"{path} is empty.")
        return 0

    header = [name.strip() for name in raw_rows[0]]
    rows = [row for row in raw_rows[1:] if any(cell.strip() for cell in row)]

    try:
        email_index = header.index(email_column)
    except ValueError:
        print(f"Column {email_column!r} not found. Columns: {header}")
        return 0

    for column in NEW_COLUMNS:
        if column not in header:
            header.append(column)

    status_index = header.index("zerobounce_status")
    sub_status_index = header.index("zerobounce_sub_status")
    did_you_mean_index = header.index("zerobounce_did_you_mean")
    checked_index = header.index("zerobounce_checked_at")

    def cell(row: list[str], index: int) -> str:
        return row[index].strip() if index < len(row) else ""

    checked_count = 0
    for row in rows:
        row += [""] * (len(header) - len(row))
        email = cell(row, email_index)

        if cell(row, checked_index):
            print(f"Skipping (already checked): {email or '(no email)'}")
            continue

        if not email:
            print("Marking row without an email as checked.")
            row[status_index] = "no_email"
            row[checked_index] = datetime.now().isoformat(timespec="seconds")
            checked_count += 1
            continue

        result = validate_email(email, api_key=api_key)
        if result is None:
            print(f"API error for {email}; left unchecked for retry.")
            continue

        row[status_index] = str(result["status"])
        row[sub_status_index] = str(result["sub_status"])
        row[did_you_mean_index] = str(result["did_you_mean"])
        row[checked_index] = datetime.now().isoformat(timespec="seconds")
        checked_count += 1

        suggestion = str(result["did_you_mean"])
        suffix = f" (did you mean: {suggestion})" if suggestion else ""
        print(
            f"{email} -> {result['status']} / {result['sub_status'] or 'none'}{suffix}"
        )
        time.sleep(delay)

    with path.open("w", newline="", encoding="utf-8") as file:
        csv.writer(file).writerows([header, *rows])

    print(f"Checked {checked_count} email(s); wrote {path}.")
    return checked_count


if __name__ == "__main__":
    try:
        fire.Fire(check_csv)
    except OutOfCreditsError as error:
        print(f"ERROR: {error}")
        sys.exit(2)
