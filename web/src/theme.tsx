// Light or dark like Home Assistant (user decision 2026-11-17): the interface runs in Home Assistant's Ingress frame
// (same origin) and reads its display setting there – the user's choice, „auto“ already resolved. Opened on its own
// (development) it follows the system setting. Home Assistant tells an embedded page nothing: the setting is read again
// every 2 s.
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import type { Theme } from "./card/palette";

export type { Theme };

interface HaElement extends Element {
  hass?: { themes?: { darkMode?: unknown } };
}

/** Home Assistant's dark mode from the surrounding page; null when there is none or it cannot be read. */
export function haDarkMode(win: Window = window): boolean | null {
  try {
    if (win.parent === win) return null;
    const dark = (win.parent.document.querySelector("home-assistant") as HaElement | null)?.hass?.themes?.darkMode;
    return typeof dark === "boolean" ? dark : null;
  } catch {
    return null;                                      // another origin
  }
}

function darkQuery(): MediaQueryList | null {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)");
  } catch {
    return null;
  }
}

export function currentTheme(win: Window = window): Theme {
  return (haDarkMode(win) ?? darkQuery()?.matches ?? false) ? "dark" : "light";
}

/** The page's colours (styles.css: `:root[data-theme="dark"]`). */
export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

const POLL_MS = 2_000;
const Context = createContext<Theme | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => currentTheme());
  useEffect(() => {
    const check = () => setTheme(currentTheme());
    const timer = setInterval(check, POLL_MS);
    const query = darkQuery();
    query?.addEventListener?.("change", check);
    return () => {
      clearInterval(timer);
      query?.removeEventListener?.("change", check);
    };
  }, []);
  useEffect(() => applyTheme(theme), [theme]);
  return <Context.Provider value={theme}>{children}</Context.Provider>;
}

/** The current theme; outside the provider (tests) as read once. */
export function useTheme(): Theme {
  return useContext(Context) ?? currentTheme();
}
