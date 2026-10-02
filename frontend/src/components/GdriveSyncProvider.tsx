import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, ChevronDown, ChevronUp, Circle, Loader2, MinusCircle, Square, Trash2, TriangleAlert, X } from "lucide-react"
import { toast } from "sonner"
import { getToken } from "@/api/client"
import { gdriveActiveSyncs, gdriveCancelSync, gdriveSyncEvents } from "@/api/connectors"
import { GdriveSyncContext } from "@/lib/gdrive-sync-context"
import { completedFileCount, createSyncProgress, progressFromSnapshot, updateSyncProgress, type SyncFileStatus, type SyncProgress } from "@/lib/gdrive-sync-state"
import { Progress } from "@/components/ui/progress"

function FileStatusIcon({ status }: { status: SyncFileStatus }) {
  if (["downloading", "processing", "removing"].includes(status)) return <Loader2 className="size-4 animate-spin text-accent-brand" />
  if (status === "added") return <CheckCircle2 className="size-4 text-green-600 dark:text-green-400" />
  if (status === "failed" || status === "interrupted") return <TriangleAlert className="size-4 text-amber-600 dark:text-amber-400" />
  if (status === "skipped") return <MinusCircle className="size-4 text-muted-foreground" />
  if (status === "removed") return <Trash2 className="size-4 text-muted-foreground" />
  return <Circle className="size-4 text-muted-foreground" />
}

