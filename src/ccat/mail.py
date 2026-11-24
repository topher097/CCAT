"""Send sponsor packet email through the Gmail API.

The dispatch page composes one message per sponsor and hands it to a
transport.  This module is the Gmail implementation of the
:class:`ccat.services.ports.MailTransport` protocol.

Setup mirrors ``ccat.gdrive``: the OAuth **client** file is the existing
Desktop-app client, and the Gmail **token** is a separate file that
carries the ``gmail.send`` scope.  The Drive token is never touched.

``ccat mail-authorize`` performs the one-time consent.  It never runs
inside a request: the OAuth flow opens a browser, which a headless host
cannot do.

Outcomes are three-valued.  A provider that *refuses* a message
(``rejected``) did not accept it, so a retry is safe.  A timeout, a
dropped connection, or an ambiguous server error (``ambiguous``) does
not say whether the message was accepted, so it must never be retried
automatically.
"""

import base64
import binascii
import json
import logging
import os
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ccat.services.ports import MailError, SendOutcome, SendRequest

logger = logging.getLogger(__name__)

#: The only scope a sending account needs.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

#: The environment variable holding the base64 service account key.
SERVICE_ACCOUNT_KEY_ENV = "GOOGLE_SERVICE_ACCOUNT_KEY"

#: Provider statuses that prove the message was not accepted.
_REJECTED_STATUSES = frozenset({400, 401, 403, 404, 429})


def build_raw_message(request: SendRequest) -> bytes:
    """Build the RFC 5322 bytes of one message.

    Args:
        request: The message to build.

    Returns:
        The raw message bytes, ready for base64url encoding.
    """
    message = EmailMessage()
    message["From"] = formataddr((request.sender_name, request.sender))
    message["To"] = request.to
    if request.bcc:
        message["Bcc"] = ", ".join(request.bcc)
    if request.reply_to:
        message["Reply-To"] = request.reply_to
    message["Subject"] = request.subject
    message["Message-ID"] = request.message_id
    message.set_content(request.html_body, subtype="html")
    for attachment in request.attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        message.add_attachment(
            attachment.payload,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return message.as_bytes()


def decode_service_account_key(encoded: str) -> dict[str, Any]:
    """Decode a base64 service account key into its JSON mapping.

    The key is stored base64-encoded so it survives a shell environment
    file; this turns it back into the JSON object Google issued.

    Args:
        encoded: The base64 text of the service account key file.

    Returns:
        The decoded key.

    Raises:
        MailError: When the text is not base64, not JSON, or not a
            service account key.
    """
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise MailError(
            f"the service account key is not valid base64: {error}"
        ) from error
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise MailError(
            f"the service account key is not valid JSON: {error}"
        ) from error
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise MailError("the service account key is not a service account key")
    if not info.get("client_email") or not info.get("private_key"):
        raise MailError(
            "the service account key is missing client_email or private_key"
        )
    return info


def _gmail_service(credentials: Credentials, timeout: float) -> Any:
    """Build a Gmail service with a per-request timeout.

    Args:
        credentials: The credentials the service authenticates with.
        timeout: The per-request timeout in seconds.

    Returns:
        The Gmail v1 service resource.

    Raises:
        MailError: When the service cannot be built.
    """
    try:
        service = build(
            "gmail",
            "v1",
            credentials=credentials,
            cache_discovery=False,
        )
    except (GoogleAuthError, OSError) as error:
        raise MailError(f"the mail service is unavailable: {error}") from error
    http = getattr(service, "_http", None)
    if http is not None and hasattr(http, "timeout"):
        http.timeout = timeout
    return service


def _send_via(service: Any, request: SendRequest) -> SendOutcome:
    """Send one message through an already-built Gmail service.

    Args:
        service: The Gmail v1 service resource.
        request: The message to send.

    Returns:
        The provider's outcome: "sent", "rejected", or "ambiguous".
    """
    raw = base64.urlsafe_b64encode(build_raw_message(request)).decode("ascii")
    try:
        response = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
    except HttpError as error:
        status = int(getattr(error.resp, "status", 0) or 0)
        if status in _REJECTED_STATUSES:
            return SendOutcome(
                "rejected",
                "",
                f"the mail provider refused the message ({status})",
            )
        return SendOutcome(
            "ambiguous", "", f"the mail provider answered with {status}"
        )
    except (TimeoutError, OSError):
        return SendOutcome("ambiguous", "", "the mail provider did not answer")
    return SendOutcome("sent", str(response.get("id", "")), "")


def _write_token(credentials: Credentials, token_file: Path) -> None:
    """Store a token file atomically.

    Args:
        credentials: The credentials to store.
        token_file: Where to write them.
    """
    token_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = token_file.with_name(token_file.name + ".tmp")
    temporary.write_text(credentials.to_json(), encoding="utf-8")
    os.replace(temporary, token_file)


def authorize(client_file: Path, token_file: Path) -> str:
    """Run the one-time OAuth consent for the sending account.

    Args:
        client_file: The OAuth client secret file.
        token_file: Where the Gmail token is written.

    Returns:
        The authorized account address, when the provider reports one.

    Raises:
        MailError: When the client file is missing or the consent fails.
    """
    if not Path(client_file).is_file():
        raise MailError(f"the OAuth client file {client_file} does not exist")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_file), [GMAIL_SEND_SCOPE]
        )
        credentials = flow.run_local_server(port=0)
    except (GoogleAuthError, OSError, ValueError) as error:
        raise MailError(f"the mail consent failed: {error}") from error
    _write_token(credentials, Path(token_file))
    return str(getattr(credentials, "account", "") or "")


