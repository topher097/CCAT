"""Verify email deliverability with the official ZeroBounce SDK.

Uses real SMTP-level validation: ZeroBounce confirms whether the mailbox
exists, unlike syntax/MX-only checkers. For gmail.com addresses, an
undeliverable inbox returns status "invalid" with sub_status
"mailbox_not_found".

Status reference:
    valid        - Deliverable. Mailbox exists.
    invalid      - Undeliverable. Sub-status says why (mailbox_not_found,
                   no_dns_entries, failed_syntax_check, ...).
    catch_all    - Domain accepts every local part; existence unknown.
    unknown      - Could not be determined (greylisting, timeouts, ...).
    spamtrap     - Spam trap address.
    abuse        - Known abusive/complaint address.
    do_not_mail  - Disposable, toxic, or globally suppressed.

The API key is read from the ZEROBOUNCE_API_KEY environment variable
unless passed as an argument.
"""

import os
import sys

import fire
from zerobouncesdk import ZBException, ZeroBounce

STATUS_MEANINGS = {
    "valid": "Deliverable - mailbox exists.",
    "invalid": "Undeliverable - mailbox or domain rejected it.",
    "catch_all": "Domain accepts all mail; inbox existence unknowable.",
    "unknown": "Could not determine; greylisting or timeout likely.",
    "spamtrap": "Spam trap address; do not mail.",
    "abuse": "Known complainer/abuse address; do not mail.",
    "do_not_mail": "Disposable, toxic, or suppressed; do not mail.",
}


class OutOfCreditsError(Exception):
    """Raised when the ZeroBounce account has no validation credits left."""


def _resolve_key(api_key: str | None) -> str | None:
    """Resolve the ZeroBounce API key from an argument or the environment.

    Args:
        api_key (str | None): Explicit API key, or None to fall back to
            the ZEROBOUNCE_API_KEY environment variable.

    Returns:
        str | None: The resolved key, or None (with a printed message)
            when neither source provides one.
    """
    key = api_key or os.environ.get("ZEROBOUNCE_API_KEY")
    if not key:
        print("No API key: pass api_key or set ZEROBOUNCE_API_KEY.")
    return key


def validate_email(
    email: str,
    api_key: str | None = None,
    ip_address: str | None = None,
    retry_greylist: bool = False,
) -> dict[str, str | bool] | None:
    """Validate one email and return the ZeroBounce result as a dict.

    Args:
        email (str): The email address to validate.
        api_key (str | None): ZeroBounce API key. Defaults to the
            ZEROBOUNCE_API_KEY environment variable.
        ip_address (str | None): Signup IP for the address, if known.
            Improves fraud/abuse scoring.
        retry_greylist (bool): Ask ZeroBounce to keep processing a
            greylisted address instead of returning "unknown" immediately.

    Returns:
        dict[str, str | bool] | None: Keys: address, status, sub_status,
        did_you_mean, mx_found, smtp_provider, free_email, name,
        processed_at. A "greylist_pending" status means ZeroBounce is
        still retrying and the result is not final. None when the API
        call failed (the error is printed).

    Raises:
        OutOfCreditsError: The account has no validation credits left.
    """
    key = _resolve_key(api_key)
    if not key:
        return None

    zero_bounce = ZeroBounce(key)

    try:
        credits_value = zero_bounce.get_credits().credits
        # The API may return the balance as a string (e.g. "1234").
        credits = int(float(str(credits_value)))
    except ZBException as error:
        print(
            f"Could not check credits (API key invalid or unreachable?): {error}"
        )
        return None
    except (TypeError, ValueError):
        print(f"Could not read credit balance ({credits_value!r}); continuing.")
    else:
        if credits <= 0:
            raise OutOfCreditsError(
                f"ZeroBounce account is out of credits ({credits} remaining). "
                "Top up at https://www.zerobounce.net/ before validating emails."
            )

    try:
        # Note: the SDK annotates ip_address as str with a default of None,
        # so it is passed only when set to satisfy the type checker.
        response = (
            zero_bounce.validate(
                email,
                ip_address=ip_address,
                timeout=60 if retry_greylist else None,
                greylist_processing=True if retry_greylist else None,
                use_post=True,
            )
            if ip_address
            else zero_bounce.validate(
                email,
                timeout=60 if retry_greylist else None,
                greylist_processing=True if retry_greylist else None,
                use_post=True,
            )
        )

        if getattr(response, "message", None):
            return {
                "address": email,
                "status": "greylist_pending",
                "sub_status": "",
                "did_you_mean": "",
                "mx_found": False,
                "smtp_provider": "",
                "free_email": False,
                "name": "",
                "processed_at": "",
                "message": str(response.message),
            }

        # status/sub_status are enums in SDK 3.x; fall back to raw values.
        status = str(getattr(response.status, "value", response.status)).lower()
        sub_status = str(
            getattr(response.sub_status, "value", response.sub_status) or ""
        ).lower()
        name = f"{response.firstname or ''} {response.lastname or ''}".strip()

        return {
            "address": response.address,
            "status": status,
            "sub_status": sub_status,
            "did_you_mean": str(response.did_you_mean or ""),
            "mx_found": bool(response.mx_found),
            "smtp_provider": str(response.smtp_provider or ""),
            "free_email": bool(response.free_email),
            "name": name,
            "processed_at": str(response.processed_at or ""),
        }

    except ZBException as error:
        if "credit" in str(error).lower():
            raise OutOfCreditsError(
                f"ZeroBounce ran out of credits during validation: {error}"
            ) from error
        print(f"ZeroBounce error: {error}")
        return None


