// Choosing a Home Assistant sensor for one data point: suggestions from the energy dashboard first, then a search on
// the hub (it filters by the point's unit and returns at most 50 hits). Keyboard: the search field has the focus,
// Escape closes, every hit is a button.
import { useEffect, useRef, useState } from "react";
import { get } from "./api";
import { useI18n, type TextKey } from "./i18n";
import type { EntityView, Suggestion } from "./plant";

interface Props {
  title: string;
  unit: string;
  suggestions: Suggestion[];
  onPick: (entityId: string) => void;
  onClose: () => void;
}

function statisticKey(s: EntityView["statistic"]): TextKey {
  return s === "mean" ? "picker.statistic.mean" : s === "sum" ? "picker.statistic.sum" : "picker.statistic.none";
}

export function SensorPicker({ title, unit, suggestions, onPick, onClose }: Props) {
  const { t } = useI18n();
  const [q, setQ] = useState("");
  const [result, setResult] = useState<{ entities: EntityView[]; total: number } | null>(null);
  const [error, setError] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => input.current?.focus(), []);
  useEffect(() => {
    let alive = true;
    const timer = setTimeout(() => {
      get<{ entities: EntityView[]; total: number }>(`entities?${new URLSearchParams({ q, unit })}`).then(
        (r) => alive && (setResult(r), setError(false)),
        () => alive && setError(true),
      );
    }, 250);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [q, unit]);

  const suggested = new Map(suggestions.map((s) => [s.entity_id, s.source]));
  const hits = result?.entities ?? [];
  const ordered = [...hits.filter((e) => suggested.has(e.entity_id)), ...hits.filter((e) => !suggested.has(e.entity_id))];
  const missing = suggestions.filter((s) => !hits.some((e) => e.entity_id === s.entity_id) && !q);

  return (
    <div className="backdrop" onClick={onClose}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title} onClick={(e) => e.stopPropagation()}
           onKeyDown={(e) => e.key === "Escape" && onClose()}>
        <header className="dialog-head">
          <h2>{title}</h2>
          <button type="button" className="plain" onClick={onClose} aria-label={t("common.close")}>✕</button>
        </header>
        <input ref={input} type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("picker.search")}
               aria-label={t("picker.search")} />
        {error && <p className="error" role="alert">{t("error.unavailable")}</p>}
        {missing.length > 0 && (
          <ul className="hits" aria-label={t("picker.suggested")}>
            {missing.map((s) => (
              <li key={s.entity_id}>
                <button type="button" className="hit" onClick={() => onPick(s.entity_id)}>
                  <strong>{s.entity_id}</strong>
                  <small>{t(`picker.source.${s.source}` as TextKey)}</small>
                </button>
              </li>
            ))}
          </ul>
        )}
        {result && (
          <>
            <p className="hint" aria-live="polite">
              {result.total ? t("picker.results", { shown: hits.length, total: result.total }) : t("picker.no_results")}
            </p>
            <ul className="hits">
              {ordered.map((e) => (
                <li key={e.entity_id}>
                  <button type="button" className="hit" onClick={() => onPick(e.entity_id)}>
                    <span className="hit-main">
                      <strong>{e.name}</strong>
                      <span className="mono">{e.entity_id}</span>
                    </span>
                    <span className="hit-side">
                      <span>{e.state ?? "–"} {e.unit ?? ""}</span>
                      <small>
                        {[e.device, e.area].filter(Boolean).join(" · ")}
                        {suggested.has(e.entity_id) && ` · ${t(`picker.source.${suggested.get(e.entity_id)!}` as TextKey)}`}
                      </small>
                      <small className={e.statistic ? "" : "warn"}>{t(statisticKey(e.statistic))}</small>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
