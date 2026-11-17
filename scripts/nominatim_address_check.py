"""Validate a reported US address against the OSM Nominatim API.

Sends the street / apt / city / state / ZIP parts as one free-text query
to a Nominatim ``/search`` endpoint (OpenStreetMap's free geocoder, no
API key) and reports whether the address exists on the map:

    found        - exactly one match; display_name/lat/lon point at it.
    ambiguous    - several distinct matches; human review decides.
    not_found    - no match at all.
    error        - the API call failed (status kept for retry).

The ENTIRE verbatim Nominatim JSON response is always included in the
result as ``response_json``, so nothing the API sent is lost.

Usage policy (https://operations.osmfoundation.org/policies/nominatim/):
at most 1 request per second with a meaningful ``User-Agent``.  The
caller (the screening stage) rate-limits; this module just adds retry
with backoff on 429/5xx.
"""

from __future__ import annotations

import json
import time
from typing import Any

import fire
import requests

DEFAULT_BASE_URL = "https://nominatim.openstreetmap.org"
DEFAULT_USER_AGENT = "ccat-angel-tree/1.0 (family address validation)"

#: HTTP statuses that get a retry with backoff (rate limit / transient).
_RETRY_STATUSES = (429, 500, 502, 503, 504)


def build_query(
    street: str | None,
    apt_floor: str | None = None,
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
) -> str:
    """Build the free-text Nominatim query from address parts.

    Args:
        street (str | None): Street address line.
        apt_floor (str | None): Apartment / floor line (appended after
            the street, before the city).
        city (str | None): City or town.
        state (str | None): State name (e.g. "Michigan").
        zip_code (str | None): ZIP code.

    Returns:
        str: The comma-joined query text (stripped parts only).
    """
    parts = [street, apt_floor, city, state, zip_code]
    return ", ".join(part.strip() for part in parts if part and part.strip())


def check_address(
    street: str | None,
    apt_floor: str | None = None,
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 30.0,
    retries: int = 2,
) -> dict[str, Any]:
    """Check one address against Nominatim and return the full verdict.

    Args:
        street (str | None): Street address line.
        apt_floor (str | None): Apartment / floor line.
        city (str | None): City or town.
        state (str | None): State name.
        zip_code (str | None): ZIP code.
        base_url (str): Nominatim root URL (self-hosted instances work).
        user_agent (str): Identifying User-Agent header (required by the
            usage policy).
        timeout (float): Per-request timeout in seconds.
        retries (int): Extra attempts on 429/5xx before giving up.

    Returns:
        dict[str, Any]: Keys ``query``, ``status`` (found / ambiguous /
        not_found / error), ``display_name``, ``lat``, ``lon``,
        ``result_count``, and ``response_json`` -- the ENTIRE verbatim
        Nominatim JSON response (the full result list).  On an HTTP
        error ``response_json`` carries the status/text instead.
    """
    query = build_query(street, apt_floor, city, state, zip_code)
    result: dict[str, Any] = {
        "query": query,
        "status": "error",
        "display_name": None,
        "lat": None,
        "lon": None,
        "result_count": 0,
        "response_json": None,
    }
    if not query:
        result["status"] = "not_found"
        result["response_json"] = json.dumps([])
        return result
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    params = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 10,
        "countrycodes": "us",
    }
    response: requests.Response | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(
                f"{base_url.rstrip('/')}/search",
                params=params,
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException as error:
            result["response_json"] = json.dumps(
                {"error": f"request failed: {error}"}
            )
            return result
        if response.status_code not in _RETRY_STATUSES:
            break
        if attempt < retries:
            time.sleep(2.0 * (attempt + 1))
    if response is None:  # pragma: no cover - loop always assigns
        return result
    if response.status_code != 200:
        result["response_json"] = json.dumps(
            {
                "error": f"HTTP {response.status_code}",
                "body": response.text[:2000],
            }
        )
        return result
    try:
        matches = response.json()
    except ValueError as error:
        result["response_json"] = json.dumps(
            {"error": f"invalid JSON response: {error}"}
        )
        return result
    result["response_json"] = json.dumps(matches, ensure_ascii=False)
    if not isinstance(matches, list):  # pragma: no cover - API contract
        result["status"] = "error"
        return result
    result["result_count"] = len(matches)
    if not matches:
        result["status"] = "not_found"
        return result
    result["status"] = "found" if len(matches) == 1 else "ambiguous"
    first = matches[0]
    result["display_name"] = first.get("display_name")
    result["lat"] = first.get("lat")
    result["lon"] = first.get("lon")
    return result


def _cli_check(
    street: str,
    apt_floor: str | None = None,
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> None:
    """CLI entry point: check one address and print the verdict."""
    result = check_address(
        street,
        apt_floor=apt_floor,
        city=city,
        state=state,
        zip_code=zip_code,
        base_url=base_url,
    )
    suffix = f" ({result['result_count']} match(es))" if result else ""
    print(f"{result['query']} -> {result['status']}{suffix}")
    if result["display_name"]:
        print(f"  {result['display_name']}")
        print(f"  lat/lon: {result['lat']}, {result['lon']}")
    print(result["response_json"])


if __name__ == "__main__":
    fire.Fire(_cli_check)
