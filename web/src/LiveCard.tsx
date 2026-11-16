// Overview: connection to Home Assistant, the main live values with their state, the balance residual.
import { useEffect, useState } from "react";
import { get } from "./api";
import { ErrorNote } from "./common";
import { DICTIONARIES, useI18n, type TextKey } from "./i18n";
import { formatValue } from "./plant";

interface Overview {
  mode: string;
  ha_connected: boolean;
  timezone: string | null;
  live: { metric_id: string; name: string | null; value: number | boolean | string | null; unit: string; state: string;
          reasons: string[] }[];
  balance_residual_w: number | null;
  not_ok: string[];
}

function has(key: string): key is TextKey {
  return key in DICTIONARIES.de;
}

export function LiveCard() {
  const { t } = useI18n();
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
  const label = (m: string) => {
    if (has(`live.metric.${m}`)) return t(`live.metric.${m}` as TextKey);
    const [mid] = m.split(".");
    return has(`module.${mid}`) ? t(`module.${mid}` as TextKey) : m;
  };
  return (
    <section className="card" aria-labelledby="live-title">
      <h2 id="live-title">{t("live.title")}</h2>
      <ErrorNote error={error} />
      {o && (
        <>
          <dl className="facts">
            <dt>{t("live.ha")}</dt>
            <dd className={o.ha_connected ? "ok" : "error"}>{t(o.ha_connected ? "live.ha.connected" : "live.ha.disconnected")}</dd>
            <dt>{t("live.timezone")}</dt>
            <dd>{o.timezone ?? "–"}</dd>
            {o.live.map((m) => (
              <div key={m.metric_id} className="fact-row">
                <dt>{m.name || label(m.metric_id)}</dt>
                <dd>
                  {formatValue(m.value as number | null, m.unit)}{" "}
                  {m.state !== "ok" && <span className="chip warning">{has(`state.${m.state}`) ? t(`state.${m.state}` as TextKey) : m.state}</span>}
                </dd>
              </div>
            ))}
            {o.live.length > 0 && (
              <>
                <dt>{t("live.residual")}</dt>
                <dd>{o.balance_residual_w === null ? t("live.residual_none") : formatValue(o.balance_residual_w, "W")}</dd>
              </>
            )}
          </dl>
          {o.not_ok.length > 0 && <p className="note">{t("live.not_ok", { metrics: o.not_ok.join(", ") })}</p>}
        </>
      )}
    </section>
  );
}