export default function GdriveSyncProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation("common")
  const [progress, setProgress] = useState<SyncProgress | null>(null)
  const [collapsed, setCollapsed] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const controller = useRef<AbortController | null>(null)

  useEffect(() => () => controller.current?.abort(), [])

  // Restore the popup after a page reload: the sync worker keeps running
  // server-side, so poll its snapshot while no live SSE stream owns the state.
  useEffect(() => {
    let stopped = false
    const tick = async () => {
      if (stopped || controller.current || !getToken()) return
      try {
        const { syncs } = await gdriveActiveSyncs()
        if (stopped) return
        setProgress((current) => {
          if (!current) {
            const running = syncs.find((s) => s.phase === "running")
            return running ? progressFromSnapshot(running) : current
          }
          const mine = syncs.find((s) => s.kb === current.kb)
          return mine ? progressFromSnapshot(mine) : current
        })
      } catch {
        // Transient API failure — keep polling on the next tick.
      }
    }
    void tick()
    const id = setInterval(() => void tick(), 1500)
    return () => {
      stopped = true
      clearInterval(id)
    }
  }, [])

  const startSync = useCallback(async (kb: string) => {
    if (controller.current) return
    const request = new AbortController()
    controller.current = request
    setCancelling(false)
    setProgress(createSyncProgress(kb))
    setCollapsed(false)
    let terminal = false
    try {
      for await (const event of gdriveSyncEvents(kb, request.signal)) {
        setProgress((current) => current ? updateSyncProgress(current, event) : current)
        if (event.event === "final" || event.event === "error") terminal = true
        if (event.event === "done") break
      }
      if (!terminal) throw new Error(t("connector.gdrive.progress.connectionLost"))
    } catch (error) {
      if (!terminal && !request.signal.aborted) {
        const message = error instanceof Error ? error.message : t("connector.gdrive.errorToast")
        setProgress((current) => current ? updateSyncProgress(current, { event: "error", data: { message } }) : current)
      }
    } finally {
      controller.current = null
    }
  }, [t])

  const stopSync = useCallback(async () => {
    if (!progress || cancelling) return
    setCancelling(true)
    try {
      await gdriveCancelSync(progress.kb)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("connector.gdrive.errorToast"))
      setCancelling(false)
    }
  }, [progress, cancelling, t])

  const running = progress !== null && !["complete", "error", "cancelled"].includes(progress.phase)
  const completed = progress ? completedFileCount(progress) : 0
  const currentFile = progress?.files.find((file) => ["downloading", "processing", "removing"].includes(file.status))
  const title = progress?.phase === "complete" && progress.result?.failed
    ? t("connector.gdrive.progress.partial")
    : t(`connector.gdrive.progress.${progress?.phase ?? "waiting"}`)

  return (
    <GdriveSyncContext.Provider value={{ runningKb: running ? progress.kb : null, progress, startSync }}>
      {children}
      {progress && (
        <section
          aria-label={t("connector.gdrive.progress.title", { kb: progress.kb })}
          className="fixed bottom-4 right-4 z-[60] w-[390px] max-w-[calc(100vw-2rem)] rounded-2xl border border-border bg-popover text-popover-foreground shadow-xl"
        >
          <div className="flex items-center gap-2 px-4 pt-3 pb-2">
            {running ? <Loader2 className="size-4 shrink-0 animate-spin text-accent-brand" /> : progress.phase === "error" || progress.phase === "cancelled" || progress.result?.failed ? <TriangleAlert className="size-4 shrink-0 text-amber-600" /> : <CheckCircle2 className="size-4 shrink-0 text-green-600" />}
            <h2 className="min-w-0 flex-1 truncate text-[13px] font-semibold" title={progress.kb}>
              {t("connector.gdrive.progress.title", { kb: progress.kb })}
            </h2>
            {running && (
              <button
                type="button"
                onClick={() => void stopSync()}
                disabled={cancelling}
                className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-2 py-0.5 text-[11.5px] font-medium text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Square className="size-3" />
                {t(cancelling ? "connector.gdrive.progress.cancelling" : "connector.gdrive.progress.stop")}
              </button>
            )}
            <button
              type="button"
              onClick={() => setCollapsed((value) => !value)}
              aria-label={t(`connector.gdrive.progress.${collapsed ? "expand" : "collapse"}`)}
              aria-expanded={!collapsed}
              aria-controls="gdrive-sync-details"
              className="rounded p-1 hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
            >
              {collapsed ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
            </button>
            {!running && (
              <button type="button" onClick={() => setProgress(null)} aria-label={t("actions.close")} className="rounded p-1 hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring">
                <X className="size-4" />
              </button>
            )}
          </div>
          <div className="px-4 pb-3" role="status" aria-live="polite" aria-atomic="true">
            <p className="text-[12px] text-muted-foreground">{title}</p>
            {progress.files.length > 0 && (
              <p className="mt-1 text-[12px] tabular-nums">{t("connector.gdrive.progress.count", { completed, total: progress.files.length })}</p>
            )}
            {currentFile && <p className="mt-1 truncate text-[12px]" title={currentFile.name}>{t(`connector.gdrive.progress.file.${currentFile.status}`)}: {currentFile.name}</p>}
          </div>
          {!collapsed && (
            <div id="gdrive-sync-details" className="border-t border-border px-4 py-3">
              {progress.files.length > 0 && <Progress aria-label={t("connector.gdrive.progress.count", { completed, total: progress.files.length })} value={100 * completed / progress.files.length} className="mb-3 h-1.5" />}
              {progress.error && <p role="alert" className="mb-2 text-[12px] text-red-600 dark:text-red-400">{progress.error}</p>}
              {progress.phase === "complete" && progress.files.length === 0 && <p className="text-[12px] text-muted-foreground">{t("connector.gdrive.progress.empty")}</p>}
              <ul className="max-h-64 space-y-2 overflow-y-auto">
                {progress.files.map((file) => (
                  <li key={file.id} className="flex items-start gap-2 text-[12px]">
                    <span className="mt-0.5 shrink-0"><FileStatusIcon status={file.status} /></span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate" title={file.name}>{file.name}</p>
                      {file.message && <p className="mt-0.5 text-red-600 dark:text-red-400">{file.message}</p>}
                    </div>
                    <span className="shrink-0 text-muted-foreground">{t(`connector.gdrive.progress.file.${file.status}`)}</span>
                  </li>
                ))}
              </ul>
              {progress.result && <p className="mt-3 border-t border-border pt-2 text-[11.5px] text-muted-foreground">{t("connector.gdrive.syncToast", { ...progress.result })}</p>}
            </div>
          )}
        </section>
      )}
    </GdriveSyncContext.Provider>
  )
}
