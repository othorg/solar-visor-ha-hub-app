// Flow allocation and ball colours – port of SolarVisorKit/PlantFlow.swift (PlantFlowInputs, PlantFlowAllocation,
// PlantBallColours). Keep in step with Swift – the shared fixtures in contracts/fixtures/flow/ check both.
import { MESH_WIRES } from "./layout";
import type { PlantSnapshot } from "./model";
import { num } from "./model";
import { NODE, metric, pvIndex, type Topology } from "./topology";

/** Where a ball's energy comes from (its colour). Order = colour order of the golden-ratio sequence. */
export const SOURCES = ["grid", "battery", "pv1", "pv2", "pv3", "unattributed"] as const;
export type Source = (typeof SOURCES)[number];
export type Shares = Partial<Record<Source, number>>;

export function pvSource(index: number): Source {
  return (["pv1", "pv2", "pv3"] as const)[Math.min(Math.max(index, 0), 2)] as Source;
}

/** Live powers in W (battery + = charging, grid + = import); null = not trusted now; absent = no such module. */
export interface FlowInputs {
  pv: Record<string, number | null>;
  hasBattery: boolean;
  battery: number | null;
  grid: number | null;
  consumption: number | null;
  home: number | null;
  beside: Record<string, number | null>;
  inside: Record<string, number | null>;
  assumedZero: string[];
}

export interface WireFlow {
  powerW: number;
  shares: Shares;
}

/** Inputs from the snapshot's edges (state ok only); an untrusted wallbox counts as 0 W only where the hub assumed so. */
export function inputsFromSnapshot(snapshot: PlantSnapshot | null, topology: Topology): FlowInputs {
  const edges = new Map<string, PlantSnapshot["edges"][number]>();
  for (const e of snapshot?.edges ?? []) if (!edges.has(e.edge_id)) edges.set(e.edge_id, e);
  const metrics = snapshot?.metrics ?? {};
  const ok = (edge: string, metricId: string): number | null => {
    const e = edges.get(edge);
    if (e) return e.state === "ok" ? e.power_w : null;
    const m = metrics[metricId];
    return m && m.state === "ok" ? num(m.value) : null;
  };
  const assumed = metrics["home.power"]?.reasons.includes("wallbox_assumed_zero") && topology.wallbox
    ? [topology.wallbox.id] : [];
  const beside: Record<string, number | null> = {};
  for (const m of topology.besideHome) beside[m.id] = assumed.includes(m.id) ? 0 : ok(m.id, metric(m, "power"));
  const pv: Record<string, number | null> = {};
  for (const m of topology.pvs) pv[m.id] = ok(m.id, metric(m, "power"));
  const inside: Record<string, number | null> = {};
  for (const m of topology.partOfHome) inside[m.id] = ok(m.id, metric(m, "power"));
  const battery = topology.battery;
  return {
    pv, hasBattery: battery !== undefined, battery: battery ? ok(battery.id, metric(battery, "power")) : null,
    grid: ok("grid", "grid.power"), consumption: ok(NODE.consumption, "consumption.power"),
    home: ok(NODE.home, "home.power"), beside, inside, assumedZero: assumed,
  };
}

function source(pvId: string): Source {
  return pvSource(pvIndex(pvId) ?? 0);
}

/** Consumption no source explains is shown as unattributed only above the measuring noise. */
export function isUnattributed(unexplainedW: number, consumptionW: number): boolean {
  return unexplainedW > Math.max(30, 0.05 * consumptionW);
}

function normalized(parts: Shares): Shares {
  const positive = Object.entries(parts).filter(([, v]) => (v ?? 0) > 0.5) as [Source, number][];
  const sum = positive.reduce((a, [, v]) => a + v, 0);
  const out: Shares = {};
  if (sum > 0) for (const [k, v] of positive) out[k] = v / sum;
  return out;
}

function scaled(shares: Shares, f: number): Shares {
  const out: Shares = {};
  for (const [k, v] of Object.entries(shares) as [Source, number][]) out[k] = v * f;
  return out;
}

/** Same convention as the hub (supply_slot): consumption is served by grid import first, then battery discharge, the
 *  rest directly by generation; remaining generation charges the battery, then is exported; remaining battery power is
 *  exported, remaining grid import charges the battery. An unknown value leaves its wires out (no balls). */
