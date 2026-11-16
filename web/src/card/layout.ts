// Dashboard flow layout – port of SolarVisorKit/PlantFlow.swift (PlantFlowLayout) and FlowLayout.swift
// (FlowEdgeGeometry). Fixed places on the 440 × 956 reference raster; keep in step with Swift – the shared fixtures in
// contracts/fixtures/flow/ check both.
import { NODE, pvIndex, type Topology } from "./topology";

export interface Point {
  x: number;
  y: number;
}

export type FlowRole =
  | { kind: "pv"; index: number }
  | { kind: "grid" | "battery" | "home" | "wallbox" | "generation" | "consumption" }
  | { kind: "generic"; satellite: boolean };

export function isJunction(role: FlowRole): boolean {
  return role.kind === "generation" || role.kind === "consumption";
}

/** Role as text (fixtures): `pv(0)`, `grid`, `generic(satellite)`. */
export function roleName(role: FlowRole): string {
  if (role.kind === "pv") return `pv(${role.index})`;
  if (role.kind === "generic") return role.satellite ? "generic(satellite)" : "generic";
  return role.kind;
}

export interface FlowNode {
  id: string;
  role: FlowRole;
  center: Point;
  radius: number;
}

export class EdgeGeometry {
  readonly length: number;

  constructor(readonly edgeId: string, readonly points: Point[]) {
    let l = 0;
    for (let i = 1; i < points.length; i++) {
      const a = points[i - 1] as Point, b = points[i] as Point;
      l += Math.hypot(b.x - a.x, b.y - a.y);
    }
    this.length = l;
  }

  /** Point at arc length `s` (clamped) along the polyline. */
  pointAt(s: number): Point {
    let remaining = Math.max(0, Math.min(s, this.length));
    for (let i = 1; i < this.points.length; i++) {
      const a = this.points[i - 1] as Point, b = this.points[i] as Point;
      const seg = Math.hypot(b.x - a.x, b.y - a.y);
      if (remaining <= seg && seg > 0) {
        const t = remaining / seg;
        return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
      }
      remaining -= seg;
    }
    return this.points[this.points.length - 1] ?? { x: 0, y: 0 };
  }
}

export const REFERENCE = { width: 440, height: 956 };
/** Mesh wires between the four hubs (generation, grid, battery, consumption); battery wires only with a battery. */
export const MESH_WIRES = ["GC", "GB", "GN", "NC", "BC", "BN"];

type Circle = { x: number; y: number; r: number };

/** Polyline with every inner corner replaced by a sampled quadratic curve of about `radius`. */
export function rounded(points: Point[], radius: number, steps = 8): Point[] {
  if (points.length <= 2) return points;
  const out: Point[] = [points[0] as Point];
  for (let i = 1; i < points.length - 1; i++) {
    const a = points[i - 1] as Point, c = points[i] as Point, b = points[i + 1] as Point;
    const la = Math.hypot(c.x - a.x, c.y - a.y), lb = Math.hypot(b.x - c.x, b.y - c.y);
    const r = Math.min(radius, la / 2, lb / 2);
    if (!(la > 0 && lb > 0 && r > 0)) {
      out.push(c);
      continue;
    }
    const start = { x: c.x - ((c.x - a.x) / la) * r, y: c.y - ((c.y - a.y) / la) * r };
    const end = { x: c.x + ((b.x - c.x) / lb) * r, y: c.y + ((b.y - c.y) / lb) * r };
    for (let k = 0; k <= steps; k++) {
      const t = k / steps, u = 1 - t;
      out.push({ x: u * u * start.x + 2 * u * t * c.x + t * t * end.x, y: u * u * start.y + 2 * u * t * c.y + t * t * end.y });
    }
  }
  out.push(points[points.length - 1] as Point);
  return out;
}

export class FlowLayout {
  readonly scale: number;
  readonly nodes = new Map<string, FlowNode>();
  readonly edges = new Map<string, EdgeGeometry>();
  /** Area covered by nodes on the reference raster. */
  readonly contentBounds: { x: number; y: number; width: number; height: number };
  /** Drawing order of the wires (the straight grid wire crosses the trunk last). */
  readonly wireOrder: string[];

