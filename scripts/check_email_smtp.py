"""Confirm email deliverability with a live SMTP RCPT TO probe.

Connects to the recipient domain's MX server, issues MAIL FROM / RCPT TO,
and interprets the reply. Sends no message data (no DATA command).

Caveats:
    - Results depend on the connecting IP's reputation. Throttling and
      policy blocks return 4xx/5.7.1 codes that are inconclusive, not
      proof of (non-)existence.
    - Repeated probes get an IP blocked. Use sparingly.
    - Catch-all domains accept every local part, so a 250 there proves
      nothing. gmail.com is not catch-all: 550 5.1.1 means the account
      does not exist.
"""

import smtplib
import socket

import dns.resolver
import fire


def _primary_mx(domain: str) -> str:
    """Resolve the primary (lowest preference) MX host for a domain.

    Args:
        domain (str): The domain to look up.

    Returns:
        str: Hostname of the primary mail exchanger.

    Raises:
        dns.resolver.NXDOMAIN: Domain does not exist.
        dns.resolver.NoAnswer: Domain has no MX records.
    """
    records = dns.resolver.resolve(domain, "MX")
    mx = min(records, key=lambda record: record.preference)
    return str(mx.exchange).rstrip(".")


def _classify(code: int, message: str) -> tuple[str, str]:
    """Map an SMTP RCPT reply to a deliverability verdict.

    Args:
        code (int): SMTP reply code (e.g. 250, 550).
        message (str): SMTP reply text.

    Returns:
        tuple[str, str]: (verdict, explanation). Verdict is one of
        DELIVERABLE, UNDELIVERABLE, INCONCLUSIVE.
    """
    text = message.lower()
    if code in (250, 251):
        return "DELIVERABLE", "Recipient accepted by the mail server."
    if code in (550, 551, 553) and (
        "5.1.1" in text
        or "user unknown" in text
        or "does not exist" in text
        or "no such user" in text
        or "bad recipient" in text
    ):
        return "UNDELIVERABLE", "Mailbox does not exist (5.1.1 unknown user)."
    if 400 <= code < 500:
        return (
            "INCONCLUSIVE",
            f"Temporary refusal ({code}); likely greylisting or rate limiting. Retry later.",
        )
    if code in (550, 551) and (
        "5.7.1" in text
        or "unsolicited" in text
        or "blocked" in text
        or "spam" in text
        or "policy" in text
    ):
        return (
            "INCONCLUSIVE",
            f"Policy block ({code}); server distrusts this connection, not the mailbox.",
        )
    return "INCONCLUSIVE", f"Refused with {code}: {message.strip()}"


def verify_email_smtp(
    email: str,
    mail_from: str = "postmaster@example.com",
    helo: str | None = None,
    timeout: float = 30.0,
) -> bool:
    """Probe an email's deliverability with a live SMTP RCPT TO check.

    Args:
        email (str): The recipient email address to check.
        mail_from (str): Envelope sender used in MAIL FROM. Use a real
            address on a domain you control for best results.
        helo (str | None): EHLO hostname. Defaults to this machine's FQDN.
        timeout (float): Per-operation timeout in seconds.

    Returns:
        bool: True only when the MX server accepted the recipient (250/251).
            False for rejections and inconclusive results; read the printed
            verdict to distinguish them.
    """
    domain = email.rsplit("@", 1)[-1]
    try:
        mx_host = _primary_mx(domain)
    except (
        dns.resolver.NXDOMAIN,
        dns.resolver.NoAnswer,
        dns.resolver.LifetimeTimeout,
    ) as error:
        print(f"MX lookup failed for {domain}: {error}")
        return False

    print(f"MX: {mx_host}")
    helo_name = helo or socket.getfqdn()

    try:
        with smtplib.SMTP(
            mx_host, 25, timeout=timeout, local_hostname=helo_name
        ) as server:
            server.ehlo()
            server.mail(mail_from)
            try:
                code, message = server.rcpt(email)
            except smtplib.SMTPRecipientsRefused as error:
                code, message = error.recipients[email]
            server.rset()

        text = (
            message.decode(errors="replace")
            if isinstance(message, bytes)
            else message
        )
        verdict, reason = _classify(code, text)
        print(f"Email: {email}")
        print(
            f"Reply: {code} {message.decode() if isinstance(message, bytes) else message}"
        )
        print(f"Verdict: {verdict} - {reason}")
        return verdict == "DELIVERABLE"

    except (
        smtplib.SMTPServerDisconnected,
        smtplib.SMTPHeloError,
        smtplib.SMTPConnectError,
    ) as error:
        print(f"Server refused the connection: {error}")
        print("Verdict: INCONCLUSIVE - no RCPT reply obtained.")
        return False
    except OSError as error:
        print(f"Connection failed (port 25 blocked or filtered?): {error}")
        print("Verdict: INCONCLUSIVE - no RCPT reply obtained.")
        return False


if __name__ == "__main__":
    fire.Fire(verify_email_smtp)
