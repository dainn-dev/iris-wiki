"""Google Drive API client: list, download/export, OAuth helpers.

Google libraries are imported lazily so CLI/API modules load without the
optional ``gdrive`` extra. Callers catch :class:`GdriveUnavailableError`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from openkb.cli import SUPPORTED_EXTENSIONS
from openkb.connectors.store import get_refresh_token, get_service_account_json, load_state

logger = logging.getLogger(__name__)

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
STATE_TTL_SECONDS = 600
MAX_LIST_FILES = int(os.environ.get("OPENKB_GDRIVE_MAX_FILES", "2000"))

EXPORT_MAP: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
}

_LIST_FIELDS = "nextPageToken, files(id, name, mimeType, modifiedTime, size, shortcutDetails)"
_FILE_FIELDS = "id, name, mimeType, modifiedTime, size, shortcutDetails"
_UNSAFE_NAME = re.compile(r"[^\w.\- ]+", re.UNICODE)


class GdriveError(RuntimeError):
    """Drive API or OAuth failure with a user-facing message."""


class GdriveUnavailableError(GdriveError):
    """Optional Google client libraries are not installed."""


@dataclass(frozen=True)
class DriveFile:
    """An ingestible Drive file (Workspace types already mapped to an export)."""

    id: str
    name: str
    mime_type: str
    modified_time: str
    size: int | None
    export_mime: str | None
    local_name: str


def max_file_bytes() -> int:
    return int(os.environ.get("OPENKB_MAX_UPLOAD_FILE_BYTES", str(100 * 1024 * 1024)))


def _require_google() -> None:
    try:
        import google.auth  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
    except ImportError as exc:
        raise GdriveUnavailableError(
            "Google Drive support requires the gdrive extra "
            "(pip install 'openkb[gdrive]'; included in openkb[web])."
        ) from exc


def oauth_redirect_uri(request_base: str | None = None) -> str:
    explicit = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if explicit:
        return explicit.rstrip("/")
    if request_base:
        return request_base.rstrip("/") + "/api/v1/connectors/gdrive/oauth/callback"
    return "http://localhost:7566/api/v1/connectors/gdrive/oauth/callback"


def _state_secret() -> bytes:
    raw = (
        os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
        or os.environ.get("OPENKB_API_TOKEN")
        or "openkb-gdrive-dev"
    )
    return raw.encode("utf-8")


def encode_oauth_state(kb: str) -> str:
    """Signed ``kb:nonce:timestamp`` blob for the OAuth ``state`` param."""
    nonce = urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=")
    payload = f"{kb}:{nonce}:{int(time.time())}"
    sig = hmac.new(_state_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlsafe_b64encode(f"{payload}:{sig}".encode("utf-8")).decode("ascii").rstrip("=")


def decode_oauth_state(state: str) -> str:
    """Return the KB name or raise :class:`GdriveError` on a bad/expired state."""
    padding = "=" * (-len(state) % 4)
    try:
        decoded = urlsafe_b64decode(state + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise GdriveError("Invalid OAuth state.") from exc
    parts = decoded.rsplit(":", 1)
    if len(parts) != 2:
        raise GdriveError("Invalid OAuth state.")
    payload, sig = parts
    expected = hmac.new(_state_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise GdriveError("Invalid OAuth state.")
    bits = payload.split(":")
    if len(bits) < 3:
        raise GdriveError("Invalid OAuth state.")
    kb, _nonce, ts_s = bits[0], bits[1], bits[-1]
    try:
        ts = int(ts_s)
    except ValueError as exc:
        raise GdriveError("Invalid OAuth state.") from exc
    if abs(time.time() - ts) > STATE_TTL_SECONDS:
        raise GdriveError("OAuth state expired. Start Connect with Google again.")
    if not kb:
        raise GdriveError("Invalid OAuth state.")
    return kb


def local_filename(drive_id: str, name: str, ext: str) -> str:
    """Stable unique raw/ name: sanitized stem + short Drive id + export ext."""
    stem = _UNSAFE_NAME.sub("_", Path(name).stem).strip("._") or "file"
    stem = stem[:80]
    suffix = ext if ext.startswith(".") else f".{ext}"
    return f"{stem}__{drive_id[:8]}{suffix.lower()}"


def resolve_ingest(
    name: str,
    mime_type: str,
    drive_id: str,
    modified_time: str,
    size: int | None,
) -> DriveFile | None:
    """Map a Drive item to an ingestible file, or None if unsupported."""
    if mime_type in (FOLDER_MIME, SHORTCUT_MIME):
        return None
    export = EXPORT_MAP.get(mime_type)
    if export is not None:
        export_mime, ext = export
        return DriveFile(
            id=drive_id,
            name=name,
            mime_type=mime_type,
            modified_time=modified_time,
            size=size,
            export_mime=export_mime,
            local_name=local_filename(drive_id, name, ext),
        )
    ext = Path(name).suffix.lower()
    if ext in SUPPORTED_EXTENSIONS:
        return DriveFile(
            id=drive_id,
            name=name,
            mime_type=mime_type,
            modified_time=modified_time,
            size=size,
            export_mime=None,
            local_name=local_filename(drive_id, name, ext),
        )
    return None


def _credentials_for_kb(kb_dir: Path) -> Any:
    _require_google()
    from google.oauth2.credentials import Credentials
    from google.oauth2.service_account import Credentials as SACredentials

    state = load_state(kb_dir)
    if state.auth_mode == "service_account" or (
        state.auth_mode is None and get_service_account_json(kb_dir)
    ):
        info = get_service_account_json(kb_dir)
        if not info:
            raise GdriveError("No Google Drive service-account key is stored for this KB.")
        return SACredentials.from_service_account_info(info, scopes=[DRIVE_READONLY_SCOPE])
    token = get_refresh_token(kb_dir)
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not token:
        raise GdriveError("Google Drive is not connected for this KB.")
    if not client_id or not client_secret:
        raise GdriveError("GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET must be set.")
    return Credentials(
        token=None,
        refresh_token=token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=[DRIVE_READONLY_SCOPE],
    )


def build_service(kb_dir: Path) -> Any:
    _require_google()
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=_credentials_for_kb(kb_dir), cache_discovery=False)


def _list_children(service: Any, parent_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        response = (
            service.files()
            .list(
                q=f"'{parent_id}' in parents and trashed = false",
                fields=_LIST_FIELDS,
                pageToken=page_token,
                pageSize=100,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        items.extend(response.get("files") or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return items


def list_folders(kb_dir: Path, parent_id: str = "root") -> list[dict[str, str]]:
    """Folder-picker listing: immediate child folders of *parent_id*."""
    service = build_service(kb_dir)
    folders: list[dict[str, str]] = []
    page_token: str | None = None
    query = f"'{parent_id}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
    while True:
        response = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name)",
                pageToken=page_token,
                pageSize=100,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                orderBy="name",
            )
            .execute()
        )
        for item in response.get("files") or []:
            folders.append({"id": item["id"], "name": item.get("name") or item["id"]})
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return folders


def get_folder_name(kb_dir: Path, folder_id: str) -> str:
    service = build_service(kb_dir)
    meta = (
        service.files()
        .get(fileId=folder_id, fields="id, name, mimeType", supportsAllDrives=True)
        .execute()
    )
    if meta.get("mimeType") != FOLDER_MIME:
        raise GdriveError("The chosen Drive id is not a folder.")
    return str(meta.get("name") or folder_id)


def list_ingestible_files(kb_dir: Path, folder_id: str) -> list[DriveFile]:
    """Recursively list ingestible files under *folder_id* (follows shortcuts)."""
    service = build_service(kb_dir)
    out: list[DriveFile] = []
    queue = [folder_id]
    seen_folders: set[str] = set()
    seen_files: set[str] = set()
    while queue and len(out) < MAX_LIST_FILES:
        parent = queue.pop(0)
        if parent in seen_folders:
            continue
        seen_folders.add(parent)
        try:
            children = _list_children(service, parent)
        except Exception as exc:
            raise GdriveError(f"Could not list Drive folder: {exc}") from exc
        for item in children:
            mime = item.get("mimeType") or ""
            fid = item.get("id") or ""
            if not fid:
                continue
            if mime == FOLDER_MIME:
                queue.append(fid)
                continue
            if mime == SHORTCUT_MIME:
                details = item.get("shortcutDetails") or {}
                target = details.get("targetId")
                target_mime = details.get("targetMimeType") or ""
                if not target:
                    continue
                if target_mime == FOLDER_MIME:
                    queue.append(target)
                    continue
                try:
                    item = (
                        service.files()
                        .get(fileId=target, fields=_FILE_FIELDS, supportsAllDrives=True)
                        .execute()
                    )
                except Exception:
                    logger.debug("Skipping unresolved Drive shortcut %s", fid, exc_info=True)
                    continue
                mime = item.get("mimeType") or ""
                fid = item.get("id") or fid
            size_raw = item.get("size")
            try:
                size = int(size_raw) if size_raw is not None else None
            except (TypeError, ValueError):
                size = None
            resolved = resolve_ingest(
                name=item.get("name") or "file",
                mime_type=mime,
                drive_id=fid,
                modified_time=str(item.get("modifiedTime") or ""),
                size=size,
            )
            if resolved is None or resolved.id in seen_files:
                continue
            seen_files.add(resolved.id)
            out.append(resolved)
    return out


def download_file(kb_dir: Path, file: DriveFile) -> bytes:
    """Download or export *file*; raise if over the upload size cap."""
    _require_google()
    from googleapiclient.http import MediaIoBaseDownload

    if file.size is not None and file.size > max_file_bytes():
        raise GdriveError(f"{file.name} exceeds the {max_file_bytes()} byte upload limit.")
    service = build_service(kb_dir)
    if file.export_mime:
        request = service.files().export_media(fileId=file.id, mimeType=file.export_mime)
    else:
        request = service.files().get_media(fileId=file.id)
    import io

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
        if buf.tell() > max_file_bytes():
            raise GdriveError(f"{file.name} exceeds the {max_file_bytes()} byte upload limit.")
    return buf.getvalue()


def authorization_url(kb: str, request_base: str | None = None) -> str:
    """Build the Google consent URL (access_type=offline so we get a refresh token)."""
    _require_google()
    from google_auth_oauthlib.flow import Flow

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise GdriveError(
            "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET to use Connect with Google."
        )
    redirect = oauth_redirect_uri(request_base)
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect],
            }
        },
        scopes=[DRIVE_READONLY_SCOPE],
        redirect_uri=redirect,
    )
    url, _unused = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=encode_oauth_state(kb),
    )
    return url


def exchange_code(code: str, request_base: str | None = None) -> str:
    """Exchange an auth code for a refresh token."""
    _require_google()
    from google_auth_oauthlib.flow import Flow

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    redirect = oauth_redirect_uri(request_base)
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect],
            }
        },
        scopes=[DRIVE_READONLY_SCOPE],
        redirect_uri=redirect,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    token = getattr(creds, "refresh_token", None)
    if not token:
        raise GdriveError(
            "Google did not return a refresh token. Re-connect and grant offline access."
        )
    return str(token)


def oauth_callback_query(error: str | None = None) -> str:
    """Hash-router fragment query after OAuth (success or error)."""
    if error:
        return urlencode({"gdrive": "error", "message": error})
    return "gdrive=connected"
