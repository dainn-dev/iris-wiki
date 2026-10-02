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
from openkb.connectors.sync_service import (
    GDriveSyncRegistry,
    SyncInProgressError,
    sync_once,
)


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


def _configure_sync(monkeypatch, kb_dir, files):
    state = load_state(kb_dir)
    state.folder_id = "folder1"
    save_state(kb_dir, state)
    monkeypatch.setattr("openkb.connectors.sync_service._default_list", lambda *_: files)
    monkeypatch.setattr("openkb.connectors.sync_service._default_fetch", lambda *_: b"body")
    monkeypatch.setattr(
        "openkb.connectors.sync_service._default_add", lambda *_: {"status": "added"}
    )


def _sse_events(text):
    events = []
    for block in text.split("\n\n"):
        lines = block.splitlines()
        if len(lines) == 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


def test_sync_progress_only_finishes_file_after_ingestion(monkeypatch, kb_dir):
    _configure_sync(monkeypatch, kb_dir, [_drive_file()])
    events = []

    def add_file(*_):
        assert events[-1] == ("file", {"id": "id1", "name": "notes.pdf", "status": "processing"})
        return {"status": "added"}

    result = sync_once(kb_dir, add_file=add_file, on_event=lambda *e: events.append(e))
    assert [name for name, _ in events] == ["scanning", "files", "file", "file", "file"]
    assert events[1][1] == {"files": [{"id": "id1", "name": "notes.pdf"}]}
    assert [data["status"] for name, data in events if name == "file"] == [
        "downloading",
        "processing",
        "added",
    ]
    assert result["added"] == 1
    assert load_state(kb_dir).files["id1"].name == "notes.pdf"


@pytest.mark.parametrize("failure", ["download", "ingest", "failed_result", "skipped_result"])
def test_sync_progress_reports_file_failures_and_skips(monkeypatch, kb_dir, failure):
    _configure_sync(monkeypatch, kb_dir, [_drive_file()])
    events = []

    def fail(*_):
        raise RuntimeError("private-provider-payload")

    if failure == "download":
        monkeypatch.setattr("openkb.connectors.sync_service._default_fetch", fail)
    elif failure == "ingest":
        monkeypatch.setattr("openkb.connectors.sync_service._default_add", fail)
    else:
        monkeypatch.setattr(
            "openkb.connectors.sync_service._default_add",
            lambda *_: {"status": failure.removesuffix("_result")},
        )
    result = sync_once(kb_dir, on_event=lambda *e: events.append(e))
    expected = "skipped" if failure == "skipped_result" else "failed"
    assert events[-1][1]["status"] == expected
    assert result[expected] == 1
    assert "private-provider-payload" not in json.dumps(events)


@pytest.mark.parametrize("modified", [False, True])
def test_sync_progress_marks_unchanged_files_skipped(monkeypatch, kb_dir, modified):
    _configure_sync(monkeypatch, kb_dir, [_drive_file()])
    sync_once(kb_dir)
    if modified:
        monkeypatch.setattr(
            "openkb.connectors.sync_service._default_list",
            lambda *_: [_drive_file(modified="new-time")],
        )
    events = []
    result = sync_once(kb_dir, on_event=lambda *e: events.append(e))
    assert events[-1][1]["status"] == "skipped"
    assert result["added"] == 0


def test_sync_progress_tracks_deleted_files(monkeypatch, kb_dir):
    _configure_sync(monkeypatch, kb_dir, [_drive_file()])
    sync_once(kb_dir)
    monkeypatch.setattr("openkb.connectors.sync_service._default_list", lambda *_: [])
    events = []
    removed = []
    sync_once(
        kb_dir,
        remove_file=lambda _, name: removed.append(name),
        on_event=lambda *e: events.append(e),
    )
    assert events[1][1] == {"files": [{"id": "id1", "name": "notes.pdf"}]}
    assert [data["status"] for name, data in events if name == "file"] == ["removing", "removed"]
    assert removed == ["notes__id1.pdf"]


def test_sync_now_streams_progress_and_preserves_json_api(monkeypatch, kb_dir):
    _configure_sync(monkeypatch, kb_dir, [_drive_file()])
    kb = _use_named_kb(monkeypatch, kb_dir)
    client = _client(monkeypatch)
    response = client.post(
        "/api/v1/connectors/gdrive/sync/now?stream=true", headers=_auth(), json={"kb": kb}
    )
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    events = _sse_events(response.text)
    assert events[0][0] == "start"
    assert events[-2] == (
        "final",
        {"kb": kb, "added": 1, "skipped": 0, "failed": 0, "removed": 0, "cancelled": 0},
    )
    assert events[-1] == ("done", {})
    assert [d["status"] for e, d in events if e == "file"] == ["downloading", "processing", "added"]
    response = client.post("/api/v1/connectors/gdrive/sync/now", headers=_auth(), json={"kb": kb})
    assert response.json() == {
        "kb": kb,
        "added": 0,
        "skipped": 0,
        "failed": 0,
        "removed": 0,
        "cancelled": 0,
    }


def test_sync_stream_requires_auth(monkeypatch, kb_dir):
    kb = _use_named_kb(monkeypatch, kb_dir)
    response = _client(monkeypatch).post(
        "/api/v1/connectors/gdrive/sync/now?stream=true", json={"kb": kb}
    )
    assert response.status_code == 401


