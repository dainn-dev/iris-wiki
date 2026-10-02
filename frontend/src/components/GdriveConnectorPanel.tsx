import { useCallback, useEffect, useState } from "react"
import { useSearchParams } from "react-router"
import { useTranslation } from "react-i18next"
import { Cloud, FolderOpen, Loader2, Pause, Play, RefreshCw, Unplug } from "lucide-react"
import { toast } from "sonner"
import { useGdriveSync } from "@/lib/gdrive-sync-context"
import {
  gdriveConnectSa,
  gdriveDisconnect,
  gdriveListFolders,
  gdriveOAuthStart,
  gdriveSetFolder,
  gdriveStatus,
  gdriveSyncStop,
  type GdriveFolder,
  type GdriveStatus,
} from "@/api/connectors"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e))

const inputCls =
  "mt-1.5 w-full rounded-md border border-input bg-transparent px-3 py-2 text-[13px] font-mono2 outline-none focus-visible:ring-2 focus-visible:ring-ring focus:border-accent-brand"

export default function GdriveConnectorPanel({
  kb,
  onSynced,
}: {
  kb: string
  onSynced?: () => void
}) {
  const { t } = useTranslation("common")
  const [params, setParams] = useSearchParams()
  const [status, setStatus] = useState<GdriveStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [saOpen, setSaOpen] = useState(false)
  const [saJson, setSaJson] = useState("")
  const [saFolder, setSaFolder] = useState("")
  const [pickerOpen, setPickerOpen] = useState(false)
  const [parentId, setParentId] = useState("root")
  const [folders, setFolders] = useState<GdriveFolder[]>([])
  const [pasteId, setPasteId] = useState("")
  const { runningKb, startSync } = useGdriveSync()

  const refresh = useCallback(async () => {
    if (!kb) return
    setLoading(true)
    try {
      setStatus(await gdriveStatus(kb))
    } catch (e) {
      setStatus(null)
      toast.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [kb])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    const flag = params.get("gdrive")
    if (!flag) return
    const message = params.get("message")
    if (flag === "connected") toast.success(t("connector.gdrive.connectedToast"))
    else toast.error(message || t("connector.gdrive.errorToast"))
    const next = new URLSearchParams(params)
    next.delete("gdrive")
    next.delete("message")
    setParams(next, { replace: true })
    void refresh()
  }, [params, refresh, setParams, t])

  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true)
    try {
      await fn()
      if (ok) toast.success(ok)
      await refresh()
      onSynced?.()
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  const connectGoogle = () =>
    run(async () => {
      const { auth_url } = await gdriveOAuthStart(kb)
      window.location.href = auth_url
    })

  const submitSa = () =>
    run(async () => {
      await gdriveConnectSa(kb, saJson, saFolder.trim() || undefined)
      setSaOpen(false)
      setSaJson("")
      setSaFolder("")
    }, t("connector.gdrive.connectedToast"))

  const openPicker = async () => {
    setPickerOpen(true)
    setParentId("root")
    setBusy(true)
    try {
      const r = await gdriveListFolders(kb, "root")
      setFolders(r.folders)
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  const drill = async (id: string) => {
    setParentId(id)
    setBusy(true)
    try {
      const r = await gdriveListFolders(kb, id)
      setFolders(r.folders)
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  const chooseFolder = (id: string) =>
    run(async () => {
      await gdriveSetFolder(kb, id)
      setPickerOpen(false)
      void startSync(kb)
    }, t("connector.gdrive.folderSetToast"))

  const syncNow = () =>
    run(async () => {
      await startSync(kb)
    })

  if (!kb) {
    return (
      <p className="text-[13px] text-muted-foreground">{t("connector.gdrive.pickKb")}</p>
    )
  }

  return (
    <>
      <div className="rounded-2xl border border-[hsl(var(--glass-border))] glass px-4 py-3.5">
        <div className="flex items-center gap-3">
          <span className="w-9 h-9 rounded-xl glass-2 border border-[hsl(var(--glass-border))] grid place-items-center shrink-0">
            <Cloud className="w-4 h-4 text-accent-brand" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-medium text-foreground">
              {t("connector.gdrive.title")}
            </div>
            <div className="text-[11.5px] text-muted-foreground mt-0.5">
              {loading
                ? t("loading")
                : status?.connected
                  ? status.folder_name
                    ? t("connector.gdrive.syncingFolder", { name: status.folder_name })
                    : t("connector.gdrive.pickFolder")
                  : t("connector.gdrive.disconnected")}
            </div>
          </div>
        </div>

        {status?.error && (
          <p className="mt-2 text-[12px] text-red-600 dark:text-red-400">{status.error}</p>
        )}
        {status?.last_sync_at && (
          <p className="mt-2 text-[11.5px] text-muted-foreground">
            {t("connector.gdrive.lastSync", { time: status.last_sync_at.replace("T", " ").replace("Z", " UTC") })}
            {status.file_count > 0
              ? ` · ${t("connector.gdrive.fileCount", { count: status.file_count })}`
              : ""}
            {status.enabled
              ? ` · ${status.active ? t("connector.gdrive.polling") : t("connector.gdrive.paused")}`
              : ""}
          </p>
        )}

        <div className="mt-3 flex flex-wrap gap-2">
          {!status?.connected && (
            <>
              <button
                type="button"
                disabled={busy || !status?.oauth_configured}
                title={!status?.oauth_configured ? t("connector.gdrive.oauthMissing") : undefined}
                onClick={() => void connectGoogle()}
                className="h-8 px-3 rounded-lg bg-accent-brand text-white text-[12.5px] font-medium disabled:opacity-50"
              >
                {t("connector.gdrive.connectGoogle")}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => setSaOpen(true)}
                className="h-8 px-3 rounded-lg border border-[hsl(var(--glass-border))] text-[12.5px] font-medium"
              >
                {t("connector.gdrive.connectSa")}
              </button>
            </>
          )}
          {status?.connected && (
            <>
              <button
                type="button"
                disabled={busy}
                onClick={() => void openPicker()}
                className="h-8 px-3 rounded-lg border border-[hsl(var(--glass-border))] text-[12.5px] inline-flex items-center gap-1.5"
              >
                <FolderOpen className="w-3.5 h-3.5" />
                {t("connector.gdrive.chooseFolder")}
              </button>
              {status.folder_id && (
                <button
                  type="button"
                  disabled={busy || Boolean(runningKb)}
                  onClick={syncNow}
                  className="h-8 px-3 rounded-lg bg-accent-brand text-white text-[12.5px] inline-flex items-center gap-1.5 disabled:opacity-50"
                >
                  {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                  {t("connector.gdrive.syncNow")}
                </button>
              )}
              {status.folder_id && status.enabled && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void run(async () => gdriveSyncStop(kb), t("connector.gdrive.pausedToast"))}
                  className="h-8 px-3 rounded-lg border border-[hsl(var(--glass-border))] text-[12.5px] inline-flex items-center gap-1.5"
                >
                  <Pause className="w-3.5 h-3.5" />
                  {t("connector.gdrive.pause")}
                </button>
              )}
              {status.folder_id && !status.enabled && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => gdriveSetFolder(kb, status.folder_id!), t("connector.gdrive.resumedToast"))
                  }
                  className="h-8 px-3 rounded-lg border border-[hsl(var(--glass-border))] text-[12.5px] inline-flex items-center gap-1.5"
                >
                  <Play className="w-3.5 h-3.5" />
                  {t("connector.gdrive.resume")}
                </button>
              )}
              <button
                type="button"
                disabled={busy}
                onClick={() => void run(async () => gdriveDisconnect(kb), t("connector.gdrive.disconnectedToast"))}
                className="h-8 px-3 rounded-lg text-[12.5px] text-red-600 dark:text-red-400 inline-flex items-center gap-1.5"
              >
                <Unplug className="w-3.5 h-3.5" />
                {t("connector.gdrive.disconnect")}
              </button>
            </>
          )}
        </div>
      </div>

      <Dialog open={saOpen} onOpenChange={setSaOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{t("connector.gdrive.saTitle")}</DialogTitle>
            <DialogDescription>{t("connector.gdrive.saDesc")}</DialogDescription>
          </DialogHeader>
          <textarea
            className={`${inputCls} min-h-36`}
            spellCheck={false}
            placeholder="{ ... }"
            value={saJson}
            onChange={(e) => setSaJson(e.target.value)}
          />
          <label className="block text-[12.5px] text-muted-foreground">
            {t("connector.gdrive.folderIdOptional")}
            <input
              className={inputCls}
              value={saFolder}
              onChange={(e) => setSaFolder(e.target.value)}
              placeholder="1abc…"
            />
          </label>
          <DialogFooter>
            <button
              type="button"
              disabled={busy || !saJson.trim()}
              onClick={() => void submitSa()}
              className="h-9 px-4 rounded-xl bg-accent-brand text-white text-[13px] font-medium disabled:opacity-50"
            >
              {t("connector.gdrive.connectSa")}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={pickerOpen} onOpenChange={setPickerOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{t("connector.gdrive.chooseFolder")}</DialogTitle>
            <DialogDescription>{t("connector.gdrive.pickerDesc")}</DialogDescription>
          </DialogHeader>
          <label className="block text-[12.5px] text-muted-foreground">
            {t("connector.gdrive.folderIdOptional")}
            <input
              className={inputCls}
              value={pasteId}
              onChange={(e) => setPasteId(e.target.value)}
              placeholder="1abc…"
            />
          </label>
          <div className="max-h-56 overflow-y-auto rounded-lg border border-[hsl(var(--glass-border))] divide-y divide-[hsl(var(--glass-border))]">
            {folders.length === 0 && (
              <p className="px-3 py-4 text-[12.5px] text-muted-foreground">{t("connector.gdrive.noFolders")}</p>
            )}
            {folders.map((f) => (
              <div key={f.id} className="flex items-center gap-2 px-3 py-2">
                <button
                  type="button"
                  className="flex-1 text-left text-[13px] truncate hover:text-accent-brand"
                  onClick={() => void drill(f.id)}
                >
                  {f.name}
                </button>
                <button
                  type="button"
                  className="text-[12px] text-accent-brand shrink-0"
                  onClick={() => void chooseFolder(f.id)}
                >
                  {t("connector.gdrive.useFolder")}
                </button>
              </div>
            ))}
          </div>
          <DialogFooter>
            {parentId !== "root" && (
              <button
                type="button"
                className="h-9 px-3 rounded-xl border border-[hsl(var(--glass-border))] text-[13px]"
                onClick={() => void chooseFolder(parentId)}
              >
                {t("connector.gdrive.useThisFolder")}
              </button>
            )}
            <button
              type="button"
              disabled={!pasteId.trim()}
              className="h-9 px-4 rounded-xl bg-accent-brand text-white text-[13px] disabled:opacity-50"
              onClick={() => void chooseFolder(pasteId.trim())}
            >
              {t("connector.gdrive.usePastedId")}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