  constructor(topology: Topology, width: number) {
    const s = width / REFERENCE.width;
    this.scale = s;
    const p = (x: number, y: number): Point => ({ x: x * s, y: y * s });
    const node = (id: string, role: FlowRole, x: number, y: number, d: number) =>
      this.nodes.set(id, { id, role, center: p(x, y), radius: (d / 2) * s });
    const wire = (id: string, pts: [number, number][]) =>
      this.edges.set(id, new EdgeGeometry(id, rounded(pts.map(([x, y]) => p(x, y)), 10 * s)));
    const straight = (id: string, pts: [number, number][]) =>
      this.edges.set(id, new EdgeGeometry(id, pts.map(([x, y]) => p(x, y))));
    const below = (c: Circle, dx: number) => c.y + Math.sqrt(c.r * c.r - dx * dx);
    const above = (c: Circle, dx: number) => c.y - Math.sqrt(c.r * c.r - dx * dx);
    const right = (c: Circle, dy: number) => c.x + Math.sqrt(c.r * c.r - dy * dy);
    const left = (c: Circle, dy: number) => c.x - Math.sqrt(c.r * c.r - dy * dy);

    const gen: Circle = { x: 220, y: 297, r: 29 }, grid: Circle = { x: 110, y: 468, r: 58 };
    const cons: Circle = { x: 295, y: 468, r: 25 };
    const bat: Circle = { x: 220, y: 690, r: 58 }, home: Circle = { x: 350, y: 377, r: 54 };
    const lane = 8;

    // PV row: one above the junction, two as before, three narrower
    const pvs = topology.pvs;
    const pvPlaces = pvs.length === 1 ? [{ x: 220, d: 120 }]
      : pvs.length === 2 ? [{ x: 110, d: 120 }, { x: 330, d: 120 }]
      : [{ x: 90, d: 104 }, { x: 220, d: 104 }, { x: 350, d: 104 }];
    pvs.forEach((m, i) => {
      const place = pvPlaces[i];
      if (!place) return;
      node(m.id, { kind: "pv", index: pvIndex(m.id) ?? 0 }, place.x, 188, place.d);
      const bottom = 188 + place.d / 2;
      if (place.x === gen.x) {
        straight(m.id, [[place.x, bottom], [gen.x, gen.y - gen.r]]);
      } else {
        const side = place.x < gen.x ? gen.x - gen.r : gen.x + gen.r;
        straight(m.id, [[place.x, bottom], [place.x, gen.y], [side, gen.y]]);
      }
    });
    node(NODE.generation, { kind: "generation" }, gen.x, gen.y, gen.r * 2);
    node("grid", { kind: "grid" }, grid.x, grid.y, grid.r * 2);
    node(NODE.home, { kind: "home" }, home.x, home.y, home.r * 2);
    node(NODE.consumption, { kind: "consumption" }, cons.x, cons.y, cons.r * 2);
    const battery = topology.battery;
    if (battery) node(battery.id, { kind: "battery" }, bat.x, bat.y, bat.r * 2);

    wire("GC", [[gen.x + lane, below(gen, lane)], [gen.x + lane, grid.y - lane], [left(cons, -lane), grid.y - lane]]);
    if (battery) wire("GB", [[gen.x, below(gen, 0)], [bat.x, above(bat, 0)]]);
    wire("GN", [[gen.x - lane, below(gen, lane)], [gen.x - lane, grid.y - lane], [right(grid, -lane), grid.y - lane]]);
    wire("NC", [[right(grid, 0), grid.y], [left(cons, 0), cons.y]]);
    if (battery) {
      wire("BC", [[bat.x + lane, above(bat, lane)], [bat.x + lane, grid.y + lane], [left(cons, lane), grid.y + lane]]);
      wire("BN", [[bat.x - lane, above(bat, lane)], [bat.x - lane, grid.y + lane], [right(grid, lane), grid.y + lane]]);
    }
    wire(NODE.home, [[right(cons, -lane / 2), cons.y - lane / 2], [home.x, cons.y - lane / 2], [home.x, below(home, 0)]]);

    // consumers beside home in a column below home
    const column = [{ y: 558, r: 50, lane: 0 }, { y: 690, r: 44, lane: 296 }, { y: 800, r: 40, lane: 289 },
                    { y: 900, r: 40, lane: 282 }];
    topology.besideHome.forEach((m, i) => {
      const place = column[i];
      if (!place) return;
      const c: Circle = { x: 350, y: place.y, r: place.r };
      node(m.id, m.kind === "wallbox" ? { kind: "wallbox" } : { kind: "generic", satellite: false }, c.x, c.y, c.r * 2);
      if (place.lane === 0) {
        wire(m.id, [[right(cons, lane / 2), cons.y + lane / 2], [c.x, cons.y + lane / 2], [c.x, above(c, 0)]]);
      } else {
        wire(m.id, [[place.lane, below(cons, place.lane - cons.x)], [place.lane, c.y], [left(c, 0), c.y]]);
      }
    });

    // modules inside home: satellites around home
    const satellites: Circle[] = [{ x: 398, y: 276, r: 36 }, { x: 414, y: 452, r: 26 }, { x: 421, y: 343, r: 19 }];
    topology.partOfHome.forEach((m, i) => {
      const c = satellites[i];
      if (!c) return;
      node(m.id, { kind: "generic", satellite: true }, c.x, c.y, c.r * 2);
      const dx = c.x - home.x, dy = c.y - home.y, d = Math.sqrt(dx * dx + dy * dy);
      const ux = dx / d, uy = dy / d;
      const r1 = (v: number) => Math.round(v * 10) / 10;
      straight(m.id, [[r1(home.x + home.r * ux), r1(home.y + home.r * uy)], [r1(c.x - c.r * ux), r1(c.y - c.r * uy)]]);
    });

    const all = [...this.nodes.values()];
    const top = Math.min(...all.map((n) => n.center.y / s - n.radius / s));
    const bottom = Math.max(...all.map((n) => n.center.y / s + n.radius / s));
    this.contentBounds = { x: 0, y: top, width: REFERENCE.width, height: bottom - top };
    const consumers = [NODE.home, ...topology.besideHome.map((m) => m.id), ...topology.partOfHome.slice(0, 3).map((m) => m.id)];
    this.wireOrder = [...pvs.map((m) => m.id), "GC", "GB", "GN", "BC", "BN", "NC", ...consumers]
      .filter((id) => this.edges.has(id));
  }

  /** Balls along a wire with their identity: evenly spaced, `phase` = distance travelled. */
  balls(edgeId: string, phase: number, forward: boolean, spacing = 30): { point: Point; identity: number }[] {
    const edge = this.edges.get(edgeId);
    if (!edge) return [];
    const gap = spacing * this.scale;
    const length = edge.length;
    if (!(length > 0 && gap > 0)) return [];
    const count = Math.max(1, Math.round(length / gap));
    const step = length / count;
    const travelled = Math.floor(phase / step);
    const offset = phase - travelled * step;
    const out: { point: Point; identity: number }[] = [];
    for (let i = 0; i < count; i++) {
      const at = i * step + offset;
      out.push({ point: edge.pointAt(forward ? at : length - at), identity: i - travelled });
    }
    return out;
  }
}
