// The calculated curve of a node without Home Assistant sensors (home, user decision 2026-11-17): the hub's power of
// today in the dialog of the history – total consumption and home as in the apps' node detail (NodeDetailView): kW,
// steps per column (the apps' stepCenter), calculated series dashed with „≈“, a gap where nothing is known (never 0),
// colours of the card's theme. Reads only.
import { openDialog } from "./history";
import type { PlantPowerResponse, PlantPowerSeries } from "./model";
import { COLORS, type Theme } from "./palette";

/** Nodes whose tap opens the hub's curve instead of Home Assistant's history (ui_card.NODE_HISTORY). */
export const CURVE_NODES: readonly string[] = ["home"];

export interface CurveRequest {
  title: string;
  note: string;
  close: string;
  loading: string;
  failed: string;
  lang: "de" | "en";
  theme: Theme;
  /** Shown instead of an empty chart. */
  empty: string;
  /** A series in words („Verbrauch gesamt“, „Zuhause“). */
  label(seriesId: string): string;
  /** Now (tests); the curve never reaches beyond it. */
  now?: number;
}

const W = 680, H = 240, LEFT = 46, RIGHT = 12, TOP = 10, BOTTOM = 24;
const HOUR_MS = 3_600_000;
const STEPS_KW = [0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50];
const SVG = "http://www.w3.org/2000/svg";

/** A series' colour as a CSS variable of the dialog – it follows the theme also while the dialog is open. */
export function seriesColor(seriesId: string): string {
  return seriesId === "home.power" ? "var(--sv-c-home-line)" : "var(--sv-c-home)";
}

/** Light or dark colours for an open curve (Home Assistant switched its display meanwhile). */
export function setCurveTheme(root: ShadowRoot, theme: Theme): void {
  root.querySelector("dialog.sv-history")?.classList.toggle("light", theme === "light");
}

/** The instant of local ``hour`` (0–24) on the day that begins at ``startMs`` in ``timeZone``: a day of 23 or 25 hours
 *  (daylight saving) puts it an hour earlier or later; 24 is the next midnight. */
export function atLocalHour(startMs: number, timeZone: string, hour: number): number {
  const hm = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone });
  const want = `${String(hour % 24).padStart(2, "0")}:00`;
  for (const shift of [0, -1, 1]) {
    const t = startMs + (hour + shift) * HOUR_MS;
    if (hm.format(t) === want) return t;
  }
  return startMs + hour * HOUR_MS;
}

/** Decimals that show a tick step exactly (0,25 → 2; 2,5 → 1; 2 → 0). */
export function stepDecimals(step: number): number {
  return (String(step).split(".")[1] ?? "").length;
}

/** Value axis in kW: 0 always inside, at most five ticks of a round step. */
export function kwTicks(values: number[]): number[] {
  const lo = Math.min(0, ...values), hi = Math.max(0, ...values);
  const step = STEPS_KW.find((s) => Math.ceil(hi / s) - Math.floor(lo / s) <= 4) ?? STEPS_KW[STEPS_KW.length - 1]!;
  const first = Math.floor(lo / step), last = Math.max(Math.ceil(hi / step), first + 1);
  return Array.from({ length: last - first + 1 }, (_, i) => Number(((first + i) * step).toFixed(4)));
}

function el<K extends keyof SVGElementTagNameMap>(name: K, attrs: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const e = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
  return e;
}

/** Step path of a series: one level per column, broken where a column is unknown. */
export function stepPath(s: PlantPowerSeries, x: (ms: number) => number, y: (kw: number) => number, endMs: number): string {
  let d = "", lastEnd: number | null = null;
  for (const p of s.points) {
    const a = Date.parse(p.t), b = Math.min(a + s.resolution_s * 1000, endMs);
    if (p.mean === null || !(b > a)) {
      lastEnd = null;
      continue;
    }
    const yy = y(p.mean / 1000).toFixed(1);
    d += `${lastEnd === a ? "L" : "M"}${x(a).toFixed(1)},${yy}L${x(b).toFixed(1)},${yy}`;
    lastEnd = b;
  }
  return d;
}

/** Whether a response has any known value. */
export function hasValues(resp: PlantPowerResponse): boolean {
  return resp.series.some((s) => s.points.some((p) => p.mean !== null));
}

