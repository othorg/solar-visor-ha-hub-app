// History: where the values come from, from when on in full resolution, what the archive holds; days can be computed
// anew (with the plant of their time).
import { useState, type FormEvent } from "react";
import { send } from "./api";
import { ErrorNote, useLoad } from "./common";
import { DICTIONARIES, useI18n, type TextKey } from "./i18n";

interface HistoryView {
  source: "recorder" | "existing_calculation" | null;
  window_start: string | null;
  archive: Record<string, { days: number; first: string; last: string }> | null;
}

function has(key: string): key is TextKey {
  return key in DICTIONARIES.de;
}

export function HistorySection() {
  const { t } = useI18n();
  const view = useLoad<HistoryView>("history");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [done, setDone] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const h = view.data;

  async function recompute(e: FormEvent) {
    e.preventDefault();
    try {
      const r = await send<{ days: number }>("POST", "history/recompute", { from, to });
      setDone(r.days);
      setError(null);
      view.reload();
    } catch (err) {
      setError(err);
    }
  }

  return (
    <section className="card" aria-labelledby="history-title">
      <h2 id="history-title">{t("history.title")}</h2>
      <ErrorNote error={view.error ?? error} />
      {h && <p>{t(`history.source.${h.source ?? "none"}` as TextKey)}</p>}
      {h?.window_start && <p>{t("history.window", { date: new Date(h.window_start).toLocaleString() })}</p>}
      {h?.archive && (
        <>
          <h3>{t("history.archive")}</h3>
          <ul>
            {Object.entries(h.archive).map(([key, a]) => (
              <li key={key}>{has(`history.archive.${key}`) ? t(`history.archive.${key}` as TextKey, a) : `${key}: ${a.days}`}</li>
            ))}
          </ul>
        </>
      )}
      {h?.source === "recorder" && (
        <form className="form" onSubmit={recompute}>
          <h3>{t("history.recompute")}</h3>
          <p className="hint">{t("history.recompute_hint")}</p>
          <div className="actions">
            <label className="inline">{t("history.from")} <input type="date" value={from} required onChange={(e) => setFrom(e.target.value)} /></label>
            <label className="inline">{t("history.to")} <input type="date" value={to} required onChange={(e) => setTo(e.target.value)} /></label>
            <button type="submit">{t("history.recompute")}</button>
          </div>
          {done !== null && <p role="status">{t("history.recomputed", { days: done })}</p>}
        </form>
      )}
    </section>
  );
}
