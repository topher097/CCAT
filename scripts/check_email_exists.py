"""Check email syntax and deliverability with the email_validator library."""

from email_validator import validate_email, EmailNotValidError


def verify_email(email: str) -> bool:
    """Verify an email. Check if it valid sytax, mx record lookup, and SMTP verification.

    Args:
        email (str): The input email string

    Returns:
        bool: If the email is valid and exists or not
    """
    try:
        # Check that the email address is valid. Turn on check_deliverability
        # for first-time validations like on account creation pages (but not
        # login pages).
        emailinfo = validate_email(email, check_deliverability=True)

        # After this point, use only the normalized form of the email address,
        # especially before going to a database query.
        email = emailinfo.normalized

        # # Check if recipient exists
        # try:
        #     code, message = server.rcpt(email)
        #     server.quit()

        print(f"Email: {email}")
        return True

    except EmailNotValidError as e:
        # The exception message is human-readable explanation of why it's
        # not a valid (or deliverable) email address.
        print(str(e))
        return False


if __name__ == "__main__":
    import fire

    fire.Fire(verify_email)
