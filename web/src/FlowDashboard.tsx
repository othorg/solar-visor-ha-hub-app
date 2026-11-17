// Overview „Jetzt“: the energy flow of the apps and the dashboard card (card/panel.ts over the hub's card routes
// card/v1/*); a click on a node shows its current values below the diagram (user decision 2026-11-17). Reads only.
import { useEffect, useMemo, useRef, useState } from "react";
import { CardApi } from "./card/api";
import type { MetricValue, PlantSnapshot } from "./card/model";
import { FlowPanel, type CardState } from "./card/panel";
import { readingOrder } from "./card/presentation";
import { textsFor, type Texts } from "./card/texts";
import { NODE, type Topology } from "./card/topology";
import { DICTIONARIES, useI18n, type TextKey } from "./i18n";
import { formatValue } from "./plant";
import { useTheme } from "./theme";

/** Metrics shown for a node: its own (power first, then its other points, then its strings); generation: the total
 *  and the power of every generator feeding home (one with full feed-in is no part of it). Only metrics the snapshot
 *  carries. */
export function nodeMetrics(node: string, snapshot: PlantSnapshot, topology: Topology): string[] {
  if (node === NODE.generation) {
    return [`${NODE.generation}.power`, ...topology.pvs.map((m) => `${m.id}.power`)].filter((id) => id in snapshot.metrics);
  }
  const own = Object.keys(snapshot.metrics).filter((id) => snapshot.metrics[id]?.module === node);
  const main = `${node}.power`;
  const depth = (id: string) => id.split(".").length;
  return [...own.filter((id) => id === main), ...own.filter((id) => id !== main && depth(id) === 2),
          ...own.filter((id) => depth(id) > 2)];
}

/** A metric in words below its node's title: without the node's name in front („String Ost · String 1 · Leistung“). */
export function metricLabel(id: string, node: string, topology: Topology): string {
  const full = topology.metricName(id), prefix = `${topology.name(node)} · `;
  return full.startsWith(prefix) ? full.slice(prefix.length) : full;
}

/** A value as the diagram above writes its numbers: decimal comma in German (`-1,20 kW`, `0,2 A`), sign kept. */
export function nodeValue(value: MetricValue, unit: string, lang: "de" | "en", onOff: [string, string]): string {
  const text = formatValue(value, unit, onOff);
  return lang === "de" && typeof value === "number" ? text.replace(".", ",") : text;
}

function has(key: string): key is TextKey {
  return key in DICTIONARIES.de;
}

export function FlowDashboard() {
  const { t, language } = useI18n();
  const theme = useTheme();
  const latestTheme = useRef(theme);
  latestTheme.current = theme;
  const host = useRef<HTMLDivElement>(null);
  const panel = useRef<FlowPanel | null>(null);
  const [shown, setShown] = useState<{ state: CardState; topology: Topology | null } | null>(null);
  const [node, setNode] = useState<string | null>(null);
  const texts = useMemo<Texts>(() => ({ ...textsFor(language), opensHistory: t("live.shows_values") }), [language, t]);
  const latestTexts = useRef(texts);
  latestTexts.current = texts;

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const root = el.shadowRoot ?? el.attachShadow({ mode: "open" });   // StrictMode mounts twice: one shadow root
    const p = new FlowPanel(root, {
      api: new CardApi({ apiBase: "", renewSession: async () => undefined }),   // the Ingress session is HA's page's
      texts: latestTexts.current,
      theme: latestTheme.current,
      clickable: () => true,
      onNode: (id) => setNode((current) => (current === id ? null : id)),
      onUpdate: (state, topology) => setShown({ state, topology }),
    });
    panel.current = p;
    return () => {
      p.destroy();
      panel.current = null;
    };
  }, []);

  useEffect(() => panel.current?.setTexts(texts), [texts]);
  useEffect(() => panel.current?.setTheme(theme), [theme]);

  const topology = shown?.topology ?? null;
  const snapshot = shown?.state.snapshot ?? null;
  const selected = node && topology && readingOrder(topology).includes(node) ? node : null;
  useEffect(() => {
    if (node && topology && !selected) setNode(null);  // gone with a new plant: not shown again when it returns
  }, [node, topology, selected]);
  const stale = shown?.state.stale ?? true;

  return (
    <div className="flow">
      <div ref={host} className="flow-host" />
      {selected && topology && snapshot ? (
        <div className="flow-node" aria-live="polite">
          <div className="flow-node-head">
            <h3>{topology.name(selected)}</h3>
            <button type="button" className="secondary" onClick={() => setNode(null)}>{t("common.close")}</button>
          </div>
          <dl className="facts">
            {nodeMetrics(selected, snapshot, topology).map((id) => {
              const m = snapshot.metrics[id]!;
              const state = stale ? "stale" : m.state;      // the diagram's freshness wins: no old value as "ok"
              return (
                <div key={id} className="fact-row">
                  <dt>{metricLabel(id, selected, topology)}</dt>
                  <dd>
                    {nodeValue(m.value, m.unit, language, [t("live.flag_on"), t("live.flag_off")])}{" "}
                    {state !== "ok" && (
                      <span className="chip warning">{has(`state.${state}`) ? t(`state.${state}` as TextKey) : state}</span>
                    )}
                  </dd>
                </div>
              );
            })}
          </dl>
        </div>
      ) : (
        topology && <p className="hint">{t("live.select_hint")}</p>
      )}
    </div>
  );
}
