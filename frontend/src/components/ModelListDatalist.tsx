import { useEffect, useState } from "react"
import { getModelList } from "@/api/config"

/** Shared id linking a text `<input list=…>` to the fetched model suggestions. */
export const MODEL_LIST_ID = "llm-models"

// Module-level cache + in-flight dedupe: model lists are keyed by KB (a KB may
// point at a different API base) and never refetched within a session — model
// catalogs change rarely, and Settings/sheets are reopened often.
const cache = new Map<string, string[]>()
const inflight = new Map<string, Promise<string[]>>()

function fetchModels(kb: string | undefined): Promise<string[]> {
  const key = kb ?? ""
  const cached = cache.get(key)
  if (cached) return Promise.resolve(cached)
  let pending = inflight.get(key)
  if (!pending) {
    pending = getModelList(kb)
      .then((r) => r.models)
      .catch(() => [] as string[])
    inflight.set(key, pending)
    void pending.finally(() => inflight.delete(key))
  }
  return pending.then((ids) => {
    cache.set(key, ids)
    return ids
  })
}

/**
 * A native `<datalist>` of model ids fetched from `{OPENAI_API_BASE}/models`,
 * turning the plain model text input into a combobox: the input stays
 * free-text (any id can be typed) while the catalog surfaces as suggestions.
 * Reference it from an input via `list={MODEL_LIST_ID}`; render once anywhere
 * in the same document as the input(s) using it. Fails silent — on a fetch
 * error the input simply behaves as plain text.
 */
export function ModelListDatalist({ kb }: { kb?: string }) {
  const [models, setModels] = useState<string[]>(cache.get(kb ?? "") ?? [])
  useEffect(() => {
    let cancelled = false
    void fetchModels(kb).then((ids) => {
      if (!cancelled) setModels(ids)
    })
    return () => {
      cancelled = true
    }
  }, [kb])
  return (
    <datalist id={MODEL_LIST_ID}>
      {models.map((id) => (
        <option key={id} value={id} />
      ))}
    </datalist>
  )
}
