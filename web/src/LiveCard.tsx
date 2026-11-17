// Overview „Jetzt“: the energy flow of the apps and the dashboard card (user decision 2026-11-17: the diagram instead of
// the table of live values), below it one line with the connection to Home Assistant, the time zone and the balance
// residual, and the metrics without a valid value.
import { useEffect, useState } from "react";
import { get } from "./api";
import { ErrorNote } from "./common";
import { FlowDashboard } from "./FlowDashboard";
import { useI18n } from "./i18n";
import { formatValue } from "./plant";

interface Overview {
  mode: string;
  ha_connected: boolean;
  timezone: string | null;
  balance_residual_w: number | null;
  not_ok: string[];
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
          {o.not_ok.length > 0 && <p className="note">{t("live.not_ok", { metrics: o.not_ok.join(", ") })}</p>}
        </>
      )}
    </section>
  );
}
