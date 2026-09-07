"""Google Drive connector: store, export mapping, poll mutations, OAuth state, API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openkb.api import create_app
from openkb.connectors.gdrive import (
    DriveFile,
    GdriveError,
    decode_oauth_state,
    encode_oauth_state,
    resolve_ingest,
)
from openkb.connectors.store import (
    FileRecord,
    GdriveStoreError,
    clear_connection,
    get_refresh_token,
    get_service_account_json,
    has_credentials,
    load_state,
    parse_service_account_json,
    public_status,
    save_state,
    set_refresh_token,
    set_service_account_json,
)
from openkb.connectors.sync_service import sync_once


def _client(monkeypatch, token: str | None = "secret") -> TestClient:
    if token is None:
        monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OPENKB_API_TOKEN", token)
    return TestClient(create_app())


def _auth(token: str = "secret") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _use_named_kb(monkeypatch, kb_dir, name: str = "test-kb") -> str:
    def resolve(kb):
        assert kb == name
        return kb_dir

    monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", resolve)
    return name


def _drive_file(
    fid: str = "id1",
    name: str = "notes.pdf",
    modified: str = "2026-01-01T00:00:00.000Z",
    local_name: str = "notes__id1.pdf",
) -> DriveFile:
    return DriveFile(
        id=fid,
        name=name,
        mime_type="application/pdf",
        modified_time=modified,
        size=12,
        export_mime=None,
        local_name=local_name,
    )


def test_store_round_trip_and_secret_redaction(kb_dir):
    set_refresh_token(kb_dir, "refresh-secret")
    sa = {
        "type": "service_account",
        "client_email": "bot@example.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
    }
    set_service_account_json(kb_dir, sa)
    state = load_state(kb_dir)
    state.auth_mode = "oauth"
    state.folder_id = "folder1"
    state.folder_name = "Docs"
    state.files["id1"] = FileRecord(
        name="a.pdf", modified_time="t", sha256="abc", local_name="a__id1.pdf"
    )
    save_state(kb_dir, state)

    state_file = kb_dir / ".openkb" / "connectors" / "gdrive.json"
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    dumped = json.dumps(raw)
    assert "refresh-secret" not in dumped
    assert "PRIVATE KEY" not in dumped
    assert get_refresh_token(kb_dir) == "refresh-secret"
    assert get_service_account_json(kb_dir)["client_email"] == sa["client_email"]

    public = public_status(kb_dir)
    assert public["connected"] is True
    assert public["folder_id"] == "folder1"
    assert "files" not in public
    assert has_credentials(kb_dir) is True

    clear_connection(kb_dir)
    assert get_refresh_token(kb_dir) is None
    assert get_service_account_json(kb_dir) is None
    assert load_state(kb_dir).folder_id is None


def test_refresh_token_rejects_newlines(kb_dir):
    with pytest.raises(GdriveStoreError):
        set_refresh_token(kb_dir, "a\nb")


def test_parse_service_account_json_rejects_oauth_client():
    with pytest.raises(GdriveStoreError):
        parse_service_account_json('{"installed": {"client_id": "x"}}')
    parsed = parse_service_account_json(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "a@b",
                "private_key": "k",
            }
        )
    )
    assert parsed["client_email"] == "a@b"


def test_resolve_ingest_export_and_skip():
    doc = resolve_ingest(
        "Spec",
        "application/vnd.google-apps.document",
        "abc12345zzzz",
        "t",
        None,
    )
    assert doc is not None
    assert doc.export_mime.endswith("wordprocessingml.document")
    assert doc.local_name.endswith(".docx")
    assert "abc12345"[:8] in doc.local_name

    sheet = resolve_ingest("Grid", "application/vnd.google-apps.spreadsheet", "s1", "t", None)
    assert sheet is not None and sheet.local_name.endswith(".xlsx")

    slides = resolve_ingest("Deck", "application/vnd.google-apps.presentation", "p1", "t", None)
    assert slides is not None and slides.local_name.endswith(".pptx")

    pdf = resolve_ingest("paper.pdf", "application/pdf", "f1", "t", 10)
    assert pdf is not None and pdf.export_mime is None

    skipped = resolve_ingest("Form", "application/vnd.google-apps.form", "x", "t", None)
    assert skipped is None
    assert resolve_ingest("photo.png", "image/png", "i", "t", 3) is None


def test_oauth_state_csrf_rejection(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "s3cret")
    token = encode_oauth_state("my-kb")
    assert decode_oauth_state(token) == "my-kb"
    with pytest.raises(GdriveError):
        decode_oauth_state(token[:-2] + "zz")
    with pytest.raises(GdriveError):
        decode_oauth_state("not-valid-state")


def test_sync_once_add_change_delete(kb_dir):
    state = load_state(kb_dir)
    state.folder_id = "root-folder"
    save_state(kb_dir, state)

    files = [_drive_file()]
    added: list[str] = []
    removed: list[str] = []

    def list_files(_kb, folder_id):
        assert folder_id == "root-folder"
        return list(files)

    def fetch(_kb, file: DriveFile) -> bytes:
        return f"body-{file.modified_time}".encode()

    def add_file(path: Path, _kb):
        added.append(path.name)

        class R:
            status = "added"

        return R()

    def remove_file(_kb, identifier: str):
        removed.append(identifier)
        return {"status": "removed"}

    result = sync_once(
        kb_dir,
        list_files=list_files,
        fetch_bytes=fetch,
        add_file=add_file,
        remove_file=remove_file,
    )
    assert result["added"] == 1
    assert added == ["notes__id1.pdf"]
    assert (kb_dir / "raw" / "notes__id1.pdf").read_bytes() == b"body-2026-01-01T00:00:00.000Z"

    # Same modifiedTime → no re-fetch ingest.
    added.clear()
    result = sync_once(
        kb_dir,
        list_files=list_files,
        fetch_bytes=fetch,
        add_file=add_file,
        remove_file=remove_file,
    )
    assert result["added"] == 0
    assert added == []

    # Changed content → remove previous, re-add.
    files[0] = _drive_file(modified="2026-02-01T00:00:00.000Z")
    result = sync_once(
        kb_dir,
        list_files=list_files,
        fetch_bytes=fetch,
        add_file=add_file,
        remove_file=remove_file,
    )
    assert result["added"] == 1
    assert removed == ["notes__id1.pdf"]

    # Deleted remotely → remove from KB.
    files.clear()
    removed.clear()
    result = sync_once(
        kb_dir,
        list_files=list_files,
        fetch_bytes=fetch,
        add_file=add_file,
        remove_file=remove_file,
    )
    assert result["removed"] == 1
    assert removed == ["notes__id1.pdf"]
    assert load_state(kb_dir).files == {}


def test_gdrive_status_requires_token(monkeypatch, kb_dir):
    kb = _use_named_kb(monkeypatch, kb_dir)
    client = _client(monkeypatch, token="secret")
    response = client.get(f"/api/v1/connectors/gdrive/status?kb={kb}")
    assert response.status_code == 401


def test_gdrive_status_ok_and_no_secrets(monkeypatch, kb_dir):
    kb = _use_named_kb(monkeypatch, kb_dir)
    set_refresh_token(kb_dir, "super-secret-token")
    state = load_state(kb_dir)
    state.auth_mode = "oauth"
    save_state(kb_dir, state)
    client = _client(monkeypatch)
    response = client.get(
        f"/api/v1/connectors/gdrive/status?kb={kb}",
        headers=_auth(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["auth_mode"] == "oauth"
    dumped = json.dumps(body)
    assert "super-secret-token" not in dumped


def test_oauth_callback_rejects_bad_state(monkeypatch, kb_dir):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "s3cret")
    client = _client(monkeypatch, token=None)
    response = client.get(
        "/api/v1/connectors/gdrive/oauth/callback",
        params={"code": "x", "state": "tampered"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "gdrive=error" in response.headers["location"]


def test_connect_service_account_without_drive_api(monkeypatch, kb_dir):
    kb = _use_named_kb(monkeypatch, kb_dir)
    client = _client(monkeypatch)
    payload = {
        "type": "service_account",
        "client_email": "bot@example.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nk\n-----END PRIVATE KEY-----\n",
    }
    response = client.post(
        "/api/v1/connectors/gdrive/connect",
        headers=_auth(),
        json={"kb": kb, "service_account_json": json.dumps(payload)},
    )
    assert response.status_code == 200
    assert response.json()["auth_mode"] == "service_account"
    assert get_service_account_json(kb_dir)["client_email"] == payload["client_email"]
