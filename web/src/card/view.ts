// The card's flow diagram – the app dashboard (ios/SharedApp/DashboardView.swift) in SVG: wires, glowing node rings,
// symbols with value and detail, energy balls in their source colour; reduced motion → direction chevrons. Static parts
// are redrawn per snapshot, only the balls per animation frame (paused when nothing flows or the card is not visible).
import { hasStaleInput, inputsFromSnapshot } from "./allocation";
import { FlowAnimator, flowWires, SPEED } from "./animation";
import { iconMarkup, type Level } from "./icons";
import { EdgeGeometry, FlowLayout, isJunction, type FlowNode, type Point } from "./layout";
import type { PlantSnapshot } from "./model";
import { edgeColor, nodeColor, sourceColor } from "./palette";
import { glowLevel, nodeText, readingOrder, usableSoc } from "./presentation";
import type { CardSensor } from "./api";
import type { CardState } from "./panel";
import type { Texts } from "./texts";
import type { Topology } from "./topology";

/** Whether a node takes a click: the card opens Home Assistant's history, so only nodes with sensors do. */
export type Clickable = (id: string, sensors: Record<string, CardSensor[]>) => boolean;
const HAS_SENSORS: Clickable = (id, sensors) => (sensors[id]?.length ?? 0) > 0;

export interface View {
  render(state: CardState, topology: Topology | null, t: Texts): void;
  notice(text: string): void;
  destroy(): void;
}

const SVG = "http://www.w3.org/2000/svg";
const GRAY = "#8E8E93";
const PAD = 14;
let instances = 0;

export const VIEW_CSS = `
:host { display: block; }
.sv { background: #000; color: #fff; border-radius: var(--ha-card-border-radius, 12px); overflow: hidden;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; position: relative; }
.sv-head { display: flex; align-items: center; gap: 8px; padding: 10px 14px 0; font-size: 12px; color: rgba(255,255,255,0.8);
  font-variant-numeric: tabular-nums; min-height: 20px; }
.sv-head .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.sv-flow { display: block; width: 100%; height: auto; }
.sv-flow text { font-variant-numeric: tabular-nums; }
.sv-hit { fill: transparent; cursor: pointer; outline: none; }
.sv-hit:focus-visible { stroke: #fff; stroke-width: 2; stroke-dasharray: 4 3; }
.sv-hit.none { cursor: default; }
.sv-notice { color: #f5c542; font-size: 12px; padding: 0 14px 10px; }
.sv-empty { padding: 28px 16px; color: rgba(255,255,255,0.85); text-align: center; font-size: 14px; }
`;

function el<K extends keyof SVGElementTagNameMap>(name: K, attrs: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const e = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
  return e;
}

function pathData(points: Point[]): string {
  return points.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join("");
}

/** Detail rows: its own lines, a single long one wrapped onto two (the app's lineLimit(2)); at most two. */
export function detailRows(detail: string | null, width: number, size: number): string[] {
  if (!detail) return [];
  const lines = detail.split("\n");
  if (lines.length > 1) return lines.slice(0, 2);
  const capacity = Math.floor(width / (size * 0.56));        // average glyph width of the system font
  if (detail.length <= capacity) return [detail];
  const cut = (sep: string) => {
    const i = detail.lastIndexOf(sep, Math.max(capacity, Math.ceil(detail.length / 2)));
    return i > 0 ? [detail.slice(0, i).trim(), detail.slice(i + sep.length).trim()] : null;
  };
  return cut(" · ") ?? cut(" ") ?? [detail];
}

