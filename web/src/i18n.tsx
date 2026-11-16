// German and English texts of the interface (user decision 8). Both dictionaries have the same keys – a test fails
// otherwise. The language follows the browser until the user picks one (kept in this browser only).
import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

import { de, en, type TextKey } from "./texts";

export type { TextKey };

export const DICTIONARIES: Record<Language, Record<TextKey, string>> = { de, en };
export type Language = "de" | "en";
const STORAGE_KEY = "solar-visor.language";

export function initialLanguage(stored: string | null, browser: string | undefined): Language {
  if (stored === "de" || stored === "en") return stored;
  return browser?.toLowerCase().startsWith("de") ? "de" : "en";
}

function readStored(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function format(text: string, params?: Record<string, string | number>): string {
  return params ? text.replace(/\{(\w+)\}/g, (m, k: string) => (k in params ? String(params[k]) : m)) : text;
}

interface I18n {
  language: Language;
  setLanguage: (l: Language) => void;
  t: (key: TextKey, params?: Record<string, string | number>) => string;
}

const Context = createContext<I18n | null>(null);

export function I18nProvider({ children, language: fixed }: { children: ReactNode; language?: Language }) {
  const [language, setState] = useState<Language>(fixed ?? initialLanguage(readStored(), navigator.language));
  const value = useMemo<I18n>(
    () => ({
      language,
      setLanguage: (l) => {
        setState(l);
        try {
          localStorage.setItem(STORAGE_KEY, l);
        } catch {
          // private mode: the choice lasts for this page only
        }
        document.documentElement.lang = l;
      },
      t: (key, params) => format(DICTIONARIES[language][key], params),
    }),
    [language],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useI18n(): I18n {
  const ctx = useContext(Context);
  if (!ctx) throw new Error("I18nProvider missing");
  return ctx;
}
