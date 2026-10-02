import { getModelList } from "@/api/config"

// Module-level cache + in-flight dedupe: model lists are keyed by KB (a KB may
// point at a different API base) and never refetched within a session — model
// catalogs change rarely, and pickers are opened often.
const cache = new Map<string, string[]>()
const inflight = new Map<string, Promise<string[]>>()

/** Fetch (deduped + session-cached) the model catalog for a KB's API base. */
export function fetchModels(kb: string | undefined): Promise<string[]> {
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
