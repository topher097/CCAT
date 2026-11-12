"""Upload and download files with the Google Drive API.

Setup follows the Drive API Python quickstart:
https://developers.google.com/workspace/drive/api/quickstart/python

Requires: google-api-python-client, google-auth-httplib2, google-auth-oauthlib.

Credentials live in `ccat_google_credentials.json` (a Desktop-app OAuth client
secret). The OAuth token is cached in `ccat_google_token.json` next to it; the
first run opens a browser for authorization.
"""

import logging
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

logger = logging.getLogger(__name__)

# Full drive scope: covers upload, download, and folder lookup.
# If modifying these scopes, delete the token file.
SCOPES = ["https://www.googleapis.com/auth/drive"]
CREDENTIALS_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "ccat_google_credentials.json"
)
TOKEN_FILE = CREDENTIALS_FILE.with_name("ccat_google_token.json")


def _get_service() -> Any:
    """Authorize with OAuth and build a Drive v3 service.

    Loads a cached token if present, refreshes it when expired, otherwise
    runs the installed-app OAuth flow and caches the new token.

    Returns:
        The Drive v3 API service resource.

    Raises:
        HttpError: If the Drive API service cannot be built.
    """
    creds: Credentials | None = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)
        with TOKEN_FILE.open("w") as token:
            token.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


def _resolve_folder(service: Any, folder: str) -> str | None:
    """Resolve a Drive folder to its folder ID.

    Interprets a plain string directly as a folder ID, extracts the ID from a
    Drive folder URL, and treats "" / "root" style names as the My Drive
    root.

    Args:
        service: The Drive v3 API service resource.
        folder: A folder ID, a Drive folder URL, or a My Drive root
            alias (""/"root"/"my drive"/"drive").

    Returns:
        The resolved folder ID, or None for the My Drive root.
    """
    if "://" in folder or "folders/" in folder:
        return folder.rsplit("folders/", 1)[1].split("/")[0].split("?")[0]
    if folder.strip(". ") in ("", "root", "my drive", "mydrive", "drive"):
        return None  # My Drive root: omit `parents`
    return folder


def _find_file_id(service: Any, name: str, parent_id: str | None = None) -> str:
    """Find the first file ID matching an exact name, optionally under a parent.

    Args:
        service: The Drive v3 API service resource.
        name: Exact file name to search for.
        parent_id: Optional parent folder ID to restrict the search under.

    Returns:
        The matched file ID.

    Raises:
        ValueError: If no matching file exists.
        HttpError: If the query fails.
    """
    clause = "name = '{}' and trashed = false".format(
        name.replace("\\", "\\\\").replace("'", "\\'")
    )
    if parent_id is not None:
        clause += f" and '{parent_id}' in parents"
    results = (
        service.files()
        .list(q=clause, pageSize=1, fields="files(id, name)")
        .execute()
    )
    items = results.get("files", [])
    if not items:
        parent_desc = f" under folder {parent_id!r}" if parent_id else ""
        raise ValueError(f"No Drive file named {name!r} found{parent_desc}")
    return items[0]["id"]


def upload(local_path: str | Path, folder: str) -> str:
    """Upload a local file to a directory on Google Drive.

    Args:
        local_path: Path to the local file to upload.
        folder: Destination Drive folder ID, or an exact folder name
            searched in My Drive.

    Returns:
        The ID of the created Drive file.

    Raises:
        FileNotFoundError: If the local file does not exist.
        HttpError: If the upload fails.
    """
    local = Path(local_path)
    if not local.is_file():
        raise FileNotFoundError(f"Local file not found: {local}")

    service = _get_service()
    folder_id = _resolve_folder(service, folder)
    body = {"name": local.name}
    if folder_id is not None:
        body["parents"] = [folder_id]

    media = MediaFileUpload(
        str(local),
        resumable=True,
    )
    file_id: str = (
        service.files()
        .create(
            body=body,
            media_body=media,
            fields="id",
        )
        .execute()
    )
    logger.info("Uploaded %s to Drive file %s", local, file_id)
    return file_id


def download(file: str, local_path: str | Path) -> Path:
    """Download a file from Google Drive to a local path.

    Args:
        file: Drive file ID or an exact file name searched in My Drive.
        local_path: Local destination path for the downloaded content.

    Returns:
        The path the file was written to.

    Raises:
        ValueError: If no Drive file matches the given name.
        HttpError: If the download fails.
    """
    service = _get_service()
    file_id = _find_file_id(service, file)
    destination = Path(local_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    request = service.files().get_media(fileId=file_id)
    with destination.open("wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    logger.info("Downloaded Drive file %s to %s", file_id, destination)
    return destination


def cli() -> None:
    """Run the Drive functions as a CLI via python-fire.

    Usage:
        python -m ccat.gdrive upload LOCAL_PATH FOLDER
        python -m ccat.gdrive download FILE LOCAL_PATH
    """
    import fire

    fire.Fire({"upload": upload, "download": download})


if __name__ == "__main__":
    cli()
