// Node symbols of the card – the apps' own shapes (ios/SharedApp/NodeIcons.swift: solar panel, pylon, motorhome,
// battery with fill level) and Material Design Icons for house, EV charger and plug (Apache 2.0; the apps use SF Symbols,
// which may not be used outside Apple platforms). SVG markup from numbers and fixed strings only – no user text.
import { mdiEvStation, mdiHome, mdiPowerPlug } from "@mdi/js";
import { socColor } from "./palette";
import type { SymbolName } from "./topology";

export type Level = { kind: "none" } | { kind: "unknown" } | { kind: "percent"; value: number };

const f = (v: number) => Math.round(v * 100) / 100;

interface Box {
  x: number;
  y: number;
  size: number;
}

function solar(b: Box): string {
  const p = (x: number, y: number) => [b.x + x * b.size, b.y + y * b.size] as const;
  const tl = p(0.16, 0.14), tr = p(0.84, 0.14), br = p(0.96, 0.62), bl = p(0.04, 0.62);
  const lerp = (a: readonly number[], c: readonly number[], t: number) =>
    [(a[0] as number) + ((c[0] as number) - (a[0] as number)) * t, (a[1] as number) + ((c[1] as number) - (a[1] as number)) * t];
  const at = (u: number, v: number) => lerp(lerp(tl, tr, u), lerp(bl, br, u), v);
  const cols = 3, rows = 2, gap = 0.035;
  let d = "";
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < cols; col++) {
      const u0 = col / cols + gap, u1 = (col + 1) / cols - gap;
      const v0 = row / rows + gap * 1.4, v1 = (row + 1) / rows - gap * 1.4;
      const pts = [at(u0, v0), at(u1, v0), at(u1, v1), at(u0, v1)];
      d += `M${pts.map((q) => `${f(q[0] as number)},${f(q[1] as number)}`).join("L")}Z`;
    }
  }
  const [sx, sy] = p(0.46, 0.64);
  d += `M${f(sx)},${f(sy)}h${f(0.08 * b.size)}v${f(0.2 * b.size)}h${f(-0.08 * b.size)}Z`;      // stand
  const [fx, fy] = p(0.28, 0.84);
  const r = 0.03 * b.size;
  return `<path d="${d}"/><rect x="${f(fx)}" y="${f(fy)}" width="${f(0.44 * b.size)}" height="${f(0.07 * b.size)}" rx="${f(r)}"/>`;
}

function pylon(b: Box): string {
  const p = (x: number, y: number) => `${f(b.x + x * b.size)},${f(b.y + y * b.size)}`;
  let d = `M${p(0.3, 0.94)}L${p(0.47, 0.08)}L${p(0.53, 0.08)}L${p(0.7, 0.94)}`;
  d += `M${p(0.14, 0.3)}L${p(0.86, 0.3)}M${p(0.22, 0.5)}L${p(0.78, 0.5)}`;
  d += `M${p(0.42, 0.3)}L${p(0.6, 0.5)}M${p(0.58, 0.3)}L${p(0.4, 0.5)}`;
  d += `M${p(0.38, 0.5)}L${p(0.66, 0.94)}M${p(0.62, 0.5)}L${p(0.34, 0.94)}`;
  for (const [x, y] of [[0.14, 0.3], [0.86, 0.3], [0.22, 0.5], [0.78, 0.5]] as const) d += `M${p(x, y)}L${p(x, y + 0.07)}`;
  return `<path d="${d}" fill="none" stroke-width="${f(Math.max(1.2, b.size * 0.075))}" stroke-linecap="round" stroke-linejoin="round" data-stroke="1"/>`;
}

