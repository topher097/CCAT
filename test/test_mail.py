"""Mail message construction and Gmail transport outcome tests."""

from __future__ import annotations

import base64
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import pytest
from conftest import load_test_cfg
from googleapiclient.errors import HttpError
from httplib2 import Response

from ccat.api.routes import dispatch as dispatch_routes
from ccat.mail import (
    GMAIL_SEND_SCOPE,
    GmailTransport,
    MailError,
    ServiceAccountGmailTransport,
    build_raw_message,
    decode_service_account_key,
)
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


# ---------------------------------------------------------------------------
# Service account credentials.
# ---------------------------------------------------------------------------


def _service_account_key() -> str:
    """Build a base64 service account key for the credential tests.

    Returns:
        The base64 text of a service account key.
    """
    info = {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "key-id",
        "private_key": "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n",
        "client_email": "mailer@test-project.iam.gserviceaccount.com",
        "client_id": "1234567890",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return base64.b64encode(json.dumps(info).encode("utf-8")).decode("ascii")


class _FakeCredentials:
    """Stand-in for the service account credentials."""

    def __init__(self) -> None:
        """Record the impersonated subject, if any."""
        self.subject = ""

    def with_subject(self, subject: str) -> _FakeCredentials:
        """Return credentials that impersonate one user.

        Args:
            subject: The user to impersonate.

        Returns:
            The impersonating credentials.
        """
        self.subject = subject
        return self


def test_service_account_key_decodes_from_base64() -> None:
    """The base64 key becomes the JSON Google issued."""
    info = decode_service_account_key(_service_account_key())

    assert info["type"] == "service_account"
    assert info["client_email"] == "mailer@test-project.iam.gserviceaccount.com"


@pytest.mark.parametrize(
    "encoded",
    [
        pytest.param("not base64 !!", id="not-base64"),
        pytest.param(
            base64.b64encode(b"not json").decode("ascii"), id="not-json"
        ),
        pytest.param(
            base64.b64encode(b'{"type": "authorized_user"}').decode("ascii"),
            id="not-a-service-account",
        ),
        pytest.param(
            base64.b64encode(b'{"type": "service_account"}').decode("ascii"),
            id="missing-fields",
        ),
    ],
)
def test_service_account_key_rejects_bad_input(encoded: str) -> None:
    """A key that is not a usable service account key is refused.

    Args:
        encoded: The candidate key text.
    """
    with pytest.raises(MailError):
        decode_service_account_key(encoded)


def test_service_account_transport_sends_with_the_decoded_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment key becomes credentials and the message goes out."""
    captured: dict[str, Any] = {}
    service = _FakeService({"id": "gmail-message-9"})

    def fake_from_info(
        info: dict[str, Any], scopes: list[str]
    ) -> _FakeCredentials:
        """Record what the transport asked Google for.

        Args:
            info: The decoded key.
            scopes: The requested scopes.

        Returns:
            The fake credentials.
        """
        captured["info"] = info
        captured["scopes"] = list(scopes)
        return _FakeCredentials()

    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY", _service_account_key())
    monkeypatch.setattr(
        "ccat.mail.service_account.Credentials.from_service_account_info",
        staticmethod(fake_from_info),
    )
    monkeypatch.setattr("ccat.mail.build", lambda *args, **kwargs: service)

    outcome = ServiceAccountGmailTransport().send(_request())

    assert outcome.state == "sent"
    assert outcome.provider_message_id == "gmail-message-9"
    assert captured["info"]["client_email"] == (
        "mailer@test-project.iam.gserviceaccount.com"
    )
    assert captured["scopes"] == [GMAIL_SEND_SCOPE]
    assert service.user_id == "me"


def test_service_account_transport_impersonates_the_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured subject is applied to the credentials.

    Args:
        monkeypatch: Pytest's patch fixture.
    """
    credentials = _FakeCredentials()
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY", _service_account_key())
    monkeypatch.setattr(
        "ccat.mail.service_account.Credentials.from_service_account_info",
        staticmethod(lambda info, scopes: credentials),
    )
    monkeypatch.setattr(
        "ccat.mail.build", lambda *args, **kwargs: _FakeService({"id": "x"})
    )

    outcome = ServiceAccountGmailTransport(
        subject="coordinator@example.org"
    ).send(_request())

    assert outcome.state == "sent"
    assert credentials.subject == "coordinator@example.org"


def test_service_account_transport_needs_the_key_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the key the transport refuses to send.

    Args:
        monkeypatch: Pytest's patch fixture.
    """
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_KEY", raising=False)

    with pytest.raises(MailError, match="GOOGLE_SERVICE_ACCOUNT_KEY"):
        ServiceAccountGmailTransport().send(_request())


def test_transport_selection_follows_the_configured_credential_source() -> None:
    """The configured credential source picks the transport."""
    cfg = load_test_cfg()

    assert isinstance(dispatch_routes._transport(cfg), GmailTransport)

    service_account_cfg = cfg.model_copy(
        update={
            "dispatch": cfg.dispatch.model_copy(
                update={
                    "credential_source": "service_account",
                    "service_account_subject": "coordinator@example.org",
                }
            )
        }
    )

    assert isinstance(
        dispatch_routes._transport(service_account_cfg),
        ServiceAccountGmailTransport,
    )
