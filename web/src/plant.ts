// The plant as the interface edits it: the raw JSON of plant.json (the hub validates it), the catalogue from the hub
// (GET api/catalog) and small pure helpers. No rules of its own – what is required, which roles exist and which unit
// a point has all come from the catalogue.

export interface PointSpec {
  unit: string;
  roles: string[];
  counter: boolean;
  nonnegative: boolean;
}

export interface ModuleSpec {
  id: string;
  kind: string;
  always_on: boolean;
  required: Record<string, string[]>; // by grid variant; "" for every other module
  points: Record<string, PointSpec>;
  strings: boolean;
}

export interface Catalog {
  modules: ModuleSpec[];
  string_points: Record<string, PointSpec>;
  max_strings: number;
  max_name: number;
  grid_variants: string[];
  generic_balances: string[];
  wallbox_trust: string[];
  icons?: string[];                            // absent from a hub before the symbols
  default_icons?: Record<string, string>;      // module kind → symbol when none is chosen
}

export interface RawPoint {
  entity: string;
  unit: string;
  role: string;
  sign?: number;
  valid_from?: string | null;
  nonnegative?: boolean;
  stale_after_s?: number;
  freshness_from?: string;
}

export interface RawString {
  id: string;
  name?: string;
  points?: Record<string, RawPoint>;
}

export interface RawModule {
  enabled?: boolean;
  name?: string;
  variant?: string;
  balance?: string;
  trust?: string;
  icon?: string;
  points?: Record<string, RawPoint>;
  strings?: RawString[];
}

export interface RawPlant {
  schema: number;
  revision?: number;
  profile?: string | null;
  /** Installed power of all PV generators in W (optional scale for the apps). */
  pv_peak_w?: number;
  modules: Record<string, RawModule> & { e3dc_rscp?: { pv: string } };
}

export const RSCP_KEY = "e3dc_rscp";

export interface CheckResult {
  status: "ok" | "info" | "warning" | "error";
  codes: string[];
  value: number | boolean | string | null;
  unit: string;
  sensor: EntityView | null;
}

export interface EntityView {
  entity_id: string;
  name: string;
  state: string | null;
  unit: string | null;
  device_class: string | null;
  state_class: string | null;
  device: string | null;
  area: string | null;
  statistic: "mean" | "sum" | null;
  recorder_excluded: boolean | null;
}

export interface Suggestion {
  entity_id: string;
  source: "energy_dashboard" | "same_device";
}

/** The plant in a form the interface can show, or null (a draft imported from anywhere may be anything). */
export function asPlant(raw: unknown): RawPlant | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const modules = r.modules;
  if (!modules || typeof modules !== "object" || Array.isArray(modules)) return null;
  if (Object.values(modules).some((m) => !m || typeof m !== "object" || Array.isArray(m))) return null;
  return { ...(r as unknown as RawPlant), schema: typeof r.schema === "number" ? r.schema : 1 };
}

/** A first plant: grid (one sensor with a sign) and the first PV generator – the two modules every plant has. */
export function emptyPlant(names: { grid: string; pv: string }): RawPlant {
  return {
    schema: 1,
    modules: {
      grid: { enabled: true, name: names.grid, variant: "signed", points: {} },
      pv_1: { enabled: true, name: names.pv, points: {} },
    },
  };
}

export function moduleOf(plant: RawPlant, id: string): RawModule {
  return plant.modules[id] ?? {};
}

export function isEnabled(spec: ModuleSpec, module: RawModule): boolean {
  return spec.always_on || module.enabled === true;
}

/** Points of the grid variant not chosen (they would be invalid next to the chosen one). */
export function otherVariantPoints(spec: ModuleSpec, module: RawModule): string[] {
  if (spec.kind !== "grid") return [];
  const chosen = module.variant ?? "signed";
  return Object.entries(spec.required).filter(([v]) => v !== chosen).flatMap(([, points]) => points);
}

export function requiredPoints(spec: ModuleSpec, module: RawModule): string[] {
  if (spec.kind === "grid") return spec.required[module.variant ?? "signed"] ?? [];
  return spec.required[""] ?? [];
}

