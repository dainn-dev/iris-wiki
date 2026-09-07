import { apiFetch } from "./client"

export interface GdriveStatus {
  kb: string
  connected: boolean
  has_credentials: boolean
  oauth_configured: boolean
  auth_mode: string | null
  folder_id: string | null
  folder_name: string | null
  enabled: boolean
  poll_seconds: number
  last_sync_at: string | null
  error: string | null
  file_count: number
  active: boolean
  counters: Record<string, number>
}

export interface GdriveFolder {
  id: string
  name: string
}

export interface GdriveSyncResult {
  kb: string
  added: number
  skipped: number
  failed: number
  removed: number
}

export function gdriveStatus(kb: string): Promise<GdriveStatus> {
  return apiFetch<GdriveStatus>(
    `/api/v1/connectors/gdrive/status?kb=${encodeURIComponent(kb)}`,
  )
}

export function gdriveOAuthStart(kb: string): Promise<{ auth_url: string }> {
  return apiFetch<{ auth_url: string }>(
    `/api/v1/connectors/gdrive/oauth/start?kb=${encodeURIComponent(kb)}`,
  )
}

export function gdriveConnectSa(
  kb: string,
  serviceAccountJson: string,
  folderId?: string,
): Promise<GdriveStatus> {
  return apiFetch<GdriveStatus>("/api/v1/connectors/gdrive/connect", {
    body: {
      kb,
      service_account_json: serviceAccountJson,
      folder_id: folderId || null,
    },
  })
}

export function gdriveListFolders(
  kb: string,
  parent = "root",
): Promise<{ kb: string; parent_id: string; folders: GdriveFolder[] }> {
  return apiFetch(
    `/api/v1/connectors/gdrive/folders?kb=${encodeURIComponent(kb)}&parent=${encodeURIComponent(parent)}`,
  )
}

export function gdriveSetFolder(kb: string, folderId: string): Promise<GdriveStatus> {
  return apiFetch<GdriveStatus>("/api/v1/connectors/gdrive/folder", {
    body: { kb, folder_id: folderId },
  })
}

export function gdriveSyncNow(kb: string): Promise<GdriveSyncResult> {
  return apiFetch<GdriveSyncResult>("/api/v1/connectors/gdrive/sync/now", {
    body: { kb },
  })
}

export function gdriveSyncStop(kb: string): Promise<GdriveStatus> {
  return apiFetch<GdriveStatus>("/api/v1/connectors/gdrive/sync/stop", {
    body: { kb },
  })
}

export function gdriveDisconnect(kb: string): Promise<{ kb: string; disconnected: boolean }> {
  return apiFetch("/api/v1/connectors/gdrive/disconnect", { body: { kb } })
}
