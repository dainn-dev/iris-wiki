import { createContext, useContext, useState, type ReactNode } from "react"
import { Languages } from "lucide-react"
import { useTranslation } from "react-i18next"
import i18n, { type Language } from "@/lib/i18n"

const KEY = "openkb_lang"

const LanguageCtx = createContext<{
  language: Language
  setLanguage: (l: Language) => void
} | null>(null)

/** Initial language: a stored choice wins; else browser/OS (anything starting
 * "vi" → vi, else en — `en` is the only non-Vietnamese bucket for the 2-locale
 * scope); else the vi default. Detection only picks the INITIAL value. */
function detectInitial(): Language {
  const stored = localStorage.getItem(KEY)
  if (stored === "vi" || stored === "en") return stored
  const nav = (navigator.languages?.[0] ?? navigator.language ?? "").toLowerCase()
  if (nav.startsWith("vi")) return "vi"
  return nav ? "en" : "vi"
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(() => {
    const initial = detectInitial()
    if (i18n.language !== initial) void i18n.changeLanguage(initial)
    return initial
  })

  const setLanguage = (l: Language) => {
    localStorage.setItem(KEY, l)
    setLanguageState(l)
    void i18n.changeLanguage(l) // re-renders every mounted useTranslation() consumer
  }

  return <LanguageCtx.Provider value={{ language, setLanguage }}>{children}</LanguageCtx.Provider>
}

export function useLanguage() {
  const ctx = useContext(LanguageCtx)
  if (!ctx) throw new Error("useLanguage must be used within LanguageProvider")
  return ctx
}

/** Language toggle icon button (toggles between vi and en). Same `h-8 w-8`
 * shape as ThemeToggle; slots into App.tsx's top-right chrome pill next to it. */
export function LanguageToggle({ className }: { className?: string }) {
  const { language, setLanguage } = useLanguage()
  const { t } = useTranslation("common")
  const next: Language = language === "vi" ? "en" : "vi"
  const label = language === "vi" ? t("language.vi") : t("language.en")
  return (
    <button
      onClick={() => setLanguage(next)}
      title={t("language.toggleTitle", { label })}
      aria-label={t("language.toggleAria", { label })}
      className={`grid h-8 w-8 place-items-center rounded-lg ${className ?? ""}`}
    >
      <Languages className="w-4 h-4" />
    </button>
  )
}