def verify_email(
    email: str,
    api_key: str | None = None,
    ip_address: str | None = None,
    retry_greylist: bool = False,
) -> bool:
    """Verify an email with the ZeroBounce API.

    Args:
        email (str): The email address to verify.
        api_key (str | None): ZeroBounce API key. Defaults to the
            ZEROBOUNCE_API_KEY environment variable.
        ip_address (str | None): Signup IP for the address, if known.
            Improves fraud/abuse scoring.
        retry_greylist (bool): Ask ZeroBounce to keep processing a
            greylisted address instead of returning "unknown" immediately.

    Returns:
        bool: True only when ZeroBounce reports status "valid". False for
            every other status; read the printed status to distinguish
            "confirmed undeliverable" from "could not determine".

    Raises:
        OutOfCreditsError: The account has no validation credits left.
    """
    result = validate_email(
        email,
        api_key=api_key,
        ip_address=ip_address,
        retry_greylist=retry_greylist,
    )
    if result is None:
        return False

    if result["status"] == "greylist_pending":
        print(f"Greylist retry in progress: {result['message']}")
        print("Verdict: INCONCLUSIVE - call again in about 30 minutes.")
        return False

    print(f"Email: {result['address']}")
    print(
        f"Status: {result['status']} - "
        f"{STATUS_MEANINGS.get(str(result['status']), 'Unknown status.')}"
    )
    print(f"Sub-status: {result['sub_status'] or 'none'}")
    print(f"Did you mean: {result['did_you_mean'] or 'n/a'}")
    print(f"MX found: {result['mx_found']}")
    print(f"SMTP provider: {result['smtp_provider']}")
    print(f"Free email: {result['free_email']}")
    if result["name"]:
        print(f"Name: {result['name']}")
    print(f"Processed at: {result['processed_at']}")

    return result["status"] == "valid"


def get_balance(api_key: str | None = None) -> int:
    """Retrieve the current ZeroBounce credit balance.

    Args:
        api_key (str | None): ZeroBounce API key. Defaults to the
            ZEROBOUNCE_API_KEY environment variable.

    Returns:
        int: Remaining validation credits, or -1 when the balance could
            not be retrieved (bad key, API error, unreadable balance).
    """
    key = _resolve_key(api_key)
    if not key:
        return -1

    try:
        # The API may return the balance as a string (e.g. "1234").
        credits = int(float(str(ZeroBounce(key).get_credits().credits)))
    except ZBException as error:
        print(f"ZeroBounce error: {error}")
        return -1
    except (TypeError, ValueError) as error:
        print(f"Could not read credit balance: {error}")
        return -1

    # The CLI prints the returned value once; no explicit print here.
    return credits


if __name__ == "__main__":
    try:
        fire.Fire({"validate": verify_email, "balance": get_balance})
    except OutOfCreditsError as error:
        print(f"ERROR: {error}")
        sys.exit(2)
