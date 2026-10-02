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
from openkb.connectors.gdrive import DriveFile, GdriveError, download_file, list_ingestible_files
from openkb.connectors.store import FileRecord, GdriveState, load_state, save_state

logger = logging.getLogger(__name__)

_MAX_EVENTS = 200


class SyncInProgressError(GdriveError):
    """A Drive sync is already running for this KB (manual or poller)."""


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
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Poll the connected folder once and ingest new/changed/deleted files.

    Returns counters ``added``, ``skipped``, ``failed``, ``removed`` — plus
    ``cancelled: 1`` when ``is_cancelled`` stopped the run between files
    (a file mid-download/mid-ingest always finishes; already-processed files
    stay recorded in ``state.files``).
    """
    list_fn = list_files or _default_list
    fetch_fn = fetch_bytes or _default_fetch
    add_fn = add_file or _default_add
    remove_fn = remove_file or _default_remove

    def emit(event: str, data: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event, data)

    def cancelled() -> bool:
        return is_cancelled is not None and is_cancelled()

    def progress(fid: str, name: str, status: str, message: str | None = None) -> None:
        data = {"id": fid, "name": name, "status": status}
        if message:
            data["message"] = message
        emit("file", data)

    state = load_state(kb_dir)
    counters = {"added": 0, "skipped": 0, "failed": 0, "removed": 0}
    if not state.folder_id:
        state.error = "No Drive folder is selected."
        save_state(kb_dir, state)
        raise GdriveError(state.error)

    emit("scanning", {})
    try:
        remote = list_fn(kb_dir, state.folder_id)
    except Exception as exc:
        state.error = str(exc)
        save_state(kb_dir, state)
        raise

    raw_dir = kb_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    remote_ids = {f.id for f in remote}
    deleted = [(fid, rec) for fid, rec in state.files.items() if fid not in remote_ids]
    emit(
        "files",
        {
            "files": [{"id": f.id, "name": f.name} for f in remote]
            + [{"id": fid, "name": rec.name} for fid, rec in deleted]
        },
    )

    was_cancelled = False
    for file in remote:
        if cancelled():
            was_cancelled = True
            break
        prev = state.files.get(file.id)
        if prev is not None and prev.modified_time == file.modified_time:
            progress(file.id, file.name, "skipped")
            continue
        progress(file.id, file.name, "downloading")
        try:
            content = fetch_fn(kb_dir, file)
        except Exception as exc:
            logger.warning("Google Drive download failed for %s: %s", file.name, exc)
            counters["failed"] += 1
            progress(file.id, file.name, "failed", "Could not download this Drive file.")
            continue
        digest = _sha256(content)
        if prev is not None and prev.sha256 == digest:
            prev.modified_time = file.modified_time
            progress(file.id, file.name, "skipped")
            continue
        dest = raw_dir / file.local_name
        dest.write_bytes(content)
        progress(file.id, file.name, "processing")
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
            progress(file.id, file.name, "failed", "Could not compile this file into the KB.")
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
            progress(file.id, file.name, "failed", "Could not compile this file into the KB.")
            continue
        else:
            counters["added"] += 1
            status = "added"
        state.files[file.id] = FileRecord(
            name=file.name,
            modified_time=file.modified_time,
            sha256=digest,
            local_name=file.local_name,
        )
        progress(file.id, file.name, status)

    for old_id, rec in deleted if not was_cancelled else []:
        if cancelled():
            was_cancelled = True
            break
        progress(old_id, rec.name, "removing")
        try:
            remove_fn(kb_dir, rec.local_name)
            counters["removed"] += 1
        except Exception as exc:
            logger.warning("Could not remove deleted Drive file %s: %s", rec.local_name, exc)
            counters["failed"] += 1
            progress(old_id, rec.name, "failed", "Could not remove this file from the KB.")
            continue
        del state.files[old_id]
        progress(old_id, rec.name, "removed")

    state.last_sync_at = _now_iso()
    state.error = None
    save_state(kb_dir, state)
    return {**counters, "cancelled": int(was_cancelled)}


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
    # Set (via ``stop``) to abort an in-flight poll between files.
    cancelled: threading.Event = field(default_factory=threading.Event)
    events: deque = field(default_factory=lambda: deque(maxlen=_MAX_EVENTS))
    counters: dict[str, int] = field(
        default_factory=lambda: {"added": 0, "skipped": 0, "failed": 0, "removed": 0}
    )
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.running.set()


@dataclass
class ManualSyncProgress:
    """Server-side snapshot of one ``sync_now`` run.

    Kept after the run finishes (until the next ``sync_now`` for the same KB
    overwrites it) so a reloaded web UI can rebuild the progress popup via
    ``GET /sync/active`` instead of losing it with the SSE connection.
    ``phase``: ``running`` | ``done`` | ``cancelled`` | ``error``.
    """

    kb: str
    started_at: float
    cancel: threading.Event = field(default_factory=threading.Event)
    phase: str = "running"
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, event: str, data: dict[str, Any]) -> None:
        with self.lock:
            if event == "files":
                self.files = {
                    item["id"]: {"id": item["id"], "name": item["name"], "status": "pending"}
                    for item in data.get("files", [])
                }
            elif event == "file":
                self.files[str(data["id"])] = dict(data)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "kb": self.kb,
                "phase": self.phase,
                "started_at": self.started_at,
                "files": [dict(f) for f in self.files.values()],
                "result": dict(self.result) if self.result is not None else None,
                "error": self.error,
            }


def _record_event(state: SyncerState, event: str, data: dict[str, Any]) -> None:
    with state._lock:
        state._seq += 1
        state.events.append({"seq": state._seq, "ts": time.time(), "event": event, "data": data})


def _inc(state: SyncerState, key: str, n: int = 1) -> None:
    with state._lock:
        state.counters[key] = state.counters.get(key, 0) + n


def _run_poll(
    syncer: SyncerState,
    *,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    raise_errors: bool = False,
) -> dict[str, int] | None:
    with syncer.sync_lock:
        try:
            result = sync_once(
                syncer.kb_dir,
                on_event=on_event,
                is_cancelled=lambda: not syncer.running.is_set() or syncer.cancelled.is_set(),
            )
        except Exception as exc:
            _record_event(syncer, "error", {"message": str(exc)})
            logger.warning("Google Drive sync failed for KB %s: %s", syncer.kb, exc)
            if raise_errors:
                raise
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
        self._progress: dict[str, ManualSyncProgress] = {}

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

    def sync_now(
        self,
        kb: str,
        kb_dir: Path,
        *,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, int]:
        """Run one poll immediately (also wakes the background worker).

        Refuses to overlap: raises :class:`SyncInProgressError` when the
        background poller holds ``sync_lock`` or another ``sync_now`` is
        still running. Records a :class:`ManualSyncProgress` snapshot so a
        reloaded UI can rebuild the popup via :meth:`sync_progress`.
        """
        syncer = self.get(kb)
        acquired = False
        if syncer is not None:
            # Non-blocking: a second run must not queue behind the poller and
            # silently re-scan the folder — the caller gets a 409 instead.
            acquired = syncer.sync_lock.acquire(blocking=False)
            if not acquired:
                raise SyncInProgressError("A Drive sync is already in progress for this KB.")
        try:
            with self._lock:
                existing = self._progress.get(kb)
                if existing is not None and existing.phase == "running":
                    raise SyncInProgressError("A Drive sync is already in progress for this KB.")
                prog = ManualSyncProgress(kb=kb, started_at=time.time())
                self._progress[kb] = prog

            def record(event: str, data: dict[str, Any]) -> None:
                prog.record(event, data)
                if on_event is not None:
                    on_event(event, data)

            try:
                result = sync_once(kb_dir, on_event=record, is_cancelled=prog.cancel.is_set)
            except Exception as exc:
                with prog.lock:
                    prog.phase = "error"
                    prog.error = str(exc)
                if syncer is not None:
                    _record_event(syncer, "error", {"message": str(exc)})
                raise
            if syncer is not None:
                for key, n in result.items():
                    _inc(syncer, key, n)
                _record_event(syncer, "sync", dict(result))
            with prog.lock:
                prog.result = dict(result)
                prog.phase = "cancelled" if result.get("cancelled") else "done"
            return result
        finally:
            if acquired and syncer is not None:
                syncer.sync_lock.release()

    def cancel_sync(self, kb: str) -> bool:
        """Request cancellation of an in-flight ``sync_now`` for ``kb``.

        Cooperative: the worker finishes the file it is on, then stops.
        Returns False when no manual sync is running.
        """
        with self._lock:
            prog = self._progress.get(kb)
        if prog is None or prog.phase != "running":
            return False
        prog.cancel.set()
        return True

    def sync_progress(self) -> list[dict[str, Any]]:
        """Snapshots of every manual sync (running and finished), for UI restore."""
        with self._lock:
            progs = list(self._progress.values())
        return [prog.snapshot() for prog in progs]

    def stop(self, kb: str, *, persist_enabled: bool = True) -> bool:
        with self._lock:
            syncer = self._syncers.get(kb)
            prog = self._progress.get(kb)
        # Cancel an in-flight manual sync too — otherwise its worker keeps
        # writing into kb_dir (e.g. recreating .openkb after a KB delete).
        if prog is not None and prog.phase == "running":
            prog.cancel.set()
        if syncer is None:
            return False
        syncer.cancelled.set()
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
