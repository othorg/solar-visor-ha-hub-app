// The plant as the card sees it – port of SolarVisorKit/PlantTopology.swift (keep both in step; shared fixtures in
// contracts/fixtures/flow check it).
import type { PlantCapabilities, PlantModuleInfo, PlantPoint, PlantString } from "./model";

export type Kind = "grid" | "pv" | "battery" | "wallbox" | "generic" | "consumption" | "unknown";
export type SymbolName = "solar" | "house" | "battery" | "grid" | "wallbox" | "camper" | "plug";
const SYMBOLS: readonly SymbolName[] = ["solar", "house", "battery", "grid", "wallbox", "camper", "plug"];
const KINDS: readonly Kind[] = ["grid", "pv", "battery", "wallbox", "generic", "consumption"];

export function fallbackSymbol(kind: Kind): SymbolName {
  switch (kind) {
    case "pv": return "solar";
    case "grid": return "grid";
    case "battery": return "battery";
    case "wallbox": return "wallbox";
    default: return "plug";
  }
}

export interface Module {
  id: string;
  kind: Kind;
  name: string;
  balance: string | null;
  symbol: SymbolName;
  points: PlantPoint[];
  strings: PlantString[];
}

export const NODE = { generation: "generation", consumption: "consumption", home: "home" } as const;
export const SLOT_ORDER = ["grid", "pv_1", "pv_2", "pv_3", "battery", "wallbox", "generic_1", "generic_2", "generic_3",
                           "consumption"];

export function metric(m: Module, point: string): string {
  return `${m.id}.${point}`;
}

export function has(m: Module, point: string): boolean {
  return m.points.some((p) => p.id === point);
}

/** A generic module inside home (its power is part of home's) – never added to the consumption again. */
export function isPartOfHome(m: Module): boolean {
  return m.kind === "generic" && m.balance !== "beside_home";
}

export class Topology {
  readonly revision: number;
  readonly timezone: string | null;
  readonly pvPeakW: number | null;
  readonly modules: Module[];
  readonly metrics: Set<string>;

