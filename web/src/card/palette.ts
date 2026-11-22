// Dashboard colours of the apps' scheme „Klar“ (ios/SharedStyle/Palette.swift) and the SOC ramp
// (SolarVisorKit/SOCGauge.swift): on a dark card the dark-surface values (Hex) of the app dashboard, on a light card the
// OnLight values of the home-screen widgets (same hue, darker; roles without one keep their value). Dark or light
// follows Home Assistant, the card surface is Home Assistant's (user decisions 2026-11-17).
import type { Source } from "./allocation";
import type { FlowLayout, FlowRole } from "./layout";

export type Theme = "dark" | "light";

interface Colors {
  pv: readonly [string, string, string];
  grid: string;
  home: string;
  /** The calculated home line beside the consumption (charts). */
  homeLine: string;
  wallbox: string;
  battery: string;
  junction: string;
  womo: string;
  /** Text inside the rings. */
  ink: string;
  /** A ball whose source is not known. */
  unattributed: string;
}

export const COLORS: Record<Theme, Colors> = {
  dark: {
    pv: ["#F7B733", "#E2684A", "#E3F2CE"], grid: "#8F949C", home: "#87D576", homeLine: "#C4EBB7", wallbox: "#EC7BD6", battery: "#3D8BF2",
    junction: "#E4E7EC", womo: "#23A28C", ink: "#FFFFFF", unattributed: "rgba(255,255,255,0.55)",
  },
  light: {
    pv: ["#D39914", "#E2684A", "#88B12F"], grid: "#8F949C", home: "#55A742", homeLine: "#2E7D1F", wallbox: "#F055D7", battery: "#3D8BF2",
    junction: "#6B7480", womo: "#23A28C", ink: "#1C1F24", unattributed: "rgba(0,0,0,0.45)",
  },
};

export function pvColor(index: number, theme: Theme = "dark"): string {
  return COLORS[theme].pv[Math.min(Math.max(index, 0), 2)] as string;
}

export function nodeColor(role: FlowRole, theme: Theme = "dark"): string {
  const c = COLORS[theme];
  switch (role.kind) {
    case "pv": return pvColor(role.index, theme);
    case "generation":
    case "consumption": return c.junction;
    case "grid": return c.grid;
    case "home": return c.home;
    case "generic": return c.womo;
    case "wallbox": return c.wallbox;
    case "battery": return c.battery;
  }
}

/** Static colour of a wire by the side it starts from (generation: its first generator feeding home; a generator's own
 *  wire – also one with full feed-in – in its slot colour). */
export function edgeColor(id: string, layout: FlowLayout, theme: Theme = "dark"): string {
  const c = COLORS[theme];
  switch (id) {
    case "GC":
    case "GB":
    case "GN": return pvColor(layout.meshPvIndex, theme);
    case "BC":
    case "BN": return c.battery;
    case "NC": return c.grid;
    default: {
      const n = layout.nodes.get(id);
      return n ? nodeColor(n.role, theme) : c.wallbox;
    }
  }
}

/** Colour of a ball: its source. */
export function sourceColor(s: Source, theme: Theme = "dark"): string {
  const c = COLORS[theme];
  switch (s) {
    case "grid": return c.grid;
    case "battery": return c.battery;
    case "pv1": return pvColor(0, theme);
    case "pv2": return pvColor(1, theme);
    case "pv3": return pvColor(2, theme);
    case "unattributed": return c.unattributed;
  }
}

const SOC_STOPS: [number, [number, number, number]][] = [
  [0.0, [0.92, 0.2, 0.18]], [0.33, [0.95, 0.55, 0.18]], [0.66, [0.96, 0.84, 0.22]], [1.0, [0.39, 0.77, 0.4]],
];

/** SOC ramp red → orange → yellow → green at position 0…1, as `rgb(…)`. */
export function socColor(position: number): string {
  const t = Math.min(Math.max(position, 0), 1);
  const upper = SOC_STOPS.findIndex(([p]) => p >= t);
  let rgb: [number, number, number];
  if (upper <= 0) {
    rgb = (SOC_STOPS[0] as [number, [number, number, number]])[1];
  } else {
    const [t0, c0] = SOC_STOPS[upper - 1] as [number, [number, number, number]];
    const [t1, c1] = SOC_STOPS[upper] as [number, [number, number, number]];
    const f = (t - t0) / (t1 - t0);
    rgb = [c0[0] + (c1[0] - c0[0]) * f, c0[1] + (c1[1] - c0[1]) * f, c0[2] + (c1[2] - c0[2]) * f];
  }
  return `rgb(${rgb.map((v) => Math.round(v * 255)).join(",")})`;
}

/** Lit segments of the SOC ring for a SOC in percent (SOCGauge.litSegments): rounded down, unknown lights none. */
export function socLitSegments(soc: number | null, segments: number): number {
  if (soc === null || !Number.isFinite(soc)) return 0;
  return Math.floor((Math.min(Math.max(soc, 0), 100) / 100) * segments);
}
