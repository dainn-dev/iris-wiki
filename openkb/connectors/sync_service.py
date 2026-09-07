"""Background Google Drive folder poller, modeled on ``watch_service``.

One daemon worker thread per KB. ``sync_once`` is the testable unit of work
(list → download/export → ``_add_for_api`` / ``run_remove_for_api``). Enabled
connectors are resumed from disk on API lifespan start.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from openkb.cli import _add_for_api, run_remove_for_api
from openkb.config import resolve_credential_bundle
from openkb.connectors.gdrive import DriveFile, download_file, list_ingestible_files
from openkb.connectors.store import FileRecord, GdriveState, load_state, save_state

logger = logging.getLogger(__name__)

_MAX_EVENTS = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _default_list(kb_dir: Path, folder_id: str) -> list[DriveFile]:
    return list_ingestible_files(kb_dir, folder_id)


def _default_fetch(kb_dir: Path, file: DriveFile) -> bytes:
    return download_file(kb_dir, file)


def _default_add(path: Path, kb_dir: Path) -> Any:
    bundle = resolve_credential_bundle(kb_dir)
    return _add_for_api(path, kb_dir, bundle=bundle)


def _default_remove(kb_dir: Path, identifier: str) -> Any:
    return run_remove_for_api(kb_dir, identifier)


def sync_once(
    kb_dir: Path,
    *,
    list_files: Callable[[Path, str], list[DriveFile]] | None = None,
    fetch_bytes: Callable[[Path, DriveFile], bytes] | None = None,
    add_file: Callable[[Path, Path], Any] | None = None,
    remove_file: Callable[[Path, str], Any] | None = None,
) -> dict[str, int]:
    """Poll the connected folder once and ingest new/changed/deleted files.

    Returns counters ``added``, ``skipped``, ``failed``, ``removed``.
    """
    list_fn = list_files or _default_list
    fetch_fn = fetch_bytes or _default_fetch
    add_fn = add_file or _default_add
    remove_fn = remove_file or _default_remove

    state = load_state(kb_dir)
    counters = {"added": 0, "skipped": 0, "failed": 0, "removed": 0}
    if not state.folder_id:
        state.error = "No Drive folder is selected."
        save_state(kb_dir, state)
        return counters

    try:
        remote = list_fn(kb_dir, state.folder_id)
    except Exception as exc:
        state.error = str(exc)
        save_state(kb_dir, state)
        raise

    raw_dir = kb_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    remote_ids = {f.id for f in remote}

    for file in remote:
        prev = state.files.get(file.id)
        if prev is not None and prev.modified_time == file.modified_time:
            continue
        try:
            content = fetch_fn(kb_dir, file)
        except Exception as exc:
            logger.warning("Google Drive download failed for %s: %s", file.name, exc)
            counters["failed"] += 1
            continue
        digest = _sha256(content)
        if prev is not None and prev.sha256 == digest:
            prev.modified_time = file.modified_time
            continue
        dest = raw_dir / file.local_name
        dest.write_bytes(content)
        if prev is not None:
            try:
                remove_fn(kb_dir, prev.local_name)
            except Exception as exc:
                logger.warning("Could not remove previous ingest of %s: %s", prev.local_name, exc)
        try:
            result = add_fn(dest, kb_dir)
        except Exception as exc:
            logger.warning("Ingest failed for %s: %s", file.local_name, exc)
            dest.unlink(missing_ok=True)
            counters["failed"] += 1
            continue
        status = getattr(result, "status", None) or (
            result.get("status") if isinstance(result, dict) else "added"
        )
        if status == "skipped":
            dest.unlink(missing_ok=True)
            counters["skipped"] += 1
        elif status == "failed":
            dest.unlink(missing_ok=True)
            counters["failed"] += 1
            continue
        else:
            counters["added"] += 1
        state.files[file.id] = FileRecord(
            name=file.name,
            modified_time=file.modified_time,
            sha256=digest,
            local_name=file.local_name,
        )

    for old_id, rec in list(state.files.items()):
        if old_id in remote_ids:
            continue
        try:
            remove_fn(kb_dir, rec.local_name)
            counters["removed"] += 1
        except Exception as exc:
            logger.warning("Could not remove deleted Drive file %s: %s", rec.local_name, exc)
            counters["failed"] += 1
            continue
        del state.files[old_id]

    state.last_sync_at = _now_iso()
    state.error = None
    save_state(kb_dir, state)
    return counters


@dataclass
class SyncerState:
    kb: str
    kb_dir: Path
    poll_seconds: float
    started_at: float
    worker_thread: threading.Thread | None = None
    running: threading.Event = field(default_factory=threading.Event)
    wake: threading.Event = field(default_factory=threading.Event)
    sync_lock: threading.Lock = field(default_factory=threading.Lock)
    events: deque = field(default_factory=lambda: deque(maxlen=_MAX_EVENTS))
    counters: dict[str, int] = field(
        default_factory=lambda: {"added": 0, "skipped": 0, "failed": 0, "removed": 0}
    )
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.running.set()


def _record_event(state: SyncerState, event: str, data: dict[str, Any]) -> None:
    with state._lock:
        state._seq += 1
        state.events.append({"seq": state._seq, "ts": time.time(), "event": event, "data": data})


def _inc(state: SyncerState, key: str, n: int = 1) -> None:
    with state._lock:
        state.counters[key] = state.counters.get(key, 0) + n


def _run_poll(syncer: SyncerState) -> dict[str, int] | None:
    with syncer.sync_lock:
        try:
            result = sync_once(syncer.kb_dir)
        except Exception as exc:
            _record_event(syncer, "error", {"message": str(exc)})
            logger.warning("Google Drive sync failed for KB %s: %s", syncer.kb, exc)
            return None
    for key, n in result.items():
        _inc(syncer, key, n)
    _record_event(syncer, "sync", dict(result))
    return result


def _run_worker(syncer: SyncerState) -> None:
    # Immediate first poll, then wait poll_seconds (or a wake for sync-now).
    while syncer.running.is_set():
        _run_poll(syncer)
        if not syncer.running.is_set():
            break
        syncer.wake.wait(timeout=syncer.poll_seconds)
        syncer.wake.clear()


class GDriveSyncRegistry:
    """One Drive poller per KB; isolated per FastAPI app instance."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._syncers: dict[str, SyncerState] = {}

    def start(self, kb: str, kb_dir: Path) -> SyncerState:
        disk = load_state(kb_dir)
        with self._lock:
            existing = self._syncers.get(kb)
            if existing is not None and existing.running.is_set():
                existing.poll_seconds = float(disk.poll_seconds)
                return existing
            syncer = SyncerState(
                kb=kb,
                kb_dir=kb_dir,
                poll_seconds=float(disk.poll_seconds),
                started_at=time.time(),
            )
            syncer.worker_thread = threading.Thread(
                target=_run_worker,
                args=(syncer,),
                daemon=True,
                name=f"openkb-gdrive-{kb}",
            )
            self._syncers[kb] = syncer
        disk.enabled = True
        save_state(kb_dir, disk)
        syncer.worker_thread.start()
        _record_event(syncer, "started", {"kb": kb})
        return syncer

    def get(self, kb: str) -> SyncerState | None:
        with self._lock:
            return self._syncers.get(kb)

    def status(self, kb: str) -> dict[str, Any]:
        with self._lock:
            syncer = self._syncers.get(kb)
        if syncer is None:
            return {"active": False, "counters": {}}
        with syncer._lock:
            counters = dict(syncer.counters)
        return {
            "active": syncer.running.is_set(),
            "started_at": syncer.started_at,
            "poll_seconds": syncer.poll_seconds,
            "counters": counters,
        }

    def sync_now(self, kb: str, kb_dir: Path) -> dict[str, int]:
        """Run one poll immediately (also wakes the background worker)."""
        syncer = self.get(kb)
        if syncer is not None:
            result = _run_poll(syncer)
            return result or {"added": 0, "skipped": 0, "failed": 0, "removed": 0}
        return sync_once(kb_dir)

    def stop(self, kb: str, *, persist_enabled: bool = True) -> bool:
        with self._lock:
            syncer = self._syncers.get(kb)
        if syncer is None:
            return False
        syncer.running.clear()
        syncer.wake.set()
        if syncer.worker_thread is not None:
            syncer.worker_thread.join(timeout=5.0)
        with self._lock:
            if self._syncers.get(kb) is syncer:
                self._syncers.pop(kb, None)
        if persist_enabled:
            disk = load_state(syncer.kb_dir)
            disk.enabled = False
            save_state(syncer.kb_dir, disk)
        return True

    def stop_all(self) -> None:
        for kb in list(self.list_active()):
            self.stop(kb, persist_enabled=False)

    def list_active(self) -> list[str]:
        with self._lock:
            return list(self._syncers.keys())

    def resume_all(self) -> None:
        """Start pollers for every registered KB with an enabled Drive connector."""
        from openkb.api_kbs import _list_knowledge_bases

        listing = _list_knowledge_bases()
        for item in listing.get("knowledge_bases") or []:
            name = item.get("name")
            path_s = item.get("path")
            if not name or not path_s:
                continue
            kb_dir = Path(path_s)
            disk = load_state(kb_dir)
            if disk.enabled and disk.folder_id and disk.auth_mode:
                try:
                    self.start(name, kb_dir)
                except Exception:
                    logger.warning(
                        "Could not resume Google Drive sync for KB %s",
                        name,
                        exc_info=True,
                    )


def pause_connector(kb: str, kb_dir: Path, registry: GDriveSyncRegistry) -> None:
    """Stop the poller and persist ``enabled: false``."""
    registry.stop(kb, persist_enabled=True)
    disk = load_state(kb_dir)
    disk.enabled = False
    save_state(kb_dir, disk)


def enable_folder(kb_dir: Path, folder_id: str, folder_name: str) -> GdriveState:
    state = load_state(kb_dir)
    state.folder_id = folder_id
    state.folder_name = folder_name
    state.enabled = True
    state.error = None
    save_state(kb_dir, state)
    return state