@pytest.mark.parametrize("active", [False, True])
def test_sync_stream_auth_error_never_emits_success(monkeypatch, kb_dir, active):
    from google.auth.exceptions import RefreshError

    from openkb.connectors.sync_service import SyncerState

    _configure_sync(monkeypatch, kb_dir, [])
    kb = _use_named_kb(monkeypatch, kb_dir)
    client = _client(monkeypatch)
    if active:
        registry = client.app.state.gdrive_registry
        registry._syncers[kb] = SyncerState(kb, kb_dir, 300, 0)

    def fail(*_):
        raise RefreshError("invalid_grant: private-provider-payload")

    monkeypatch.setattr("openkb.connectors.sync_service._default_list", fail)
    response = client.post(
        "/api/v1/connectors/gdrive/sync/now?stream=true", headers=_auth(), json={"kb": kb}
    )
    events = _sse_events(response.text)
    assert events[-2][0] == "error"
    assert "reconnect" in events[-2][1]["message"].lower()
    assert "private-provider-payload" not in response.text
    assert "final" not in [e for e, _ in events]
    assert events[-1] == ("done", {})


def test_sync_stream_without_folder_reports_error(monkeypatch, kb_dir):
    kb = _use_named_kb(monkeypatch, kb_dir)
    response = _client(monkeypatch).post(
        "/api/v1/connectors/gdrive/sync/now?stream=true", headers=_auth(), json={"kb": kb}
    )
    events = _sse_events(response.text)
    assert events[-2][0] == "error"
    assert "folder" in events[-2][1]["message"].lower()
    assert "final" not in [e for e, _ in events]


def test_sync_once_cancel_stops_between_files_and_keeps_results(monkeypatch, kb_dir):
    _configure_sync(
        monkeypatch,
        kb_dir,
        [
            _drive_file("id1", "a.pdf", "t1", "a__id1.pdf"),
            _drive_file("id2", "b.pdf", "t2", "b__id2.pdf"),
        ],
    )
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # process file 1, cancel before file 2

    events = []
    result = sync_once(kb_dir, is_cancelled=cancel, on_event=lambda *e: events.append(e))
    assert result == {"added": 1, "skipped": 0, "failed": 0, "removed": 0, "cancelled": 1}
    assert "id1" in load_state(kb_dir).files
    assert "id2" not in load_state(kb_dir).files


def test_sync_now_rejects_overlap_and_records_snapshot(monkeypatch, kb_dir):
    import threading
    import time

    _configure_sync(monkeypatch, kb_dir, [_drive_file()])
    registry = GDriveSyncRegistry()
    gate = threading.Event()
    monkeypatch.setattr(
        "openkb.connectors.sync_service._default_list",
        lambda *_: (gate.wait(5), [_drive_file()])[1],
    )
    worker = threading.Thread(target=lambda: registry.sync_now("demo", kb_dir), daemon=True)
    worker.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        snaps = registry.sync_progress()
        if snaps and snaps[0]["phase"] == "running":
            break
        time.sleep(0.01)
    else:
        pytest.fail("manual sync did not register a running snapshot")
    with pytest.raises(SyncInProgressError):
        registry.sync_now("demo", kb_dir)
    assert registry.cancel_sync("demo") is True
    gate.set()
    worker.join(5)
    snap = registry.sync_progress()[0]
    # Cancelled while still listing → no files were ever ingested.
    assert snap["phase"] == "cancelled"
    assert snap["result"]["added"] == 0
    assert snap["files"] == [{"id": "id1", "name": "notes.pdf", "status": "pending"}]


def test_sync_now_marks_partial_run_cancelled(monkeypatch, kb_dir):
    _configure_sync(
        monkeypatch,
        kb_dir,
        [
            _drive_file("id1", "a.pdf", "t1", "a__id1.pdf"),
            _drive_file("id2", "b.pdf", "t2", "b__id2.pdf"),
        ],
    )
    registry = GDriveSyncRegistry()
    monkeypatch.setattr(
        "openkb.connectors.sync_service._default_add",
        lambda *_: (registry.cancel_sync("demo"), {"status": "added"})[1],
    )
    result = registry.sync_now("demo", kb_dir)
    assert result["cancelled"] == 1
    assert result["added"] == 1
    snap = registry.sync_progress()[0]
    assert snap["phase"] == "cancelled"
    assert {f["id"]: f["status"] for f in snap["files"]} == {
        "id1": "added",
        "id2": "pending",
    }


def test_sync_active_and_cancel_endpoints(monkeypatch, kb_dir):
    _configure_sync(monkeypatch, kb_dir, [_drive_file()])
    kb = _use_named_kb(monkeypatch, kb_dir)
    client = _client(monkeypatch)
    response = client.get("/api/v1/connectors/gdrive/sync/active", headers=_auth())
    assert response.status_code == 200
    assert response.json() == {"syncs": []}
    response = client.post(
        "/api/v1/connectors/gdrive/sync/cancel", headers=_auth(), json={"kb": kb}
    )
    assert response.status_code == 409
    client.post("/api/v1/connectors/gdrive/sync/now", headers=_auth(), json={"kb": kb})
    syncs = client.get("/api/v1/connectors/gdrive/sync/active", headers=_auth()).json()["syncs"]
    assert syncs[0]["kb"] == kb
    assert syncs[0]["phase"] == "done"
    assert syncs[0]["files"][0]["status"] == "added"


def test_sync_now_conflict_returns_409(monkeypatch, kb_dir):
    from openkb.connectors.sync_service import SyncerState

    _configure_sync(monkeypatch, kb_dir, [])
    kb = _use_named_kb(monkeypatch, kb_dir)
    client = _client(monkeypatch)
    registry = client.app.state.gdrive_registry
    syncer = SyncerState(kb, kb_dir, 300, 0)
    syncer.sync_lock.acquire()  # simulate the poller mid-poll
    registry._syncers[kb] = syncer
    try:
        response = client.post(
            "/api/v1/connectors/gdrive/sync/now", headers=_auth(), json={"kb": kb}
        )
        assert response.status_code == 409
    finally:
        syncer.sync_lock.release()
