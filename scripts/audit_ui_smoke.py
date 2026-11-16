"""Ad-hoc smoke check: boot the NiceGUI audit server and hit every page.

Runs ONLY against a throwaway workbook: the config is a full copy of
``config/ccat_2026_v2.json`` with every file path redirected into a
temporary directory, so this script can never touch a real season
workbook (see output/ccat_2026.xlsx — the old system of record is
never written by the v2 audit tool).

Usage: `.venv/bin/python scripts/audit_ui_smoke.py`
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "test"))

from test_audit_store import seed_raw, seed_submissions  # noqa: E402

from ccat.audit.store import ensure_adj_sheets, save_child  # noqa: E402
from ccat.config import load_config  # noqa: E402


def build_smoke_config(tmp: Path) -> Any:
    """Build a full v2 config with every file path inside ``tmp``."""
    base = load_config(ROOT / "config" / "ccat_2026_v2.json")
    return base.model_copy(
        update={
            "excel_file": str(tmp / "wb.xlsx"),
            "log_file": str(tmp / "log.txt"),
            "match_report_csv": str(tmp / "report.csv"),
            "clarify": base.clarify.model_copy(
                update={
                    "output_dir": str(tmp / "clarify_emails"),
                    "clarifications_csv": str(tmp / "clarifications.csv"),
                }
            ),
            "audit": base.audit.model_copy(
                update={"port": 8799, "host": "127.0.0.1"}
            ),
            "info_sheet": base.info_sheet.model_copy(
                update={"output_dir": str(tmp / "sheets")}
            ),
            "sponsor_packets": base.sponsor_packets.model_copy(
                update={"output_dir": str(tmp / "packets")}
            ),
        }
    )


def main() -> None:
    """Seed a throwaway workbook, boot the server, hit every page."""
    tmp = Path(tempfile.mkdtemp(prefix="audit_smoke_"))
    cfg = build_smoke_config(tmp)
    seed_raw(cfg)
    seed_submissions(cfg)
    ensure_adj_sheets(cfg)
    save_child(cfg, "c1", {"need_1": "fixed coat", "audited": True})

    # The server must be started with this config, never DEFAULT_CONFIG.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys; from ccat.config import load_config;"
                " from ccat.audit.server import run as audit_run;"
                f" cfg = load_config({str(tmp / 'smoke.json')!r});"
                " audit_run(cfg)"
            ),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        import json

        (tmp / "smoke.json").write_text(json.dumps(cfg.model_dump(mode="json")))
        deadline = time.time() + 30
        ok = False
        while time.time() < deadline:
            try:
                response = requests.get("http://127.0.0.1:8799/", timeout=2)
                if (
                    response.status_code == 200
                    and "Human Audit" in response.text
                ):
                    ok = True
                    break
            except requests.RequestException:
                time.sleep(0.5)
        if not ok:
            print("FAILED to boot dashboard")
            print(proc.stdout.read() if proc.stdout else "")
            raise SystemExit(1)
        for path in (
            "/sponsors",
            "/sponsors/1",
            "/families",
            "/families/1",
            "/families/1/children/c1",
            "/families/0/children/orphan1",
        ):
            page = requests.get(f"http://127.0.0.1:8799{path}", timeout=10)
            assert page.status_code == 200, (path, page.status_code)
            print(f"OK {path} ({len(page.text)} bytes)")
        print("SMOKE PASS: all pages render")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
