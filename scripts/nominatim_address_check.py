"""Backward-compat shim: the engine moved to ``ccat.engines`` (Phase 1a).

The Nominatim engine implementation now lives in
:mod:`ccat.engines.nominatim_address_check`; this shim keeps the script
runnable standalone (``python scripts/nominatim_address_check.py ...``)
and keeps flat imports of ``nominatim_address_check`` working by
re-exporting the package version.
"""

import sys
from pathlib import Path

try:
    from ccat.engines.nominatim_address_check import *  # noqa: F403
    from ccat.engines.nominatim_address_check import (  # noqa: F401
        build_query,
        check_address,
    )
    from ccat.engines.nominatim_address_check import _cli_check
except ImportError:
    # ccat is not installed (e.g. run from a bare scripts/ checkout):
    # bootstrap the package source tree onto sys.path and retry.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from ccat.engines.nominatim_address_check import *  # noqa: F403
    from ccat.engines.nominatim_address_check import (  # noqa: F401
        build_query,
        check_address,
    )
    from ccat.engines.nominatim_address_check import _cli_check

if __name__ == "__main__":
    import fire

    fire.Fire(_cli_check)
