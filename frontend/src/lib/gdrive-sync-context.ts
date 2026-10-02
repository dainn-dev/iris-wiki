import { createContext, useContext } from "react"
import type { SyncProgress } from "./gdrive-sync-state"

export const GdriveSyncContext = createContext<{
  runningKb: string | null
  /** Live sync state (stream-driven or restored from the server snapshot). */
  progress: SyncProgress | null
  startSync: (kb: string) => Promise<void>
} | null>(null)

export function useGdriveSync() {
  const context = useContext(GdriveSyncContext)
  if (!context) throw new Error("Google Drive sync provider is missing")
  return context
}