  constructor(caps: PlantCapabilities, private readonly lang: "de" | "en" = "de") {
    this.revision = caps.plant_revision;
    this.timezone = caps.timezone;
    this.pvPeakW = caps.pv_peak_w && caps.pv_peak_w > 0 ? caps.pv_peak_w : null;
    this.metrics = new Set(caps.metrics);
    const rank = (id: string) => {
      const i = SLOT_ORDER.indexOf(id);
      return i < 0 ? Number.MAX_SAFE_INTEGER : i;
    };
    const infos = [...caps.modules].sort((a, b) => rank(a.id) - rank(b.id) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    this.modules = infos.map((info: PlantModuleInfo) => {
      const kind = (KINDS as readonly string[]).includes(info.kind) ? (info.kind as Kind) : "unknown";
      const trimmed = info.name.trim();
      const symbol = info.icon && (SYMBOLS as readonly string[]).includes(info.icon) ? (info.icon as SymbolName)
        : fallbackSymbol(kind);
      return { id: info.id, kind, name: trimmed || fallbackName(info.id, kind, lang), balance: info.balance, symbol,
               points: info.points, strings: info.strings };
    });
  }

  module(id: string): Module | undefined {
    return this.modules.find((m) => m.id === id);
  }

  symbol(nodeId: string): SymbolName {
    return this.module(nodeId)?.symbol ?? (nodeId === NODE.home ? "house" : "plug");
  }

  get grid(): Module | undefined { return this.modules.find((m) => m.kind === "grid"); }
  get pvs(): Module[] { return this.modules.filter((m) => m.kind === "pv"); }
  get battery(): Module | undefined { return this.modules.find((m) => m.kind === "battery"); }
  get wallbox(): Module | undefined { return this.modules.find((m) => m.kind === "wallbox"); }
  get generics(): Module[] { return this.modules.filter((m) => m.kind === "generic"); }
  /** Consumers next to home on the consumption junction: the wallbox first, then generic modules „beside home“. */
  get besideHome(): Module[] {
    return [...(this.wallbox ? [this.wallbox] : []), ...this.generics.filter((m) => !isPartOfHome(m))];
  }
  get partOfHome(): Module[] { return this.generics.filter(isPartOfHome); }
  get measuresConsumption(): boolean {
    const c = this.module("consumption");
    return c !== undefined && has(c, "power");
  }

  name(id: string): string {
    const de = this.lang === "de";
    switch (id) {
      case NODE.generation: return de ? "Erzeugung" : "Generation";
      case NODE.consumption: return de ? "Verbrauch gesamt" : "Total consumption";
      case NODE.home: return de ? "Zuhause" : "Home";
      default: return this.module(id)?.name ?? fallbackName(id, "unknown", this.lang);
    }
  }

  /** „Wallbox · Leistung“, „Dach · String Ost · Leistung“ – a metric id in words. */
  metricName(metricId: string): string {
    const parts = metricId.split(".");
    const first = parts[0] ?? metricId;
    if (parts.length === 2 && parts[1] === "power" && [NODE.generation, NODE.consumption, NODE.home].includes(first as never)) {
      return this.name(first);
    }
    const words = [this.name(first)];
    const m = this.module(first);
    if (parts.length === 3 && m) {
      const i = m.strings.findIndex((s) => s.id === parts[1]);
      const s = m.strings[i];
      if (s) words.push(stringTitle(s, i + 1));
    }
    const point = parts[parts.length - 1];
    if (point && parts.length > 1) words.push(pointLabel(point, this.lang));
    return words.join(" · ");
  }
}

/** Colour index of a PV generator by its slot (pv_1 → 0). */
export function pvIndex(id: string): number | null {
  const m = /^pv_([1-3])$/.exec(id);
  return m ? Number(m[1]) - 1 : null;
}

export function fallbackName(id: string, kind: Kind, lang: "de" | "en" = "de"): string {
  const n = Number(id.split("_").pop());
  const slot = Number.isInteger(n) && n > 0 ? n : null;
  const de = lang === "de";
  switch (kind) {
    case "grid": return de ? "Netz" : "Grid";
    case "pv": return slot ? `PV ${slot}` : "PV";
    case "battery": return de ? "Batterie" : "Battery";
    case "wallbox": return "Wallbox";
    case "generic": return slot ? (de ? `Zusatz ${slot}` : `Extra ${slot}`) : (de ? "Zusatz" : "Extra");
    case "consumption": return de ? "Verbrauch gesamt" : "Total consumption";
    default: return id;
  }
}

export function stringTitle(s: PlantString, index: number): string {
  const name = s.name.trim();
  return name ? `String ${name} · String ${index}` : `String ${index}`;
}

const POINT_LABELS_DE: Record<string, string> = {
  power: "Leistung", ac_power: "AC-Leistung", dc_power: "DC-Leistung", energy_total: "Zählerstand", status: "Status",
  import_power: "Bezug", export_power: "Einspeisung", import_energy: "Bezug gesamt", export_energy: "Einspeisung gesamt",
  soc: "Ladezustand", capacity: "Installierte Kapazität", max_charge_power: "Max. Ladeleistung",
  max_discharge_power: "Max. Entladeleistung", charge_energy: "Geladen", discharge_energy: "Entladen",
  plugged: "Stecker", charging: "Laden", mode: "Lademodus", storage_power: "Speicher",
  storage_soc: "Speicher-Ladezustand", voltage: "Spannung", current: "Strom",
};
const POINT_LABELS_EN: Record<string, string> = {
  power: "Power", ac_power: "AC power", dc_power: "DC power", energy_total: "Meter reading", status: "Status",
  import_power: "Import", export_power: "Export", import_energy: "Import total", export_energy: "Export total",
  soc: "State of charge", capacity: "Installed capacity", max_charge_power: "Max. charge power",
  max_discharge_power: "Max. discharge power", charge_energy: "Charged", discharge_energy: "Discharged",
  plugged: "Plug", charging: "Charging", mode: "Charge mode", storage_power: "Storage",
  storage_soc: "Storage charge", voltage: "Voltage", current: "Current",
};

export function pointLabel(point: string, lang: "de" | "en" = "de"): string {
  const phase = /^phase_(\d+)/.exec(point);
  if (phase) return `Phase ${phase[1]}`;
  const label = (lang === "en" ? POINT_LABELS_EN : POINT_LABELS_DE)[point];
  return label ?? point.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
