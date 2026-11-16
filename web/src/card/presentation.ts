// Node texts, glow and number formats – port of SolarVisorKit PlantPresentation.swift (PlantText.node, PlantNodeGlow),
// DashboardText.swift and UnitFormat.swift. German as the apps; English for Home Assistant in English. Keep the German
// texts in step with Swift – the shared fixtures check them.
import { SPEED } from "./animation";
import type { MetricState, MetricValue, PlantSnapshot } from "./model";
import { num } from "./model";
import { NODE, metric, type Topology } from "./topology";

export type Lang = "de" | "en";

function fixed(value: number, digits: number, lang: Lang): string {
  const text = value.toFixed(digits);
  return lang === "de" ? text.replace(".", ",") : text;
}

/** `737 W`, `1,08 kW`, `12,3 kW` – magnitude only; unit and precision chosen after rounding. */
export function power(watts: number | null, lang: Lang = "de"): string {
  if (watts === null) return "—";
  const a = Math.abs(watts);
  if (Math.round(a) < 1000) return `${fixed(a, 0, lang)} W`;
  const kw = a / 1000;
  return `${fixed(kw, Math.round(kw * 100) / 100 < 10 ? 2 : 1, lang)} kW`;
}

export function percent(value: number | null, lang: Lang = "de", digits = 0): string {
  return value === null ? "—" : `${fixed(value, digits, lang)} %`;
}

export interface NodeText {
  name: string;
  showsTitle: boolean;
  value: string;
  detail: string | null;
  calculated: boolean;
  dim: boolean;
  spoken: string;
}

const WORDS = {
  de: { import: "Bezug", export: "Einspeisung", charging: "lädt", discharging: "entlädt", soc: "SOC",
        disconnected: "getrennt", storage: "Akku", stale: "veraltet", implausible: "unplausibel",
        noStatus: "Status nicht verfügbar", plug: "Stecker", notConnected: "nicht verbunden", calculated: "berechnet",
        unknown: "unbekannt", asOf: "Stand" },
  en: { import: "import", export: "export", charging: "charging", discharging: "discharging", soc: "SOC",
        disconnected: "disconnected", storage: "Battery", stale: "stale", implausible: "implausible",
        noStatus: "status unavailable", plug: "plugged in", notConnected: "not connected", calculated: "calculated",
        unknown: "unknown", asOf: "as of" },
};

function direction(watts: number | null, positive: string, negative: string): string | null {
  if (watts === null || Math.abs(watts) < SPEED.offThresholdW) return null;
  return watts > 0 ? positive : negative;
}

/** True while the hub counts the wallbox as 0 W for home (its value is not trusted). */
export function wallboxAssumedZero(snapshot: PlantSnapshot | null): boolean {
  return snapshot?.metrics["home.power"]?.reasons.includes("wallbox_assumed_zero") === true;
}

/** The value as shown: an untrusted wallbox counts as 0 W where the hub assumed so (user decision 2026-10-05). */
export function displayed(metricId: string, snapshot: PlantSnapshot | null, topology: Topology):
    { value: MetricValue; state: MetricState } | null {
  const point = snapshot?.metrics[metricId];
  if (!point) return null;
  const wallbox = topology.wallbox;
  if (wallbox && wallboxAssumedZero(snapshot)
      && (point.state !== "ok" || snapshot?.metrics[metric(wallbox, "power")]?.state !== "ok")) {
    if (metricId === metric(wallbox, "power")) return { value: 0, state: "ok" };
    if (metricId === metric(wallbox, "charging")) return { value: false, state: "ok" };
  }
  return { value: point.value, state: point.state };
}

function wallboxStatus(id: string, snapshot: PlantSnapshot | null, lang: Lang): string {
  const w = WORDS[lang];
  const plug = snapshot?.metrics[`${id}.plugged`];
  if (!plug || plug.state !== "ok") return w.noStatus;
  const plugged = plug.value === true;
  const charging = snapshot?.metrics[`${id}.charging`]?.value === true && snapshot?.metrics[`${id}.power`]?.state === "ok";
  const modeValue = snapshot?.metrics[`${id}.mode`]?.value;
  const mode = typeof modeValue === "string" ? modeValue : null;
  return [plugged ? w.plug : w.notConnected, charging ? w.charging : null, mode].filter((x) => x !== null).join(" · ");
}