function withModule(plant: RawPlant, id: string, change: (m: RawModule) => RawModule): RawPlant {
  return { ...plant, modules: { ...plant.modules, [id]: change({ ...moduleOf(plant, id) }) } };
}

export function setModuleField(plant: RawPlant, id: string, field: "enabled" | "name" | "variant" | "balance" | "trust" | "icon",
                               value: boolean | string): RawPlant {
  return withModule(plant, id, (m) => {
    const next: RawModule = { ...m, [field]: value };
    if (field === "icon" && value === "") delete next.icon;           // "default": no choice stored
    if (field === "variant") {
      // points of the other variant would be invalid: they go, the chosen variant's stay
      const drop = value === "signed" ? ["import_power", "export_power"] : ["power"];
      next.points = Object.fromEntries(Object.entries(m.points ?? {}).filter(([pid]) => !drop.includes(pid)));
    }
    return next;
  });
}

/** Assigns a sensor (keeping sign and role of an earlier assignment) or removes the point (``null``). */
export function setPointEntity(plant: RawPlant, id: string, pointId: string, spec: PointSpec,
                               entity: string | null): RawPlant {
  return withModule(plant, id, (m) => {
    const points = { ...(m.points ?? {}) };
    if (entity === null) {
      delete points[pointId];
    } else {
      points[pointId] = assign(points[pointId], spec, entity);
    }
    return { ...m, points };
  });
}

export function setPointField(plant: RawPlant, id: string, pointId: string, field: "sign" | "role",
                              value: number | string): RawPlant {
  return withModule(plant, id, (m) => {
    const old = m.points?.[pointId];
    if (!old) return m;
    return { ...m, points: { ...m.points, [pointId]: { ...old, [field]: value } } };
  });
}

export function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** "modules.pv_2.points.power.role" → the metric id "pv_2.power" (problems are shown at their point). */
export function metricOfPath(path: string): string | null {
  const m = /^modules\.([a-z0-9_]+)\.points\.([a-z0-9_]+)/.exec(path);
  return m ? `${m[1]}.${m[2]}` : null;
}

export function formatValue(value: CheckResult["value"], unit: string,
                            onOff: [string, string] = ["on", "off"]): string {
  if (value === null) return "–";
  if (typeof value === "boolean") return value ? onOff[0] : onOff[1];
  if (typeof value === "string") return value;
  if (unit === "W" && Math.abs(value) >= 1000) return `${(value / 1000).toFixed(2)} kW`;
  if (unit === "Wh" && Math.abs(value) >= 1000) return `${(value / 1000).toFixed(1)} kWh`;
  return `${Number.isInteger(value) ? value : value.toFixed(1)} ${unit}`;
}

function assign(old: RawPoint | undefined, spec: PointSpec, entity: string): RawPoint {
  return {
    entity,
    unit: spec.unit,
    role: old && spec.roles.includes(old.role) ? old.role : spec.roles[0]!,
    sign: old?.sign ?? 1,
    valid_from: old?.valid_from ?? null,
  };
}

type Extra = "stale_after_s" | "valid_from" | "nonnegative" | "freshness_from";

/** Optional fields of a point; ``undefined`` (or an empty value) removes the field – the default applies again. */
export function setPointExtra(plant: RawPlant, id: string, pointId: string, field: Extra,
                              value: string | number | boolean | undefined): RawPlant {
  return withModule(plant, id, (m) => {
    const old = m.points?.[pointId];
    if (!old) return m;
    const next: RawPoint = { ...old };
    if (value === undefined || value === "" || value === false) {
      if (field === "valid_from") next.valid_from = null;
      else delete next[field];
    } else {
      (next as unknown as Record<string, unknown>)[field] = value;
    }
    return { ...m, points: { ...m.points, [pointId]: next } };
  });
}

// --- strings of a PV generator ---------------------------------------------------------------------------------

export function addString(plant: RawPlant, id: string, max: number): RawPlant {
  return withModule(plant, id, (m) => {
    const used = new Set((m.strings ?? []).map((s) => s.id));
    const n = Array.from({ length: max }, (_, i) => i + 1).find((i) => !used.has(`string_${i}`));
    return n === undefined ? m : { ...m, strings: [...(m.strings ?? []), { id: `string_${n}`, name: "", points: {} }] };
  });
}