function camper(b: Box): string {
  const s = b.size;
  const p = (x: number, y: number) => `${f(b.x + x * s)},${f(b.y + y * s)}`;
  const rr = (x: number, y: number, w: number, h: number, r: number) => {
    const X = b.x + x * s, Y = b.y + y * s, W = w * s, H = h * s, R = r * s;
    return `M${f(X + R)},${f(Y)}h${f(W - 2 * R)}a${f(R)},${f(R)} 0 0 1 ${f(R)},${f(R)}v${f(H - 2 * R)}`
      + `a${f(R)},${f(R)} 0 0 1 ${f(-R)},${f(R)}h${f(-(W - 2 * R))}a${f(R)},${f(R)} 0 0 1 ${f(-R)},${f(-R)}`
      + `v${f(-(H - 2 * R))}a${f(R)},${f(R)} 0 0 1 ${f(R)},${f(-R)}Z`;
  };
  let d = rr(0.04, 0.18, 0.7, 0.54, 0.07);
  d += `M${p(0.72, 0.34)}L${p(0.84, 0.36)}L${p(0.96, 0.54)}L${p(0.96, 0.72)}L${p(0.72, 0.72)}Z`;
  d += rr(0.11, 0.28, 0.22, 0.14, 0.025) + rr(0.44, 0.3, 0.12, 0.34, 0.02);
  d += `M${p(0.76, 0.41)}L${p(0.83, 0.42)}L${p(0.9, 0.53)}L${p(0.76, 0.53)}Z`;
  const wheels = [0.25, 0.79].map((x) => {
    const cx = f(b.x + x * s), cy = f(b.y + 0.74 * s);
    return `<circle cx="${cx}" cy="${cy}" r="${f(0.1 * s)}" stroke="#000" stroke-width="${f(0.035 * s)}"/>`
      + `<circle cx="${cx}" cy="${cy}" r="${f(0.035 * s)}" fill="#000"/>`;
  }).join("");
  return `<path d="${d}" fill-rule="evenodd"/>${wheels}`;
}

function battery(b: Box, color: string, level: Level, dimmed: boolean): string {
  const s = b.size;
  const bx = b.x + 0.06 * s, by = b.y + 0.26 * s, bw = 0.8 * s, bh = 0.48 * s;
  const unknown = level.kind === "unknown";
  const sw = 0.055 * s;
  const dash = unknown ? ` stroke-dasharray="${f(0.07 * s)} ${f(0.05 * s)}"` : "";
  const dim = dimmed ? 0.5 : 1;                 // stale: outline and terminal like every symbol, the fill on its own
  let out = `<rect x="${f(bx)}" y="${f(by)}" width="${f(bw)}" height="${f(bh)}" rx="${f(0.1 * s)}" fill="none" stroke="${color}"`
    + ` stroke-opacity="${(unknown ? 0.6 : 1) * dim}" stroke-width="${f(sw)}"${dash}/>`;
  out += `<rect x="${f(bx + bw + 0.02 * s)}" y="${f(b.y + 0.4 * s)}" width="${f(0.07 * s)}" height="${f(0.2 * s)}" rx="${f(0.03 * s)}"`
    + ` fill="${color}" fill-opacity="${dim}"/>`;
  if (level.kind === "percent") {
    const fraction = Math.min(Math.max(level.value / 100, 0), 1);
    const inset = 0.07 * s;
    const w = (bw - 2 * inset) * fraction;
    if (w > 0) {
      out += `<rect x="${f(bx + inset)}" y="${f(by + inset)}" width="${f(w)}" height="${f(bh - 2 * inset)}" rx="${f(0.05 * s)}"`
        + ` fill="${socColor(fraction)}" fill-opacity="${dimmed ? 0.5 : 1}"/>`;
    }
  }
  return out;
}

function mdi(b: Box, path: string): string {
  const k = b.size / 24;
  return `<path transform="translate(${f(b.x)} ${f(b.y)}) scale(${f(k * 1000) / 1000})" d="${path}"/>`;
}

/** SVG markup of a symbol in the square at (x, y) with side `size`; colour applies as fill (stroke for the pylon). */
export function iconMarkup(symbol: SymbolName, x: number, y: number, size: number, color: string,
                           level: Level = { kind: "none" }, dimmed = false): string {
  const b = { x, y, size };
  switch (symbol) {
    case "solar": return `<g fill="${color}">${solar(b)}</g>`;
    case "grid": return `<g stroke="${color}">${pylon(b)}</g>`;
    case "camper": return `<g fill="${color}">${camper(b)}</g>`;
    case "battery": return battery(b, color, level, dimmed);
    case "house": return `<g fill="${color}">${mdi(b, mdiHome)}</g>`;
    case "wallbox": return `<g fill="${color}">${mdi(b, mdiEvStation)}</g>`;
    case "plug": return `<g fill="${color}">${mdi(b, mdiPowerPlug)}</g>`;
  }
}
