"""Export the committed OpenAPI contract (Phase 4 client source).

Refresh whenever the API changes::

    .venv/bin/python scripts/export_openapi.py

Writes ``frontend/openapi.json`` from the checked-in season config.
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend" / "openapi.json"


def main() -> None:
    """Write the OpenAPI schema for the default config."""
    from ccat.api import create_app
    from ccat.config import load_config

    config = os.environ.get("CCAT_CONFIG", "config/ccat_2026.json")
    app = create_app(load_config(config))
    schema = app.openapi()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(schema['components']['schemas'])} schemas)")


if __name__ == "__main__":
    main()
