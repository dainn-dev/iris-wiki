import type { GdriveSyncResult } from "../api/connectors"

export type SyncFileStatus =
  | "pending" | "downloading" | "processing" | "removing"
  | "added" | "skipped" | "failed" | "removed" | "interrupted"

export interface SyncFile {
  id: string
  name: string
  status: SyncFileStatus
  message?: string
}

export interface SyncProgress {
  kb: string
  phase: "waiting" | "scanning" | "syncing" | "complete" | "error" | "cancelled"
  files: SyncFile[]
  result: GdriveSyncResult | null
  error: string | null
}

export type GdriveSyncEvent =
  | { event: "start" | "scanning" | "done"; data: Record<string, unknown> }
  | { event: "files"; data: { files: { id: string; name: string }[] } }
  | { event: "file"; data: SyncFile }
  | { event: "final"; data: GdriveSyncResult }
  | { event: "error"; data: { message: string } }

const completedStatuses = new Set<SyncFileStatus>(["added", "skipped", "failed", "removed"])

export function createSyncProgress(kb: string): SyncProgress {
  return { kb, phase: "waiting", files: [], result: null, error: null }
}

export function completedFileCount(state: SyncProgress): number {
  return state.files.filter((file) => completedStatuses.has(file.status)).length
}

export function updateSyncProgress(state: SyncProgress, event: GdriveSyncEvent): SyncProgress {
  switch (event.event) {
    case "scanning":
      return { ...state, phase: "scanning" }
    case "files":
      return { ...state, phase: "syncing", files: event.data.files.map((file) => ({ ...file, status: "pending" })) }
    case "file":
      return { ...state, files: state.files.map((file) => file.id === event.data.id ? { ...file, ...event.data } : file) }
    case "final": {
      const cancelled = Boolean(event.data.cancelled)
      return {
        ...state,
        phase: cancelled ? "cancelled" : "complete",
        result: event.data,
        files: cancelled
          ? state.files.map((file) =>
              completedStatuses.has(file.status) ? file : { ...file, status: "interrupted" },
            )
          : state.files,
      }
    }
    case "error":
      return {
        ...state,
        phase: "error",
        error: event.data.message,
        files: state.files.map((file) => completedStatuses.has(file.status) ? file : { ...file, status: "interrupted" }),
      }
    default:
      return state
  }
}

/** Rebuild popup state from a server-side snapshot (page reload mid-sync). */
export function progressFromSnapshot(snapshot: {
  kb: string
  phase: "running" | "done" | "cancelled" | "error"
  files: SyncFile[]
  result: GdriveSyncResult | null
  error: string | null
}): SyncProgress {
  const terminal = snapshot.phase !== "running"
  const phase: SyncProgress["phase"] =
    snapshot.phase === "running"
      ? snapshot.files.length > 0
        ? "syncing"
        : "scanning"
      : snapshot.phase === "done"
        ? "complete"
        : snapshot.phase
  return {
    kb: snapshot.kb,
    phase,
    files: snapshot.files.map((file) =>
      terminal && !completedStatuses.has(file.status)
        ? { ...file, status: "interrupted" }
        : { ...file },
    ),
    result: snapshot.result,
    error: snapshot.error,
  }
}
