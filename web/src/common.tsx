// Small shared pieces of the interface: loading a resource, showing an error.
import { useCallback, useEffect, useState } from "react";
import { ApiError, get } from "./api";
import { useI18n } from "./i18n";

export function useLoad<T>(path: string): { data: T | null; error: unknown; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    get<T>(path).then(
      (d) => alive && (setData(d), setError(null)),
      (e: unknown) => alive && setError(e),
    );
    return () => {
      alive = false;
    };
  }, [path, tick]);
  return { data, error, reload: useCallback(() => setTick((n) => n + 1), []) };
}

/** Takes freshly loaded data into a form's fields in the same render – not in an effect after painting, where it would
 *  overwrite what was typed in between (flaky "saves the addresses" test, 2026-11-16). Once per loaded object. */
export function useAdopt<T>(data: T | null, adopt: (d: T) => void): void {
  const [seen, setSeen] = useState<T | null>(null);
  if (data !== null && data !== seen) {
    setSeen(data);
    adopt(data);
  }
}

/** Narrow screen (phone, HA companion app): wide tables become lists. Without `matchMedia` (tests) the wide layout. */
export const NARROW = "(max-width: 720px)";

export function useNarrow(query: string = NARROW): boolean {
  const [narrow, setNarrow] = useState(() => typeof matchMedia === "function" && matchMedia(query).matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const m = matchMedia(query);
    const update = () => setNarrow(m.matches);
    update();
    m.addEventListener("change", update);
    return () => m.removeEventListener("change", update);
  }, [query]);
  return narrow;
}

export function ErrorNote({ error }: { error: unknown }) {
  const { t } = useI18n();
  if (!error) return null;
  const text =
    error instanceof ApiError && error.status === 403
      ? t("error.forbidden")
      : error instanceof ApiError
        ? `${t("common.error")} ${error.status}: ${error.message}`
        : t("error.unavailable");
  return (
    <p className="error" role="alert">
      {text}
    </p>
  );
}