export function nodeText(id: string, topology: Topology, snapshot: PlantSnapshot | null, stale: boolean,
                         lang: Lang = "de"): NodeText {
  const w = WORDS[lang];
  const module = topology.module(id);
  const name = topology.name(id);
  const shown = displayed(`${id}.power`, snapshot, topology);
  const calculated = id === NODE.home || id === NODE.generation || (id === NODE.consumption && !topology.measuresConsumption);
  const watts = shown && (shown.state === "ok" || shown.state === "stale") ? num(shown.value) : null;
  const value = !shown || shown.state === "unknown" ? "—" : shown.state === "implausible" ? w.implausible : power(watts, lang);
  const dim = stale || shown?.state !== "ok";
  const usable = (metricId: string) => {
    const m = snapshot?.metrics[metricId];
    return m && (m.state === "ok" || m.state === "stale") ? m : null;
  };
  const details: string[] = [];
  switch (module?.kind) {
    case "grid": {
      const d = direction(watts, w.import, w.export);
      if (d) details.push(d);
      break;
    }
    case "battery": {
      const d = direction(watts, w.charging, w.discharging);
      if (d) details.push(d);
      const soc = num(usable(`${id}.soc`)?.value);
      if (soc !== null) details.push(`${percent(soc, lang)} ${w.soc}`);
      break;
    }
    case "wallbox":
      details.push(wallboxStatus(id, snapshot, lang));
      break;
    case "generic": {
      if (usable(`${id}.status`)?.value === "not_connected") details.push(w.disconnected);
      const soc = usable(`${id}.storage_soc`);
      if (soc) details.push(`${w.storage} ${percent(num(soc.value), lang)}${soc.state === "stale" ? ` · ${w.stale}` : ""}`);
      break;
    }
    default:
      break;
  }
  const spoken = [name, watts === null ? (value === w.implausible ? w.implausible : w.unknown) : value, ...details,
                  ...(calculated ? [w.calculated] : [])];
  const reading = snapshot?.metrics[`${id}.power`];
  if (reading && watts !== null && reading.state === shown?.state) {
    const at = new Date(reading.reported_at ?? reading.observed_at);
    if (!Number.isNaN(at.getTime())) {
      spoken.push(`${w.asOf} ${at.toLocaleTimeString(lang === "de" ? "de-DE" : "en-GB", { hour: "2-digit", minute: "2-digit" })}`);
    }
  }
  if (stale || shown?.state === "stale") spoken.push(w.stale);
  const junction = id === NODE.generation || id === NODE.consumption;
  return { name, showsTitle: !junction, value, detail: details.length ? details.join("\n") : null, calculated, dim,
           spoken: spoken.join(", ") };
}

const FALLBACK_REFERENCE_W = 21_000;
const BATTERY_REFERENCE_W = 12_000;

/** Node glow 0…1 by the power it handles: square-root scale up to the installed PV power (battery: 12 kW). */
export function glowLevel(id: string, topology: Topology, snapshot: PlantSnapshot | null, stale: boolean): number {
  if (stale) return 0;
  const shown = displayed(`${id}.power`, snapshot, topology);
  const watts = shown && shown.state === "ok" ? num(shown.value) : null;
  if (watts === null || Math.abs(watts) < SPEED.offThresholdW) return 0;
  const reference = topology.module(id)?.kind === "battery" ? BATTERY_REFERENCE_W : (topology.pvPeakW ?? FALLBACK_REFERENCE_W);
  return Math.min(1, Math.sqrt(Math.abs(watts) / reference));
}

/** SOC in percent if it may be shown (fresh or marked stale), else null. */
export function usableSoc(snapshot: PlantSnapshot | null, metricId: string): number | null {
  const point = snapshot?.metrics[metricId];
  return point && (point.state === "ok" || point.state === "stale") ? num(point.value) : null;
}

/** Reading order of the nodes (keyboard, screen readers): totals and sources first, then the consumers. */
export function readingOrder(t: Topology): string[] {
  return [NODE.generation, ...t.pvs.map((m) => m.id), ...(t.battery ? [t.battery.id] : []), "grid", NODE.consumption,
          NODE.home, ...t.partOfHome.map((m) => m.id), ...t.besideHome.map((m) => m.id)];
}