function reducedMotion(): boolean {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

export function createView(root: ShadowRoot, t0: Texts, onNode: (id: string) => void,
                           clickableNode: Clickable = HAS_SENSORS): View {
  const prefix = `sv${++instances}`;
  const box = document.createElement("div");
  box.className = "sv";
  const head = document.createElement("div");
  head.className = "sv-head";
  const dot = document.createElement("span");
  dot.className = "dot";
  const headText = document.createElement("span");
  head.append(dot, headText);
  const svg = el("svg", { class: "sv-flow", role: "group" });
  const defs = el("defs");
  const gWires = el("g"), gNodes = el("g"), gBalls = el("g"), gLabels = el("g", { "aria-hidden": "true" }), gHits = el("g");
  svg.append(defs, gWires, gNodes, gBalls, gLabels, gHits);
  const noticeBox = document.createElement("div");
  noticeBox.className = "sv-notice";
  const empty = document.createElement("div");
  empty.className = "sv-empty";
  empty.textContent = t0.waiting;
  box.append(head, empty, noticeBox);
  root.append(box);

  let layout: FlowLayout | null = null;
  let layoutFor: Topology | null = null;
  let animator = new FlowAnimator();
  let current: { snapshot: PlantSnapshot | null; topology: Topology; stale: boolean; flowing: boolean } | null = null;
  let frame = 0;
  let visible = true;
  const pools = new Map<string, SVGGElement>();

  // the preference may change while the card is shown: restart the balls (or drop them for chevrons)
  let motionQuery: MediaQueryList | null = null;
  try {
    motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  } catch {
    motionQuery = null;
  }
  let lastRender: (() => void) | null = null;
  const onMotion = () => {
    stopBalls();
    lastRender?.();
  };
  motionQuery?.addEventListener?.("change", onMotion);

  function stopBalls(): void {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    pools.clear();
    gBalls.replaceChildren();
  }

  const observer = typeof IntersectionObserver === "function"
    ? new IntersectionObserver((entries) => {
      visible = entries.some((e) => e.isIntersecting);
      if (visible) wake();
    })
    : null;
  observer?.observe(svg);

  function wake(): void {
    if (frame || !visible || !current || reducedMotion()) return;
    frame = requestAnimationFrame(step);
  }

  function step(ms: number): void {
    frame = 0;
    if (!current || !layout || !visible) return;
    const { snapshot, topology, stale, flowing } = current;
    animator.advance(ms / 1000, snapshot, topology, layout.wireOrder, stale);
    let moving = false;
    for (const id of layout.wireOrder) {
      const st = animator.state(id);
      let pool = pools.get(id);
      if (!pool) {
        pool = el("g");
        pools.set(id, pool);
        gBalls.append(pool);
      }
      if (!st || st.opacity <= 0.01) {
        pool.replaceChildren();
        continue;
      }
      moving = true;
      const balls = layout.balls(id, st.phase * layout.scale, st.renderForward);
      while (pool.childElementCount < balls.length * 2) {
        pool.append(el("circle", { r: 8 }), el("circle", { r: 3 }));
      }
      while (pool.childElementCount > balls.length * 2) pool.lastElementChild?.remove();
      balls.forEach((b, i) => {
        const source = animator.colour(id, b.identity);
        const color = source ? sourceColor(source) : edgeColor(id, layout as FlowLayout);
        const halo = pool.children[2 * i] as SVGCircleElement, core = pool.children[2 * i + 1] as SVGCircleElement;
        for (const c of [halo, core]) {
          c.setAttribute("cx", b.point.x.toFixed(2));
          c.setAttribute("cy", b.point.y.toFixed(2));
          c.setAttribute("fill", color);
        }
        halo.setAttribute("fill-opacity", (0.25 * st.opacity).toFixed(3));
        core.setAttribute("fill-opacity", st.opacity.toFixed(3));
      });
      animator.forget(id, new Set(balls.map((b) => b.identity)));
    }
    if (moving || flowing) frame = requestAnimationFrame(step);
  }

  function drawStatic(topology: Topology, snapshot: PlantSnapshot | null, stale: boolean, t: Texts,
                      sensors: Record<string, CardSensor[]>): void {
    const L = layout as FlowLayout;
    const motionOff = reducedMotion();
    const wires = flowWires(snapshot, topology, stale);
    const assumed = inputsFromSnapshot(snapshot, topology).assumedZero;
    defs.replaceChildren();
    gWires.replaceChildren();
    gNodes.replaceChildren();
    gLabels.replaceChildren();
    gHits.replaceChildren();

    for (const id of L.wireOrder) {
      const geo = L.edges.get(id);
      if (!geo) continue;
      const flow = wires[id];
      const active = Math.abs(flow?.powerW ?? 0) >= SPEED.offThresholdW;
      const dashed = stale || hasStaleInput(id, snapshot, topology, assumed);
      const color = edgeColor(id, L);
      gWires.append(el("path", { d: pathData(geo.points), fill: "none", stroke: color, "stroke-opacity": active ? 0.75 : 0.3,
                                 "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round",
                                 ...(dashed ? { "stroke-dasharray": "4 4" } : {}) }));
      if (motionOff && active && flow) chevrons(geo, flow.powerW >= 0, Math.abs(flow.powerW), color);
    }

    for (const node of L.nodes.values()) {
      const color = nodeColor(node.role);
      const op = stale ? 0.5 : 1;
      const g = glowLevel(node.id, topology, snapshot, stale);
      const { x, y } = node.center, r = node.radius;
      if (g > 0) {
        const halo = `${prefix}-halo-${node.id}`;
        defs.append(filter(halo, 8 + 22 * g));
        gNodes.append(el("circle", { cx: x, cy: y, r, fill: "none", stroke: color, "stroke-opacity": (0.15 + 0.7 * g) * op,
                                     "stroke-width": 6 + 18 * g, filter: `url(#${halo})` }));
      }
      const glow = `${prefix}-glow-${node.id}`;
      defs.append(filter(glow, 4 + 4 * g));
      gNodes.append(el("circle", { cx: x, cy: y, r, fill: "none", stroke: color, "stroke-opacity": (0.35 + 0.45 * g) * op,
                                   "stroke-width": 4 + 4 * g, filter: `url(#${glow})` }));
      gNodes.append(el("circle", { cx: x, cy: y, r, fill: "#000", stroke: color, "stroke-opacity": op,
                                   "stroke-width": isJunction(node.role) ? 3 : 2.5 }));
      labels(node, topology, snapshot, stale, t);
    }

    // hit areas in reading order – also what keyboards and screen readers reach
    for (const id of readingOrder(topology)) {
      const node = L.nodes.get(id);
      if (!node) continue;
      const text = nodeText(id, topology, snapshot, stale, t.lang);
      const clickable = clickableNode(id, sensors);
      const hit = el("circle", { cx: node.center.x, cy: node.center.y, r: node.radius, class: clickable ? "sv-hit" : "sv-hit none",
                                 role: clickable ? "button" : "img",
                                 "aria-label": clickable ? `${text.spoken}. ${t.opensHistory}` : text.spoken });
      if (clickable) {
        hit.setAttribute("tabindex", "0");
        hit.addEventListener("click", () => onNode(id));
        hit.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onNode(id);
          }
        });
      }
      gHits.append(hit);
    }
  }

  function filter(id: string, blur: number): SVGFilterElement {
    const f = el("filter", { id, x: "-100%", y: "-100%", width: "300%", height: "300%" });
    f.append(el("feGaussianBlur", { stdDeviation: (blur / 2).toFixed(2) }));
    return f;
  }

  function chevrons(geo: EdgeGeometry, forward: boolean, power: number, color: string): void {
    const opacity = power < 1000 ? 0.4 : power < 5000 ? 0.7 : 1;
    let d = "";
    for (let s = 20; s < geo.length; s += 40) {
      const a = geo.pointAt(forward ? s - 3 : s + 3), b = geo.pointAt(forward ? s + 3 : s - 3);
      const angle = Math.atan2(b.y - a.y, b.x - a.x);
      const p1 = { x: b.x - 5 * Math.cos(angle - Math.PI / 4), y: b.y - 5 * Math.sin(angle - Math.PI / 4) };
      const p2 = { x: b.x - 5 * Math.cos(angle + Math.PI / 4), y: b.y - 5 * Math.sin(angle + Math.PI / 4) };
      d += `M${p1.x.toFixed(2)},${p1.y.toFixed(2)}L${b.x.toFixed(2)},${b.y.toFixed(2)}L${p2.x.toFixed(2)},${p2.y.toFixed(2)}`;
    }
    gWires.append(el("path", { d, fill: "none", stroke: color, "stroke-opacity": opacity, "stroke-width": 1.5,
                               "stroke-linecap": "round" }));
  }

  /** Name, symbol, value and detail inside the ring (symbol nodes); value (and detail) only for the junctions. */
  function labels(node: FlowNode, topology: Topology, snapshot: PlantSnapshot | null, stale: boolean, t: Texts): void {
    const text = nodeText(node.id, topology, snapshot, stale, t.lang);
    const ink = text.dim ? GRAY : "#fff";
    const lines: { kind: "text" | "icon"; text?: string; size: number; weight?: number; fill?: string; opacity?: number }[] = [];
    const junction = isJunction(node.role);
    const detailLines = detailRows(text.detail, node.radius * (junction ? 1.8 : 1.7),
                                   junction ? 10 * (node.role.kind === "generation" ? 0.72 : 0.66) : node.radius < 45 ? 6.5 : 9);
    let width: number;
    if (junction) {
      const k = node.role.kind === "generation" ? 0.72 : 0.66;
      lines.push({ kind: "text", text: text.value, size: 16 * k, weight: 600, fill: ink });
      for (const d of detailLines) lines.push({ kind: "text", text: d, size: 10 * k, fill: ink });
      width = node.radius * 1.8;
    } else {
      const small = node.radius < 45;
      if (text.showsTitle) lines.push({ kind: "text", text: text.name, size: small ? 8 : 10.5, fill: "#fff", opacity: 0.75 });
      lines.push({ kind: "icon", size: node.radius * (small ? 0.4 : 0.5) });
      lines.push({ kind: "text", text: text.value, size: small ? 9.5 : 15, weight: 600, fill: ink });
      for (const d of detailLines) lines.push({ kind: "text", text: d, size: small ? 6.5 : 9, fill: "#fff", opacity: 0.7 });
      width = node.radius * (node.role.kind === "battery" ? 1.5 : 1.7);
    }
    const gap = junction ? 2 : 1;
    const heights = lines.map((l) => (l.kind === "icon" ? l.size : l.size * 1.2));
    const total = heights.reduce((a, b) => a + b, 0) + gap * (lines.length - 1);
    let y = node.center.y - total / 2;
    const group = el("g");
    lines.forEach((line, i) => {
      const h = heights[i] as number;
      if (line.kind === "icon") {
        const symbol = topology.symbol(node.id);
        const color = nodeColor(node.role);
        const iconGroup = el("g", { opacity: stale && symbol !== "battery" ? 0.5 : 1 });   // the battery dims itself
        iconGroup.innerHTML = iconMarkup(symbol, node.center.x - h / 2, y, h, color, level(node.id, symbol, topology, snapshot),
                                         stale);
        group.append(iconGroup);
      } else {
        const tx = el("text", { x: node.center.x, y: y + h / 2, "text-anchor": "middle", "dominant-baseline": "central",
                                "font-size": (line.size as number).toFixed(2), fill: line.fill ?? "#fff" });
        if (line.weight) tx.setAttribute("font-weight", String(line.weight));
        if (line.opacity !== undefined) tx.setAttribute("fill-opacity", String(line.opacity));
        tx.textContent = line.text ?? "";
        group.append(tx);
      }
      y += h + gap;
    });
    gLabels.append(group);
    fit(group, width);
  }

  /** Shrinks texts wider than the ring allows (down to 60 %, as the app's minimumScaleFactor), then squeezes. */
  function fit(group: SVGGElement, width: number): void {
    for (const tx of Array.from(group.querySelectorAll("text"))) {
      let w = 0;
      try {
        w = tx.getComputedTextLength();
      } catch {
        return;                              // not rendered (tests)
      }
      if (!(w > width)) continue;
      const size = Number(tx.getAttribute("font-size"));
      const k = Math.max(0.6, width / w);
      tx.setAttribute("font-size", (size * k).toFixed(2));
      if (w * k > width) {
        tx.setAttribute("textLength", width.toFixed(1));
        tx.setAttribute("lengthAdjust", "spacingAndGlyphs");
      }
    }
  }

  function level(id: string, symbol: string, topology: Topology, snapshot: PlantSnapshot | null): Level {
    if (symbol !== "battery") return { kind: "none" };
    const metricId = topology.module(id)?.kind === "battery" ? `${id}.soc` : `${id}.storage_soc`;
    const soc = usableSoc(snapshot, metricId);
    return soc === null ? { kind: "unknown" } : { kind: "percent", value: soc };
  }

  function header(state: CardState, t: Texts): void {
    const time = state.snapshot
      ? `${t.asOf} ${new Date(state.snapshot.generated_at).toLocaleTimeString(t.lang === "de" ? "de-DE" : "en-GB")}`
      : t.noData;
    headText.textContent = state.problem ? `${state.problem} · ${time}` : state.stale ? `${t.stale} · ${time}` : time;
    dot.style.background = state.stale ? "#FF9F0A" : state.problem ? "#FFD60A" : "#30D158";
  }

  return {
    render(state, topology, t) {
      header(state, t);
      if (!topology) {
        lastRender = null;                      // a preference change must not bring back an earlier plant
        stopBalls();
        empty.textContent = state.problem ?? t.waiting;
        if (svg.isConnected) svg.replaceWith(empty);
        current = null;
        return;
      }
      if (topology !== layoutFor) {
        layout = new FlowLayout(topology, 440);
        layoutFor = topology;
        animator = new FlowAnimator();
        pools.clear();
        gBalls.replaceChildren();
        const b = layout.contentBounds;
        svg.setAttribute("viewBox", `0 ${(b.y - PAD).toFixed(1)} 440 ${(b.height + 2 * PAD).toFixed(1)}`);
      }
      if (!svg.isConnected) empty.replaceWith(svg);
      svg.setAttribute("aria-label", t.lang === "de" ? "Energiefluss" : "Energy flow");
      // frames only while a wire would show balls: the gate opens at the on-threshold (a 30–50 W flow from rest stays
      // idle), a running wire keeps the loop until it has faded out
      const flowing = Object.values(flowWires(state.snapshot, topology, state.stale))
        .some((w) => Math.abs(w.powerW) >= SPEED.onThresholdW);
      current = { snapshot: state.snapshot, topology, stale: state.stale, flowing };
      drawStatic(topology, state.snapshot, state.stale, t, state.plant?.sensors ?? {});
      lastRender = () => this.render(state, topology, t);
      if (reducedMotion()) stopBalls();
      wake();
    },
    notice(text) {
      noticeBox.textContent = text;
    },
    destroy() {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      observer?.disconnect();
      motionQuery?.removeEventListener?.("change", onMotion);
      lastRender = null;
      current = null;
      box.remove();
    },
  };
}

