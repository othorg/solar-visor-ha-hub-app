// Dashboard colours on black – the dark values of the apps' scheme „Klar“ (ios/SharedStyle/Palette.swift, Hex) and the
// SOC ramp (SolarVisorKit/SOCGauge.swift). The card is always dark like the app dashboard.
import type { Source } from "./allocation";
import type { FlowLayout, FlowRole } from "./layout";

export const COLOR = {
  pv: ["#F7B733", "#E2684A", "#E3F2CE"],
  grid: "#8F949C",
  home: "#87D576",
  wallbox: "#EC7BD6",
  battery: "#3D8BF2",
  junction: "#E4E7EC",
  womo: "#23A28C",
} as const;

export function pvColor(index: number): string {
  return COLOR.pv[Math.min(Math.max(index, 0), 2)] as string;
}

export function nodeColor(role: FlowRole): string {
  switch (role.kind) {
    case "pv": return pvColor(role.index);
    case "generation":
    case "consumption": return COLOR.junction;
    case "grid": return COLOR.grid;
    case "home": return COLOR.home;
    case "generic": return COLOR.womo;
    case "wallbox": return COLOR.wallbox;
    case "battery": return COLOR.battery;
  }
}

/** Static colour of a wire by the side it starts from. */
export function edgeColor(id: string, layout: FlowLayout): string {
  switch (id) {
    case "GC":
    case "GB":
    case "GN": return COLOR.pv[0];
    case "BC":
    case "BN": return COLOR.battery;
    case "NC": return COLOR.grid;
    default: {
      const n = layout.nodes.get(id);
      return n ? nodeColor(n.role) : COLOR.wallbox;
    }
  }
}

/** Colour of a ball: its source. */
export function sourceColor(s: Source): string {
  switch (s) {
    case "grid": return COLOR.grid;
    case "battery": return COLOR.battery;
    case "pv1": return pvColor(0);
    case "pv2": return pvColor(1);
    case "pv3": return pvColor(2);
    case "unattributed": return "rgba(255,255,255,0.55)";
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
