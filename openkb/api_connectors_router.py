"""Google Drive connector REST endpoints.

An APIRouter so ``api.py`` stays under the per-file line gate. The poller
registry lives on ``app.state.gdrive_registry`` (one per ``create_app``).
The OAuth browser callback is unauthenticated; CSRF is the signed ``state``.
"""

from __future__ import annotations

import threading
from contextvars import copy_context
from pathlib import Path
from queue import Empty, Queue
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from openkb.api_helpers import _resolve_kb, _sse, require_bearer_token
from openkb.api_models import (
    GdriveActiveSyncsResponse,
    GdriveConnectRequest,
    GdriveDisconnectResponse,
    GdriveFolderItem,
    GdriveFolderListResponse,
    GdriveFolderRequest,
    GdriveKbRequest,
    GdriveOAuthStartResponse,
    GdriveStatusResponse,
    GdriveSyncCancelResponse,
    GdriveSyncResult,
)
from openkb.connectors.gdrive import (
    GdriveError,
    GdriveUnavailableError,
    authorization_url,
    decode_oauth_state,
    exchange_code,
    get_folder_name,
    list_folders,
    oauth_callback_query,
)
from openkb.connectors.store import (
    GdriveStoreError,
    clear_connection,
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
    enable_folder,
    pause_connector,
)

connectors_router = APIRouter()


def _gdrive_registry(request: Request) -> GDriveSyncRegistry:
    return request.app.state.gdrive_registry


