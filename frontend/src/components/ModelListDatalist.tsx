import { useEffect, useState } from "react"
import { fetchModels } from "@/lib/model-catalog"

/** Shared id linking a text `<input list=…>` to the fetched model suggestions. */
export const MODEL_LIST_ID = "llm-models"

// The catalog itself is fetched via @/lib/model-catalog (session-cached +
// in-flight deduped, shared with the chat composer's model picker).

/**
 * A native `<datalist>` of model ids fetched from `{OPENAI_API_BASE}/models`,
 * turning the plain model text input into a combobox: the input stays
 * free-text (any id can be typed) while the catalog surfaces as suggestions.
 * Reference it from an input via `list={MODEL_LIST_ID}`; render once anywhere
 * in the same document as the input(s) using it. Fails silent — on a fetch
 * error the input simply behaves as plain text.
 */
export function ModelListDatalist({ kb }: { kb?: string }) {
  const [models, setModels] = useState<string[]>([])
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