class GmailTransport:
    """Send messages with the Gmail API."""

    def __init__(
        self,
        client_file: Path,
        token_file: Path,
        timeout: float = 30.0,
    ) -> None:
        """Build the transport.

        Args:
            client_file: The OAuth client secret file.
            token_file: The Gmail token file.
            timeout: The per-request timeout in seconds.
        """
        self._client_file = Path(client_file)
        self._token_file = Path(token_file)
        self._timeout = timeout

    def _credentials(self) -> Credentials:
        """Load the sending account's credentials, refreshing when stale.

        Returns:
            Valid credentials.

        Raises:
            MailError: When the account is not authorized.
        """
        if not self._token_file.is_file():
            raise MailError(
                "the mail account is not authorized; run `ccat mail-authorize`"
            )
        credentials = Credentials.from_authorized_user_file(
            str(self._token_file), [GMAIL_SEND_SCOPE]
        )
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except (GoogleAuthError, OSError) as error:
                raise MailError(
                    f"the mail token could not be refreshed: {error}"
                ) from error
            _write_token(credentials, self._token_file)
        if not credentials.valid:
            raise MailError(
                "the mail token is not valid; run `ccat mail-authorize`"
            )
        return credentials

    def _service(self) -> Any:
        """Build the Gmail API service.

        Returns:
            The Gmail v1 service resource.

        Raises:
            MailError: When the service cannot be built.
        """
        return _gmail_service(self._credentials(), self._timeout)

    def send(self, request: SendRequest) -> SendOutcome:
        """Send one message.

        Args:
            request: The message to send.

        Returns:
            The provider's outcome: "sent", "rejected", or "ambiguous".

        Raises:
            MailError: When the mail account itself is not usable.
        """
        return _send_via(self._service(), request)


class ServiceAccountGmailTransport:
    """Send messages with a Google service account.

    The key is read from :data:`SERVICE_ACCOUNT_KEY_ENV` as base64 and
    decoded back to the JSON Google issued.

    A service account has no mailbox of its own, so Gmail only accepts
    its sends when the account impersonates a Workspace user through
    domain-wide delegation.  ``subject`` must then name a user in a
    Workspace domain whose administrator authorized this service
    account's client id for the ``gmail.send`` scope; without it the
    provider refuses the message.
    """

    def __init__(
        self,
        subject: str = "",
        timeout: float = 30.0,
        key_env: str = SERVICE_ACCOUNT_KEY_ENV,
    ) -> None:
        """Build the transport.

        Args:
            subject: The Workspace user to impersonate, for
                domain-wide delegation.  Empty means the service
                account itself, which owns no mailbox.
            timeout: The per-request timeout in seconds.
            key_env: The environment variable holding the base64 key.
        """
        self._subject = subject
        self._timeout = timeout
        self._key_env = key_env

    def _credentials(self) -> Credentials:
        """Build the service account's credentials.

        Returns:
            Credentials for the sending identity.

        Raises:
            MailError: When the key is missing or unusable.
        """
        encoded = os.environ.get(self._key_env, "").strip()
        if not encoded:
            raise MailError(
                f"the service account key is not set; export {self._key_env}"
            )
        info = decode_service_account_key(encoded)
        try:
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=[GMAIL_SEND_SCOPE]
            )
        except (KeyError, ValueError) as error:
            raise MailError(
                f"the service account key could not be loaded: {error}"
            ) from error
        if self._subject:
            credentials = credentials.with_subject(self._subject)
        return credentials

    def _service(self) -> Any:
        """Build the Gmail API service.

        Returns:
            The Gmail v1 service resource.

        Raises:
            MailError: When the service cannot be built.
        """
        return _gmail_service(self._credentials(), self._timeout)

    def send(self, request: SendRequest) -> SendOutcome:
        """Send one message.

        Args:
            request: The message to send.

        Returns:
            The provider's outcome: "sent", "rejected", or "ambiguous".

        Raises:
            MailError: When the service account key is missing or
                unusable.
        """
        return _send_via(self._service(), request)


__all__ = [
    "GMAIL_SEND_SCOPE",
    "SERVICE_ACCOUNT_KEY_ENV",
    "GmailTransport",
    "MailError",
    "ServiceAccountGmailTransport",
    "authorize",
    "build_raw_message",
    "decode_service_account_key",
]
