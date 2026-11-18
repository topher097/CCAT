"""Backward-compat shim: the engine moved to ``ccat.engines`` (Phase 1a).

The ZeroBounce engine implementation now lives in
:mod:`ccat.engines.zerobounce_email_check`; this shim keeps the script
runnable standalone (``python scripts/zerobounce_email_check.py ...``)
and keeps flat imports of ``zerobounce_email_check`` working by
re-exporting the package version.
"""

import sys
from pathlib import Path

try:
    from ccat.engines.zerobounce_email_check import *  # noqa: F403
    from ccat.engines.zerobounce_email_check import (  # noqa: F401
        OutOfCreditsError,
        bulk_validate_emails,
        get_balance,
        validate_email,
        verify_email,
    )
except ImportError:
    # ccat is not installed (e.g. run from a bare scripts/ checkout):
    # bootstrap the package source tree onto sys.path and retry.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from ccat.engines.zerobounce_email_check import *  # noqa: F403
    from ccat.engines.zerobounce_email_check import (  # noqa: F401
        OutOfCreditsError,
        bulk_validate_emails,
        get_balance,
        validate_email,
        verify_email,
    )

if __name__ == "__main__":
    try:
        import fire

        fire.Fire({"validate": verify_email, "balance": get_balance})
    except OutOfCreditsError as error:
        print(f"ERROR: {error}")
        sys.exit(2)