export function wires(x: FlowInputs): Record<string, WireFlow> {
  const out: Record<string, WireFlow> = {};
  for (const [id, value] of Object.entries(x.pv)) {
    if (value !== null) out[id] = { powerW: Math.max(0, value), shares: { [source(id)]: 1 } };
  }
  const besideValues = Object.values(x.beside);
  const besideKnown = besideValues.every((v) => v !== null);
  const besideSum = besideValues.reduce<number>((a, v) => a + (v === null ? 0 : Math.max(0, v)), 0);
  const rawTotal = x.consumption ?? (besideKnown && x.home !== null ? x.home + besideSum : null);
  const total = rawTotal === null ? null : Math.max(0, rawTotal);
  let consumerShares: Shares = { unattributed: 1 };
  const pvValues = Object.values(x.pv);
  const pvKnown = pvValues.length > 0 && pvValues.every((v) => v !== null);
  const battery = x.hasBattery ? x.battery : 0;
  if (pvKnown && battery !== null && x.grid !== null && total !== null) {
    const parts: Record<string, number> = {};
    for (const [id, v] of Object.entries(x.pv)) parts[id] = Math.max(0, v ?? 0);
    const gen = Object.values(parts).reduce((a, b) => a + b, 0);
    const consumption = total;
    const discharge = Math.max(0, -battery), charge = Math.max(0, battery);
    const imp = Math.max(0, x.grid), exp = Math.max(0, -x.grid);

    const gridToC = Math.min(imp, consumption);
    const batToC = Math.min(discharge, consumption - gridToC);
    const genToC = Math.min(consumption - gridToC - batToC, gen);
    const genRest = gen - genToC;
    const genToB = Math.min(charge, genRest);
    const genToN = Math.min(exp, genRest - genToB);
    const batToN = Math.min(discharge - batToC, exp - genToN);
    const gridToB = Math.min(imp - gridToC, charge - genToB);

    const genMix: Shares = {};
    if (gen > 0) for (const [id, w] of Object.entries(parts)) genMix[source(id)] = (genMix[source(id)] ?? 0) + w / gen;
    out.GC = { powerW: genToC, shares: genToC > 0 ? genMix : {} };
    out.GN = { powerW: genToN, shares: genToN > 0 ? genMix : {} };
    out.NC = { powerW: gridToC, shares: gridToC > 0 ? { grid: 1 } : {} };
    if (x.hasBattery) {
      out.GB = { powerW: genToB, shares: genToB > 0 ? genMix : {} };
      out.BC = { powerW: batToC, shares: batToC > 0 ? { battery: 1 } : {} };
      out.BN = batToN > 0 ? { powerW: batToN, shares: { battery: 1 } }
        : { powerW: -gridToB, shares: gridToB > 0 ? { grid: 1 } : {} };
    }
    const mix: Shares = { grid: gridToC, battery: batToC };
    if (gen > 0) for (const [id, w] of Object.entries(parts)) mix[source(id)] = (mix[source(id)] ?? 0) + (genToC * w) / gen;
    const unexplained = consumption - gridToC - batToC - genToC;
    if (isUnattributed(unexplained, consumption)) mix.unattributed = unexplained;
    consumerShares = normalized(mix);
  }

  // consumers: their own measured power, bounded by the total consumption; an unknown home stays unknown
  const cap = total ?? Number.POSITIVE_INFINITY;
  const home = x.home === null ? null : Math.min(Math.max(0, x.home), cap);
  if (home !== null) out[NODE.home] = { powerW: home, shares: home > 0 ? consumerShares : {} };
  for (const [id, raw] of Object.entries(x.beside)) {
    if (raw === null) continue;
    const w = Math.min(Math.max(0, raw), cap);
    out[id] = { powerW: w, shares: w > 0 ? consumerShares : {} };
  }
  for (const [id, raw] of Object.entries(x.inside)) {
    // inside home: at most home's power carries home's mix, any excess (independently sampled) is unattributed
    if (raw === null) continue;
    const w = Math.max(0, raw), homeW = home ?? 0;
    let shares: Shares = {};
    if (w > 0) {
      const attributed = homeW > 0 && Object.keys(consumerShares).length > 0 ? Math.min(1, homeW / w) : 0;
      shares = scaled(consumerShares, attributed);
      if (attributed < 1) shares.unattributed = (shares.unattributed ?? 0) + (1 - attributed);
    }
    out[id] = { powerW: w, shares: normalized(scaled(shares, 1000)) };
  }
  return out;
}

/** Hub edges a wire is made of: the mesh wires share PV, battery, grid and consumption; a consumer only its own. */
export function inputEdges(wireId: string, topology: Topology): string[] {
  if (MESH_WIRES.includes(wireId)) {
    return [...topology.pvs.map((m) => m.id), ...(topology.battery ? [topology.battery.id] : []), "grid", NODE.consumption];
  }
  return [wireId];
}

export function hasStaleInput(wireId: string, snapshot: PlantSnapshot | null, topology: Topology,
                              assumedZero: string[] = []): boolean {
  const inputs = new Set(inputEdges(wireId, topology).filter((e) => !assumedZero.includes(e)));
  return (snapshot?.edges ?? []).some((e) => inputs.has(e.edge_id) && e.state === "stale");
}

const GOLDEN = 0.618_033_988_749_895;

/** Colour of the ball with identity `n` – the golden-ratio sequence over the cumulative shares. */
export function sourceFor(n: number, shares: Shares): Source | null {
  const ordered = SOURCES.filter((s) => (shares[s] ?? 0) > 0).map((s) => [s, shares[s] as number] as const);
  if (ordered.length === 0) return null;
  let u = (n * GOLDEN) % 1;                 // like Swift's truncatingRemainder: sign of n
  if (u < 0) u += 1;
  const total = ordered.reduce((a, [, w]) => a + w, 0);
  let acc = 0;
  for (const [s, w] of ordered) {
    acc += w / total;
    if (u < acc) return s;
  }
  return (ordered[ordered.length - 1] as readonly [Source, number])[0];
}

/** Colours of the balls on one wire: a ball keeps the source it got when it appeared. */
export class BallColours {
  private assigned = new Map<number, Source>();

  colour(identity: number, shares: Shares): Source | null {
    const known = this.assigned.get(identity);
    if (known) return known;
    const c = sourceFor(identity, shares);
    if (c) this.assigned.set(identity, c);
    return c;
  }

  keepOnly(identities: Set<number>): void {
    for (const k of [...this.assigned.keys()]) if (!identities.has(k)) this.assigned.delete(k);
  }
}