def _http_from_gdrive(exc: Exception) -> HTTPException:
    if isinstance(exc, GdriveUnavailableError):
        return HTTPException(status_code=501, detail=str(exc))
    if isinstance(exc, (GdriveError, GdriveStoreError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Google Drive request failed.")


def _status_payload(kb: str, kb_dir, registry: GDriveSyncRegistry) -> GdriveStatusResponse:
    payload = public_status(kb_dir)
    runtime = registry.status(kb)
    payload.update(runtime)
    payload["kb"] = kb
    return GdriveStatusResponse(**payload)


def _request_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@connectors_router.get("/api/v1/connectors/gdrive/status", response_model=GdriveStatusResponse)
async def gdrive_status(
    request: Request,
    kb: str = Query(...),
    _: None = Depends(require_bearer_token),
) -> GdriveStatusResponse:
    kb_dir = _resolve_kb(kb)
    return _status_payload(kb, kb_dir, _gdrive_registry(request))


@connectors_router.get(
    "/api/v1/connectors/gdrive/oauth/start",
    response_model=GdriveOAuthStartResponse,
)
async def gdrive_oauth_start(
    request: Request,
    kb: str = Query(...),
    _: None = Depends(require_bearer_token),
) -> GdriveOAuthStartResponse:
    _resolve_kb(kb)
    try:
        url = authorization_url(kb, _request_base(request))
    except (GdriveError, GdriveUnavailableError) as exc:
        raise _http_from_gdrive(exc) from exc
    return GdriveOAuthStartResponse(auth_url=url)


@connectors_router.get("/api/v1/connectors/gdrive/oauth/callback")
async def gdrive_oauth_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    # Browser redirect from Google — no bearer token. State is HMAC-signed.
    kb_name = "unknown"
    try:
        if error:
            raise GdriveError(error)
        if not code or not state:
            raise GdriveError("Google OAuth callback is missing code or state.")
        kb_name = decode_oauth_state(state)
        kb_dir = _resolve_kb(kb_name)
        token = exchange_code(code, _request_base(request))
        set_refresh_token(kb_dir, token)
        set_service_account_json(kb_dir, None)
        disk = load_state(kb_dir)
        disk.auth_mode = "oauth"
        disk.error = None
        save_state(kb_dir, disk)
        fragment = f"/kb/{kb_name}?{oauth_callback_query()}"
    except HTTPException:
        fragment = f"/kb/{kb_name}?{oauth_callback_query('Knowledge base not found.')}"
    except Exception as exc:
        fragment = f"/kb/{kb_name}?{oauth_callback_query(str(exc))}"
    return RedirectResponse(url=f"/#{fragment}", status_code=302)


@connectors_router.post("/api/v1/connectors/gdrive/connect", response_model=GdriveStatusResponse)
async def gdrive_connect(
    request: Request,
    body: GdriveConnectRequest,
    _: None = Depends(require_bearer_token),
) -> GdriveStatusResponse:
    kb_dir = _resolve_kb(body.kb)
    registry = _gdrive_registry(request)
    try:
        info = parse_service_account_json(body.service_account_json)
        set_service_account_json(kb_dir, info)
        set_refresh_token(kb_dir, None)
        disk = load_state(kb_dir)
        disk.auth_mode = "service_account"
        disk.error = None
        save_state(kb_dir, disk)
        if body.folder_id:
            name = await run_in_threadpool(get_folder_name, kb_dir, body.folder_id)
            enable_folder(kb_dir, body.folder_id, name)
            registry.start(body.kb, kb_dir)
    except (GdriveError, GdriveStoreError, GdriveUnavailableError) as exc:
        raise _http_from_gdrive(exc) from exc
    return _status_payload(body.kb, kb_dir, registry)


@connectors_router.get(
    "/api/v1/connectors/gdrive/folders",
    response_model=GdriveFolderListResponse,
)
async def gdrive_folders(
    kb: str = Query(...),
    parent: str = Query(default="root"),
    _: None = Depends(require_bearer_token),
) -> GdriveFolderListResponse:
    kb_dir = _resolve_kb(kb)
    try:
        folders = await run_in_threadpool(list_folders, kb_dir, parent)
    except (GdriveError, GdriveUnavailableError) as exc:
        raise _http_from_gdrive(exc) from exc
    return GdriveFolderListResponse(
        kb=kb,
        parent_id=parent,
        folders=[GdriveFolderItem(**f) for f in folders],
    )


@connectors_router.post("/api/v1/connectors/gdrive/folder", response_model=GdriveStatusResponse)
async def gdrive_set_folder(
    request: Request,
    body: GdriveFolderRequest,
    _: None = Depends(require_bearer_token),
) -> GdriveStatusResponse:
    kb_dir = _resolve_kb(body.kb)
    registry = _gdrive_registry(request)
    try:
        name = await run_in_threadpool(get_folder_name, kb_dir, body.folder_id)
        enable_folder(kb_dir, body.folder_id, name)
        registry.start(body.kb, kb_dir)
    except (GdriveError, GdriveUnavailableError) as exc:
        raise _http_from_gdrive(exc) from exc
    return _status_payload(body.kb, kb_dir, registry)


def _sync_error_message(exc: Exception) -> str:
    if isinstance(exc, (GdriveUnavailableError, SyncInProgressError)):
        return str(exc)
    if isinstance(exc, GdriveError) and str(exc) == "No Drive folder is selected.":
        return str(exc)
    cause: BaseException = exc
    while cause.__cause__ is not None:
        cause = cause.__cause__
    try:
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError
    except ImportError:
        return "Google Drive sync failed. Check the server configuration and try again."
    if isinstance(cause, RefreshError):
        return "Google Drive authentication failed. Reconnect with valid Google credentials."
    if isinstance(cause, HttpError):
        status = cause.resp.status
        if status == 401:
            return "Google Drive authentication expired. Reconnect Google Drive."
        if status == 404:
            return "Drive folder or file not found. Check the folder ID and sharing permissions."
        if status == 403:
            reasons = {item.get("reason") for item in cause.error_details if isinstance(item, dict)}
            if "accessNotConfigured" in reasons:
                return "Enable Google Drive API in the credentials' Google Cloud project and retry."
            return "Google Drive access denied. Check sharing permissions and API quota."
        if status == 429:
            return "Google Drive rate limit reached. Try again later."
    return "Google Drive sync failed. Check the server configuration and try again."


async def _stream_gdrive_sync(
    kb: str, kb_dir: Path, registry: GDriveSyncRegistry
) -> AsyncIterator[str]:
    events: Queue[tuple[str, dict[str, Any]]] = Queue()
    closed = threading.Event()

    def emit(event: str, data: dict[str, Any]) -> None:
        if not closed.is_set():
            events.put((event, data))

    def worker() -> None:
        try:
            result = registry.sync_now(kb, kb_dir, on_event=emit)
            emit("final", {"kb": kb, **result})
        except Exception as exc:
            emit("error", {"message": _sync_error_message(exc)})
        finally:
            emit("done", {})

    yield _sse("start", {"kb": kb})
    threading.Thread(target=copy_context().run, args=(worker,), daemon=True).start()
    try:
        while True:
            try:
                event, data = await run_in_threadpool(events.get, True, 10)
            except Empty:
                yield ": keep-alive\n\n"
                continue
            yield _sse(event, data)
            if event == "done":
                break
    finally:
        closed.set()


@connectors_router.post("/api/v1/connectors/gdrive/sync/now", response_model=GdriveSyncResult)
async def gdrive_sync_now(
    request: Request,
    body: GdriveKbRequest,
    stream: bool = Query(default=False),
    _: None = Depends(require_bearer_token),
) -> Any:
    kb_dir = _resolve_kb(body.kb)
    registry = _gdrive_registry(request)
    if stream:
        return StreamingResponse(
            _stream_gdrive_sync(body.kb, kb_dir, registry),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        result = await run_in_threadpool(registry.sync_now, body.kb, kb_dir)
    except SyncInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (GdriveError, GdriveUnavailableError) as exc:
        raise _http_from_gdrive(exc) from exc
    return GdriveSyncResult(kb=body.kb, **result)


@connectors_router.get(
    "/api/v1/connectors/gdrive/sync/active",
    response_model=GdriveActiveSyncsResponse,
)
async def gdrive_sync_active(
    request: Request,
    _: None = Depends(require_bearer_token),
) -> GdriveActiveSyncsResponse:
    """Snapshots of manual syncs (running + finished) for popup restore."""
    return GdriveActiveSyncsResponse(syncs=_gdrive_registry(request).sync_progress())


@connectors_router.post(
    "/api/v1/connectors/gdrive/sync/cancel",
    response_model=GdriveSyncCancelResponse,
)
async def gdrive_sync_cancel(
    body: GdriveKbRequest,
    request: Request,
    _: None = Depends(require_bearer_token),
) -> GdriveSyncCancelResponse:
    """Cooperatively cancel an in-flight manual sync (between files)."""
    if not _gdrive_registry(request).cancel_sync(body.kb):
        raise HTTPException(status_code=409, detail="No Drive sync is running for this KB.")
    return GdriveSyncCancelResponse(kb=body.kb)


@connectors_router.post("/api/v1/connectors/gdrive/sync/stop", response_model=GdriveStatusResponse)
async def gdrive_sync_stop(
    request: Request,
    body: GdriveKbRequest,
    _: None = Depends(require_bearer_token),
) -> GdriveStatusResponse:
    kb_dir = _resolve_kb(body.kb)
    registry = _gdrive_registry(request)
    pause_connector(body.kb, kb_dir, registry)
    return _status_payload(body.kb, kb_dir, registry)


@connectors_router.post(
    "/api/v1/connectors/gdrive/disconnect",
    response_model=GdriveDisconnectResponse,
)
async def gdrive_disconnect(
    request: Request,
    body: GdriveKbRequest,
    _: None = Depends(require_bearer_token),
) -> GdriveDisconnectResponse:
    kb_dir = _resolve_kb(body.kb)
    registry = _gdrive_registry(request)
    registry.stop(body.kb, persist_enabled=False)
    clear_connection(kb_dir)
    return GdriveDisconnectResponse(kb=body.kb, disconnected=True)