export function removeString(plant: RawPlant, id: string, sid: string): RawPlant {
  return withModule(plant, id, (m) => {
    const rest = (m.strings ?? []).filter((s) => s.id !== sid);
    const { strings: _dropped, ...without } = m;
    return rest.length ? { ...m, strings: rest } : without;
  });
}

function withString(plant: RawPlant, id: string, sid: string, change: (s: RawString) => RawString): RawPlant {
  return withModule(plant, id, (m) => ({ ...m, strings: (m.strings ?? []).map((s) => (s.id === sid ? change({ ...s }) : s)) }));
}

export function setStringName(plant: RawPlant, id: string, sid: string, name: string): RawPlant {
  return withString(plant, id, sid, (s) => ({ ...s, name }));
}

export function setStringPointEntity(plant: RawPlant, id: string, sid: string, pointId: string, spec: PointSpec,
                                     entity: string | null): RawPlant {
  return withString(plant, id, sid, (s) => {
    const points = { ...(s.points ?? {}) };
    if (entity === null) delete points[pointId];
    else points[pointId] = assign(points[pointId], spec, entity);
    return { ...s, points };
  });
}

export function setStringPointField(plant: RawPlant, id: string, sid: string, pointId: string, field: "sign" | "role",
                                    value: number | string): RawPlant {
  return withString(plant, id, sid, (s) => {
    const old = s.points?.[pointId];
    return old ? { ...s, points: { ...s.points, [pointId]: { ...old, [field]: value } } } : s;
  });
}

// --- E3/DC details (RSCP) ----------------------------------------------------------------------------------------

export function setRscp(plant: RawPlant, pv: string | null): RawPlant {
  const modules = { ...plant.modules };
  if (pv === null) delete modules.e3dc_rscp;
  else modules.e3dc_rscp = { pv };
  return { ...plant, modules };
}

/** kWp entry → W; null when it is not a plain number ("9,8" and "9.8" count, "1.234,5" or "23 kWp" do not). */
export function parsePvPeak(kwp: string): number | null {
  const text = kwp.trim().replace(",", ".");
  if (!/^\d+(\.\d+)?$/.test(text)) return null;
  return Math.round(Number(text) * 1000);
}

/** Installed PV power from a kWp entry: empty removes it; an entry that is not a number keeps the stored value (the
 *  card says so) – a typo never erases it. The hub checks the range. */
export function setPvPeak(plant: RawPlant, kwp: string): RawPlant {
  const { pv_peak_w: _old, ...rest } = plant;
  if (kwp.trim() === "") return rest;
  const value = parsePvPeak(kwp);
  return value === null ? plant : { ...rest, pv_peak_w: value };
}

/** "2026-11-14T08:00" in the browser's zone → ISO with offset (the hub wants a point in time with a zone). */
export function localToIso(local: string): string | null {
  if (!local) return null;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export function isoToLocal(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Fills every empty point that has a suggestion with its first candidate; a module that is off is switched on only
 *  through its main point (``power``) – a suggested counter alone does not make a battery. Never overwrites a choice. */
export function applySuggestions(plant: RawPlant, catalog: Catalog, suggestions: Record<string, Suggestion[]>,
                                 names: Record<string, string>): RawPlant {
  let p = plant;
  for (const spec of catalog.modules) {
    const order = Object.keys(spec.points).sort((a, b) => Number(b === "power") - Number(a === "power"));
    for (const pid of order) {
      const first = suggestions[`${spec.id}.${pid}`]?.[0];
      const module = moduleOf(p, spec.id);
      if (!first || module.points?.[pid]) continue;
      if (!isEnabled(spec, module)) {
        if (pid !== "power") continue;
        p = setModuleField(p, spec.id, "enabled", true);
        if (!moduleOf(p, spec.id).name) p = setModuleField(p, spec.id, "name", names[spec.id] ?? spec.id);
      }
      p = setPointEntity(p, spec.id, pid, spec.points[pid]!, first.entity_id);
    }
  }
  return p;
}
