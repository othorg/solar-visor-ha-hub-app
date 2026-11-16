// Every assigned data point at a glance: sensor, live value and check – of the draft if there is one, else of the
// active plant. Editing happens in "Plant" (the link jumps there). Wide screens: one table; narrow ones (phone, HA
// companion app, user request 2026-11-16): a list grouped by module, one compact block per point.
import { useEffect, useState } from "react";
import { get, send, type PlantView } from "./api";
import { ErrorNote, useNarrow } from "./common";
import { DICTIONARIES, useI18n, type TextKey } from "./i18n";
import { formatValue, type CheckResult, type RawPlant } from "./plant";

function has(key: string): key is TextKey {
  return key in DICTIONARIES.de;
}

export function PointsSection() {
  const { t } = useI18n();
  const [source, setSource] = useState<"draft" | "active" | null>(null);
  const [plant, setPlant] = useState<RawPlant | null>(null);
  const [checks, setChecks] = useState<Record<string, CheckResult> | null>(null);
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const narrow = useNarrow();

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const view = await get<PlantView>("plant");
        const p = (view.draft?.plant ?? view.active) as RawPlant | null;
        if (!alive) return;
        setSource(view.draft ? "draft" : view.active ? "active" : null);
        setPlant(p);
        if (p) {
          const r = await send<{ points: Record<string, CheckResult> }>("POST", "check", { plant: p });
          if (alive) setChecks(r.points);
        }
      } catch (e) {
        if (alive) setError(e);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const rows = Object.entries(checks ?? {}).filter(([, c]) => !onlyProblems || c.status !== "ok");
  const moduleName = (metric: string) => {
    const mid = metric.split(".")[0]!;
    return plant?.modules[mid]?.name || (has(`module.${mid}`) ? t(`module.${mid}` as TextKey) : mid);
  };
  const pointName = (metric: string) => {
    const parts = metric.split(".");
    const pid = parts[parts.length - 1]!;
    const label = has(`point.${pid}`) ? t(`point.${pid}` as TextKey) : pid;
    return parts.length === 3 ? `${parts[1]!.replace("string_", "String ")} – ${label}` : label;
  };

  return (
    <section className="card" aria-labelledby="points-title">
      <h2 id="points-title">{t("points.title")}</h2>
      <ErrorNote error={error} />
      {source && <p className="hint">{t("points.source", { source: t(`points.source.${source}` as TextKey) })}</p>}
      <label className="inline">
        <input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} />
        {t("points.only_problems")}
      </label>
      {checks === null && !error && plant !== null && <p>{t("common.loading")}</p>}
      {(plant === null || (checks !== null && rows.length === 0)) && !error && <p>{t("points.none")}</p>}
      {rows.length > 0 && (narrow ? (
        <div className="point-groups">
          {groups(rows).map(([mid, items]) => (
            <section key={mid} className="point-group" aria-labelledby={`points-${mid}`}>
              <div className="point-group-head">
                <h3 id={`points-${mid}`}>{moduleName(items[0]![0])}</h3>
                <a href="#plant">{t("points.edit")}</a>
              </div>
              <ul className="point-list">
                {items.map(([metric, c]) => (
                  <li key={metric} className="point-row">
                    <div className="point-row-line">
                      <strong>{pointName(metric)}</strong>
                      <Status c={c} />
                    </div>
                    <div className="point-row-line">
                      <span>{c.sensor?.name ?? "–"}</span>
                      <span className="point-row-value">{formatValue(c.value, c.unit, ["on", "off"])}</span>
                    </div>
                    {c.sensor && <div className="mono">{c.sensor.entity_id}</div>}
                    <Codes c={c} />
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="points-table">
            <thead>
              <tr>
                <th>{t("points.module")}</th>
                <th>{t("points.point")}</th>
                <th>{t("points.sensor")}</th>
                <th>{t("points.value")}</th>
                <th>{t("points.status")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([metric, c]) => (
                <tr key={metric}>
                  <td>{moduleName(metric)}</td>
                  <td>{pointName(metric)}</td>
                  <td>
                    {c.sensor?.name ?? "–"}
                    <div className="mono">{c.sensor?.entity_id ?? ""}</div>
                  </td>
                  <td>{formatValue(c.value, c.unit, ["on", "off"])}</td>
                  <td>
                    <Status c={c} />
                    <Codes c={c} />
                    <a href={`#plant`}>{t("points.edit")}</a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}

/** Rows grouped by module, in the order the modules first appear. */
function groups(rows: [string, CheckResult][]): [string, [string, CheckResult][]][] {
  const out = new Map<string, [string, CheckResult][]>();
  for (const row of rows) {
    const mid = row[0].split(".")[0]!;
    out.set(mid, [...(out.get(mid) ?? []), row]);
  }
  return [...out];
}

function Status({ c }: { c: CheckResult }) {
  const { t } = useI18n();
  return <span className={`chip ${c.status}`}>{t(`status.${c.status}` as TextKey)}</span>;
}

function Codes({ c }: { c: CheckResult }) {
  const { t } = useI18n();
  if (c.codes.length === 0) return null;
  return (
    <ul className="codes">
      {c.codes.map((code) => <li key={code}>{has(`check.${code}`) ? t(`check.${code}` as TextKey) : code}</li>)}
    </ul>
  );
}
