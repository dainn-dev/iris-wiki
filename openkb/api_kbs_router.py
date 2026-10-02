"""Knowledge-base lifecycle endpoints beyond config (POST /api/v1/kb/delete).

An APIRouter (sibling of api_config_router.py / api_graph.py / api_pages_router.py)
so api.py stays under the per-file line gate (tests/test_file_size.py). delete_kb
does its own filesystem removal (under the KB ingest lock) + global-registry
unregister, so this endpoint needs no create_app closure and extracts cleanly.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from openkb.api_helpers import _is_kb_dir, require_bearer_token
from openkb.api_models import KbDeleteRequest, KbDeleteResponse
from openkb.config import registered_kbs, resolve_kb_alias
from openkb.kb_admin import delete_kb

kbs_router = APIRouter()


@kbs_router.post("/api/v1/kb/delete", response_model=KbDeleteResponse)
async def delete_kb_endpoint(
    request: KbDeleteRequest,
    raw_request: Request,
    _: None = Depends(require_bearer_token),
) -> KbDeleteResponse:
    # Type-the-name confirmation, re-checked server-side: this physically
    # removes the whole KB directory (raw docs + wiki) and is irreversible, so
    # it must never fire from a client that skipped the guard.
    if request.confirm_name != request.kb:
        raise HTTPException(status_code=400, detail="confirm_name does not match the KB name.")
    try:
        kb_dir = resolve_kb_alias(request.kb)
    except ValueError as exc:  # malformed name
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Accept a live KB dir OR a registered name whose directory is already gone
    # (a ghost entry) — delete_kb cleans up both; reject anything else with 404.
    registered = any(p == kb_dir for _, p in registered_kbs())
    if not _is_kb_dir(kb_dir) and not registered:
        raise HTTPException(status_code=404, detail=f"No knowledge base named {request.kb!r}.")
    # Stop the Drive poller (and cancel an in-flight manual sync) BEFORE
    # rmtree — otherwise the worker thread can write state back into kb_dir
    # and recreate .openkb after the delete, breaking a later re-init with
    # "already initialized".
    registry = getattr(raw_request.app.state, "gdrive_registry", None)
    if registry is not None:
        await run_in_threadpool(registry.stop, request.kb, persist_enabled=False)
    try:
        await run_in_threadpool(delete_kb, kb_dir)
    except ValueError as exc:  # resolved to an existing path that is not a KB
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError:
        pass  # a concurrent delete already removed it — idempotent success
    except OSError as exc:  # rmtree failed (permission/disk; a still-open file on Windows)
        raise HTTPException(
            status_code=500, detail=f"Failed to delete the knowledge base: {exc}"
        ) from exc
    return KbDeleteResponse(deleted=True, kb=request.kb, path=str(kb_dir))
