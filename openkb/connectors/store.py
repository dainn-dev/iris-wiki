"""Per-KB Google Drive connection state and secrets.

Public sync metadata lives in ``{kb}/.openkb/connectors/gdrive.json``.
Secrets never appear in that file:

* OAuth refresh token → ``{kb}/.env`` as ``GOOGLE_DRIVE_REFRESH_TOKEN``
* Service-account JSON → ``{kb}/.openkb/connectors/gdrive-sa.json`` (0600)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from openkb.locks import atomic_write_json, atomic_write_text

logger = logging.getLogger(__name__)

REFRESH_TOKEN_KEY = "GOOGLE_DRIVE_REFRESH_TOKEN"
DEFAULT_POLL_SECONDS = int(os.environ.get("OPENKB_GDRIVE_POLL_SECONDS", "300"))
AuthMode = Literal["oauth", "service_account"]


class GdriveStoreError(ValueError):
    """Invalid connector state or secret payload."""


@dataclass
class FileRecord:
    """One Drive file that has been ingested (or attempted) into this KB."""

    name: str
    modified_time: str
    sha256: str
    local_name: str


@dataclass
class GdriveState:
    """Persisted public connection state (no secrets)."""

    auth_mode: AuthMode | None = None
    folder_id: str | None = None
    folder_name: str | None = None
    enabled: bool = False
    poll_seconds: int = DEFAULT_POLL_SECONDS
    last_sync_at: str | None = None
    error: str | None = None
    files: dict[str, FileRecord] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "auth_mode": self.auth_mode,
            "folder_id": self.folder_id,
            "folder_name": self.folder_name,
            "enabled": self.enabled,
            "poll_seconds": self.poll_seconds,
            "last_sync_at": self.last_sync_at,
            "error": self.error,
            "files": {fid: asdict(rec) for fid, rec in self.files.items()},
        }

    def public_dict(self) -> dict[str, Any]:
        """Status payload: counts only, never the per-file map or secrets."""
        return {
            "auth_mode": self.auth_mode,
            "folder_id": self.folder_id,
            "folder_name": self.folder_name,
            "enabled": self.enabled,
            "poll_seconds": self.poll_seconds,
            "last_sync_at": self.last_sync_at,
            "error": self.error,
            "file_count": len(self.files),
        }


def connectors_dir(kb_dir: Path) -> Path:
    return kb_dir / ".openkb" / "connectors"


def state_path(kb_dir: Path) -> Path:
    return connectors_dir(kb_dir) / "gdrive.json"


def sa_json_path(kb_dir: Path) -> Path:
    return connectors_dir(kb_dir) / "gdrive-sa.json"


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_private_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    _chmod_private(path)


def load_state(kb_dir: Path) -> GdriveState:
    """Load connector state; missing/corrupt files yield empty defaults."""
    path = state_path(kb_dir)
    if not path.is_file():
        return GdriveState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Ignoring unreadable Google Drive connector state at %s", path)
        return GdriveState()
    if not isinstance(raw, dict):
        return GdriveState()
    files: dict[str, FileRecord] = {}
    raw_files = raw.get("files") or {}
    if isinstance(raw_files, dict):
        for fid, rec in raw_files.items():
            if not isinstance(fid, str) or not isinstance(rec, dict):
                continue
            try:
                files[fid] = FileRecord(
                    name=str(rec["name"]),
                    modified_time=str(rec["modified_time"]),
                    sha256=str(rec["sha256"]),
                    local_name=str(rec["local_name"]),
                )
            except (KeyError, TypeError):
                continue
    mode = raw.get("auth_mode")
    auth_mode: AuthMode | None
    if mode in ("oauth", "service_account"):
        auth_mode = mode
    else:
        auth_mode = None
    poll = raw.get("poll_seconds", DEFAULT_POLL_SECONDS)
    try:
        poll_seconds = max(30, int(poll))
    except (TypeError, ValueError):
        poll_seconds = DEFAULT_POLL_SECONDS
    return GdriveState(
        auth_mode=auth_mode,
        folder_id=raw.get("folder_id") or None,
        folder_name=raw.get("folder_name") or None,
        enabled=bool(raw.get("enabled")),
        poll_seconds=poll_seconds,
        last_sync_at=raw.get("last_sync_at") or None,
        error=raw.get("error") or None,
        files=files,
    )


def save_state(kb_dir: Path, state: GdriveState) -> None:
    path = state_path(kb_dir)
    _ensure_private_file(path)
    atomic_write_json(path, state.to_json())
    _chmod_private(path)


def _merge_env(env_path: Path, updates: dict[str, str | None]) -> None:
    """Read-modify-write a ``.env`` file; ``None`` removes the key."""
    env_lines: dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export ") or line.startswith("export\t"):
                line = line[len("export") :].lstrip()
            if "=" not in line:
                continue
            key, _eq, value = line.partition("=")
            env_lines[key.strip()] = value
    for key, value in updates.items():
        if value is None:
            env_lines.pop(key, None)
        else:
            env_lines[key] = value
    _ensure_private_file(env_path)
    atomic_write_text(env_path, "".join(f"{k}={v}\n" for k, v in env_lines.items()))
    _chmod_private(env_path)


def set_refresh_token(kb_dir: Path, token: str | None) -> None:
    if token is not None and token != "".join(token.splitlines()):
        raise GdriveStoreError("refresh token must not contain newlines")
    _merge_env(kb_dir / ".env", {REFRESH_TOKEN_KEY: token})


def get_refresh_token(kb_dir: Path) -> str | None:
    from dotenv import dotenv_values

    env_path = kb_dir / ".env"
    if not env_path.is_file():
        return None
    values = dict(dotenv_values(str(env_path)))
    token = values.get(REFRESH_TOKEN_KEY) or None
    return token or None


def set_service_account_json(kb_dir: Path, payload: dict[str, Any] | None) -> None:
    path = sa_json_path(kb_dir)
    if payload is None:
        path.unlink(missing_ok=True)
        return
    _ensure_private_file(path)
    atomic_write_json(path, payload)
    _chmod_private(path)


def get_service_account_json(kb_dir: Path) -> dict[str, Any] | None:
    path = sa_json_path(kb_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_service_account_json(raw: str) -> dict[str, Any]:
    """Validate a pasted Google service-account key (never log the body)."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise GdriveStoreError("Service-account key is not valid JSON.") from exc
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise GdriveStoreError("Not a Google service-account JSON key.")
    if not data.get("client_email") or not data.get("private_key"):
        raise GdriveStoreError("Service-account JSON is missing client_email or private_key.")
    return data


def has_credentials(kb_dir: Path, state: GdriveState | None = None) -> bool:
    state = state if state is not None else load_state(kb_dir)
    if state.auth_mode == "oauth":
        return bool(get_refresh_token(kb_dir))
    if state.auth_mode == "service_account":
        return get_service_account_json(kb_dir) is not None
    return bool(get_refresh_token(kb_dir) or get_service_account_json(kb_dir))


def oauth_client_configured() -> bool:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    return bool(client_id and client_secret)


def clear_connection(kb_dir: Path) -> None:
    """Wipe tokens, the SA key, and public state. Idempotent."""
    set_refresh_token(kb_dir, None)
    set_service_account_json(kb_dir, None)
    path = state_path(kb_dir)
    path.unlink(missing_ok=True)


def public_status(kb_dir: Path) -> dict[str, Any]:
    state = load_state(kb_dir)
    payload = state.public_dict()
    payload["connected"] = has_credentials(kb_dir, state)
    payload["has_credentials"] = payload["connected"]
    payload["oauth_configured"] = oauth_client_configured()
    return payload