export function curveChart(resp: PlantPowerResponse, req: CurveRequest): HTMLElement {
  const box = document.createElement("div");
  box.className = "sv-curve";
  const start = Date.parse(resp.start);
  const end = Math.min(Date.parse(resp.end), req.now ?? Date.now());
  const domainEnd = Math.max(atLocalHour(start, resp.timezone, 24), end);   // the whole local day, data until now
  const values = resp.series.flatMap((s) => s.points.flatMap((p) => (p.mean === null ? [] : [p.mean / 1000])));
  const ticks = kwTicks(values);
  const lo = ticks[0]!, hi = ticks[ticks.length - 1]!;
  const x = (ms: number) => LEFT + ((ms - start) / (domainEnd - start)) * (W - LEFT - RIGHT);
  const y = (kw: number) => TOP + ((hi - kw) / (hi - lo)) * (H - TOP - BOTTOM);
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
                          "aria-label": `${req.title}: ${resp.series.map((s) => req.label(s.series_id)).join(", ")}` });
  const decimals = stepDecimals(Number((ticks[1]! - ticks[0]!).toFixed(4)));
  const fmt = (v: number) => (req.lang === "de" ? v.toFixed(decimals).replace(".", ",") : v.toFixed(decimals));
  for (const v of ticks) {
    svg.append(el("line", { x1: LEFT, x2: W - RIGHT, y1: y(v), y2: y(v), stroke: "currentColor",
                            "stroke-opacity": v === 0 ? 0.4 : 0.15, "stroke-dasharray": v === 0 ? "" : "2 3" }));
    const label = el("text", { x: LEFT - 6, y: y(v), "text-anchor": "end", "dominant-baseline": "central",
                               "font-size": 11, fill: "currentColor", "fill-opacity": 0.7 });
    label.textContent = fmt(v);
    svg.append(label);
  }
  const unit = el("text", { x: 4, y: TOP + 2, "font-size": 11, fill: "currentColor", "fill-opacity": 0.7,
                            "dominant-baseline": "hanging" });
  unit.textContent = "kW";
  svg.append(unit);
  const time = new Intl.DateTimeFormat(req.lang === "de" ? "de-DE" : "en-GB",
                                       { hour: "2-digit", minute: "2-digit", timeZone: resp.timezone });
  for (let k = 0; k <= 4; k++) {
    const t = atLocalHour(start, resp.timezone, 6 * k);
    const label = el("text", { x: x(t), y: H - 6, "text-anchor": k === 0 ? "start" : k === 4 ? "end" : "middle",
                               "font-size": 11, fill: "currentColor", "fill-opacity": 0.7 });
    label.textContent = k === 4 ? "24:00" : time.format(t);
    svg.append(label);
  }
  for (const s of resp.series) {
    svg.append(el("path", { d: stepPath(s, x, y, end), fill: "none", style: `stroke: ${seriesColor(s.series_id)}`,
                            "stroke-width": 1.8, "stroke-linejoin": "round", "stroke-linecap": "round",
                            ...(s.derived ? { "stroke-dasharray": "4 3" } : {}) }));
  }
  const legend = document.createElement("div");
  legend.className = "sv-curve-legend";
  for (const s of resp.series) {
    const item = document.createElement("span");
    const dot = document.createElement("i");
    dot.style.background = seriesColor(s.series_id);
    item.append(dot, `${s.derived ? "≈ " : ""}${req.label(s.series_id)}`);
    legend.append(item);
  }
  box.append(svg, legend);
  return box;
}

/** Opens the dialog at once (loading), then shows the curve or why there is none. Closing aborts the request. */
export function openCurve(root: ShadowRoot, req: CurveRequest,
                          load: (signal: AbortSignal) => Promise<PlantPowerResponse>): void {
  const content = document.createElement("div");
  content.className = "sv-curve-body";
  const title = document.createElement("h3");
  title.textContent = req.title;
  const status = document.createElement("p");
  status.className = "sv-curve-status";
  status.textContent = req.loading;
  content.append(title, status);
  const abort = new AbortController();
  if (!openDialog(root, req.note, req.close, content, (d) => d.classList.toggle("light", req.theme === "light"),
                  () => abort.abort())) return;
  load(abort.signal).then(
    (resp) => {
      if (abort.signal.aborted) return;
      if (hasValues(resp)) status.replaceWith(curveChart(resp, req));
      else status.textContent = req.empty;
    },
    () => {
      if (!abort.signal.aborted) status.textContent = req.failed;
    },
  );
}

export const CURVE_CSS = `
dialog.sv-history { --sv-c-home: ${COLORS.dark.home}; --sv-c-home-line: ${COLORS.dark.homeLine}; }
dialog.sv-history.light { --sv-c-home: ${COLORS.light.home}; --sv-c-home-line: ${COLORS.light.homeLine}; }
.sv-curve-body h3 { margin: 0 0 8px; font-size: 16px; font-weight: 500; }
.sv-curve svg { display: block; width: 100%; height: auto; font-variant-numeric: tabular-nums; }
.sv-curve-legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; margin-top: 4px;
  color: var(--secondary-text-color, #9b9b9b); }
.sv-curve-legend i { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; }
.sv-curve-status { font-size: 13px; color: var(--secondary-text-color, #9b9b9b); }
`;
