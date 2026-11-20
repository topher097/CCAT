"""Module app for ``uvicorn ccat.api.main:app`` plus the serve helper.

The config comes from ``CCAT_CONFIG`` (or the ``DEFAULT_CONFIG``
fallback), matching the CLI's override chain (plan §5.4).
"""

import os

from ccat.api import create_app
from ccat.config import load_config

#: The ``DEFAULT_CONFIG`` env override and fallback (§5.4).
DEFAULT_CONFIG = os.environ.get("DEFAULT_CONFIG", "config/ccat_2026.json")

#: The app's config path: ``CCAT_CONFIG`` wins over ``DEFAULT_CONFIG``.
CONFIG_PATH = os.environ.get("CCAT_CONFIG", DEFAULT_CONFIG)

app = create_app(load_config(CONFIG_PATH), config_path=CONFIG_PATH)


def serve() -> None:
    """Run uvicorn against the module app (operator entry point)."""
    import uvicorn

    cfg = load_config(CONFIG_PATH)
    uvicorn.run(
        app,
        host=cfg.api.host,
        port=cfg.api.port,
        reload=False,
    )


__all__ = ["app", "serve"]
