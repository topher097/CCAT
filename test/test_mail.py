"""Mail message construction and Gmail transport outcome tests."""

from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from ccat.mail import GmailTransport, MailError, build_raw_message
from ccat.services.ports import Attachment, SendRequest


def _request() -> SendRequest:
    """Build one request with fixed headers and two attachments.

    Returns:
        The outgoing mail request used by the transport tests.
    """
    return SendRequest(
        sender="sender@example.com",
        sender_name="Dispatch Team",
        reply_to="replies@example.com",
        to="sponsor@example.com",
        bcc=("archive@example.com", "audit@example.com"),
        subject="Your Angel packet",
        html_body="<html><body><p>Dear Mary</p></body></html>",
        message_id="<fixed-message-id@ccat.local>",
        attachments=(
            Attachment(
                "child-information.pdf",
                "application/pdf",
                b"packet bytes",
                "packet-hash",
            ),
            Attachment(
                "instructions.pdf",
                "application/pdf",
                b"instruction bytes",
                "instructions-hash",
            ),
        ),
    )


class _FakeService:
    """Small stand-in for Gmail's chained service resource."""

    def __init__(self, result: dict[str, Any] | Exception) -> None:
        """Store the response or exception produced by ``execute``.

        Args:
            result: Provider response, or the error to raise.
        """
        self.result = result
        self.user_id = ""
        self.body: dict[str, Any] = {}

    def users(self) -> _FakeService:
        """Return the users resource.

        Returns:
            This fake resource.
        """
        return self

    def messages(self) -> _FakeService:
        """Return the messages resource.

        Returns:
            This fake resource.
        """
        return self

    def send(self, *, userId: str, body: dict[str, Any]) -> _FakeService:  # noqa: N803 - the Gmail API's parameter name
        """Capture the arguments of the provider send call.

        Args:
            userId: Gmail's sending account identifier.
            body: The base64url-encoded RFC 5322 message.

        Returns:
            A fake executable request.
        """
        self.user_id = userId
        self.body = body
        return self

    def execute(self) -> dict[str, Any]:
        """Return the configured response or raise the configured error.

        Returns:
            The fake Gmail response.

        Raises:
            Exception: The configured provider or transport failure.
        """
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _transport(
    monkeypatch: pytest.MonkeyPatch, service: _FakeService
) -> GmailTransport:
    """Build a Gmail transport whose service factory is faked.

    Args:
        monkeypatch: Pytest's patch fixture.
        service: The fake Gmail resource.

    Returns:
        The transport under test.
    """
    transport = GmailTransport(Path("client.json"), Path("token.json"))
    monkeypatch.setattr(transport, "_service", lambda: service)
    return transport


def test_build_raw_message_has_headers_html_and_attachments() -> None:
    """The MIME message preserves addressing, body, and file bytes."""
    raw = build_raw_message(_request())
    message = BytesParser(policy=policy.default).parsebytes(raw)

    assert message["From"] == "Dispatch Team <sender@example.com>"
    assert message["To"] == "sponsor@example.com"
    assert message["Bcc"] == "archive@example.com, audit@example.com"
    assert message["Reply-To"] == "replies@example.com"
    assert message["Subject"] == "Your Angel packet"
    assert message["Message-ID"] == "<fixed-message-id@ccat.local>"

    html = [
        part
        for part in message.walk()
        if part.get_content_type() == "text/html"
    ]
    assert len(html) == 1
    assert "<p>Dear Mary</p>" in html[0].get_content()
    attachments = list(message.iter_attachments())
    assert [part.get_filename() for part in attachments] == [
        "child-information.pdf",
        "instructions.pdf",
    ]
    assert [part.get_payload(decode=True) for part in attachments] == [
        b"packet bytes",
        b"instruction bytes",
    ]
    assert [part.get_content_type() for part in attachments] == [
        "application/pdf",
        "application/pdf",
    ]


def test_gmail_send_returns_provider_id_and_raw_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted provider call is sent and carries the provider id."""
    service = _FakeService({"id": "gmail-message-123"})
    transport = _transport(monkeypatch, service)

    outcome = transport.send(_request())

    assert outcome.state == "sent"
    assert outcome.provider_message_id == "gmail-message-123"
    assert outcome.error == ""
    assert service.user_id == "me"
    raw = base64.urlsafe_b64decode(service.body["raw"])
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert message["Message-ID"] == "<fixed-message-id@ccat.local>"
    assert len(list(message.iter_attachments())) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_gmail_provider_refusals_are_rejected(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A provider refusal is retryable and never marked ambiguous.

    Args:
        monkeypatch: Pytest's patch fixture.
        status: The provider's refusal status.
    """
    error = HttpError(Response({"status": status}), b"refused")
    transport = _transport(monkeypatch, _FakeService(error))

    outcome = transport.send(_request())

    assert outcome.state == "rejected"
    assert outcome.provider_message_id == ""
    assert str(status) in outcome.error


@pytest.mark.parametrize("status", [500, 502, 503])
def test_gmail_server_errors_are_ambiguous(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A server error does not prove that Gmail refused the message.

    Args:
        monkeypatch: Pytest's patch fixture.
        status: The provider's server-error status.
    """
    error = HttpError(Response({"status": status}), b"server error")
    transport = _transport(monkeypatch, _FakeService(error))

    outcome = transport.send(_request())

    assert outcome.state == "ambiguous"
    assert outcome.provider_message_id == ""
    assert str(status) in outcome.error


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(TimeoutError("timed out"), id="timeout"),
        pytest.param(TimeoutError("timed out"), id="socket-timeout"),
        pytest.param(ConnectionResetError("dropped"), id="dropped-connection"),
    ],
)
def test_gmail_network_failures_are_ambiguous(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Timeouts and dropped connections never resemble refusals.

    Args:
        monkeypatch: Pytest's patch fixture.
        error: The simulated network failure.
    """
    transport = _transport(monkeypatch, _FakeService(error))

    outcome = transport.send(_request())

    assert outcome.state == "ambiguous"
    assert outcome.provider_message_id == ""
    assert "did not answer" in outcome.error


def test_missing_token_file_means_account_not_authorized(
    tmp_path: Path,
) -> None:
    """Sending without the Gmail token raises a configuration error.

    Args:
        tmp_path: The test's isolated temporary directory.
    """
    transport = GmailTransport(
        tmp_path / "client.json", tmp_path / "missing-token.json"
    )

    with pytest.raises(MailError, match="not authorized"):
        transport.send(_request())
