// Overview „Jetzt“: the energy flow of the apps and the dashboard card (user decision 2026-11-17: the diagram instead of
// the table of live values), below it one line with the connection to Home Assistant, the time zone and the balance
// residual, and the metrics without a valid value – in words, with the reason and since when („Netz · Zähler Bezug:
// veraltet – Zähler läuft nicht weiter … · seit 11:23“, user decision 2026-11-17).
import { useEffect, useState } from "react";
import { get } from "./api";
import { ErrorNote } from "./common";
import { FlowDashboard } from "./FlowDashboard";
import { DICTIONARIES, useI18n, type Language, type TextKey } from "./i18n";
import { formatValue } from "./plant";

export interface Problem {
  metric_id: string;
  name: string | null;
  state: string;
  reasons: string[];
  since: string | null;
}

interface Overview {
  mode: string;
  ha_connected: boolean;
  timezone: string | null;
  balance_residual_w: number | null;
  not_ok: string[];
  problems?: Problem[];
}

type Translate = (key: TextKey, params?: Record<string, string | number>) => string;

function has(key: string): key is TextKey {
  return key in DICTIONARIES.de;
}

/** A hub reason code in words; „unit_mismatch:kWh->W“ carries its units, an unknown code stays visible as code. */
export function reasonText(code: string, t: Translate): string {
  const colon = code.indexOf(":");
  const base = colon < 0 ? code : code.slice(0, colon);
  const key = `reason.${base}`;
  return has(key) ? t(key, { unit: colon < 0 ? "" : code.slice(colon + 1) }) : code;
}

/** „11:23“ in the plant's time zone, with the date when that is not today there. */
export function sinceText(iso: string, timezone: string | null, lang: Language, now: Date = new Date()): string {
  const at = new Date(iso);
  const timeZone = timezone ?? undefined;
  const day = (d: Date) =>
    new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
  const time = new Intl.DateTimeFormat(lang, { timeZone, hour: "2-digit", minute: "2-digit", hour12: false }).format(at);
  if (day(at) === day(now)) return time;
  return `${new Intl.DateTimeFormat(lang, { timeZone, day: "2-digit", month: "2-digit" }).format(at)} ${time}`;
}

/** One line per value: „Netz · Zähler Bezug: veraltet – Meldung zu alt · seit 11:23“. */
export function problemLine(p: Problem, timezone: string | null, lang: Language, t: Translate, now?: Date): string {
  const [module, ...rest] = p.metric_id.split(".");
  const point = rest.join(".");
  const moduleKey = `module.${module}`, pointKey = `point.${point}`, stateKey = `state.${p.state}`;
  const moduleName = p.name ?? (has(moduleKey) ? t(moduleKey) : module);
  const parts = p.reasons.map((r) => reasonText(r, t));
  if (p.since) parts.push(t("live.since", { time: sinceText(p.since, timezone, lang, now) }));
  return `${moduleName} · ${has(pointKey) ? t(pointKey) : point}: ${has(stateKey) ? t(stateKey) : p.state}` +
    (parts.length ? ` – ${parts.join(" · ")}` : "");
}

export function LiveCard() {
  const { t, language } = useI18n();
  const [o, setO] = useState<Overview | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let alive = true;
    const load = () => get<Overview>("overview").then((r) => alive && (setO(r), setError(null)), (e) => alive && setError(e));
    void load();
    const timer = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);
  return (
    <section className="card" aria-labelledby="live-title">
      <h2 id="live-title">{t("live.title")}</h2>
      <FlowDashboard />
      <ErrorNote error={error} />
      {o && (
        <>
          <p className="flow-status">
            <span>
              {t("live.ha")}:{" "}
              <span className={o.ha_connected ? "ok" : "error"}>{t(o.ha_connected ? "live.ha.connected" : "live.ha.disconnected")}</span>
            </span>
            <span>{t("live.timezone")}: {o.timezone ?? "–"}</span>
            <span>
              {t("live.residual")}: {o.balance_residual_w === null ? t("live.residual_none") : formatValue(o.balance_residual_w, "W")}
            </span>
          </p>
          {o.problems ? (
            o.problems.length > 0 && (
              <div className="note">
                <p>{t("live.problems")}:</p>
                <ul>
                  {o.problems.map((p) => <li key={p.metric_id}>{problemLine(p, o.timezone, language, t)}</li>)}
                </ul>
              </div>
            )
          ) : (
            o.not_ok.length > 0 && <p className="note">{t("live.not_ok", { metrics: o.not_ok.join(", ") })}</p>
          )}
        </>
      )}
    </section>
  );
}
