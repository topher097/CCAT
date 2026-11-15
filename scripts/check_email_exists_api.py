"""Verify emails via the free Rapid Email Validator API."""

import requests

API_URL = "https://rapid-email-verifier.fly.dev/api/validate"


def verify_email(email: str, timeout: float = 10.0) -> bool:
    """Verify an email via the free Rapid Email Validator API.

    Checks syntax, DNS/MX records, disposable domains, and role addresses.

    Note: the API never checks SMTP RCPT TO. "VALID" means the address is
    syntactically valid, the domain can accept mail, and it is not a known
    disposable. It does not prove a specific inbox exists.

    Args:
        email (str): The input email string.
        timeout (float): Request timeout in seconds.

    Returns:
        bool: True if the API reports status "VALID", False otherwise.
    """
    try:
        response = requests.get(
            API_URL,
            params={"email": email},
            timeout=timeout,
        )
        response.raise_for_status()

        result = response.json()
        email = result["email"]
        status = result["status"]
        validations = result.get("validations", {})

        print(f"Email: {email}")
        print(f"Status: {status}")
        print(f"Score: {result.get('score')}")
        print(f"Syntax: {validations.get('syntax')}")
        print(f"Domain exists: {validations.get('domain_exists')}")
        print(f"MX records: {validations.get('mx_records')}")
        print(
            f"Mailbox exists (domain accepts mail): {validations.get('mailbox_exists')}"
        )
        print(f"Disposable: {validations.get('is_disposable')}")
        print(f"Role-based: {validations.get('is_role_based')}")
        if "aliasOf" in result:
            print(f"Alias of: {result['aliasOf']}")

        return status == "VALID"

    except requests.exceptions.HTTPError as error:
        print(f"HTTP error: {error}")
        return False
    except requests.exceptions.RequestException as error:
        print(f"Request error: {error}")
        return False
    except (KeyError, ValueError) as error:
        print(f"Unexpected API response: {error}")
        return False


if __name__ == "__main__":
    import fire

    fire.Fire(verify_email)
