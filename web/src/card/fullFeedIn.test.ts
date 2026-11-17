// A PV generator with full feed-in (plan full-feed-in §5) on a synthetic plant: its own node left above the grid, its
// own wire into the grid, never part of the home's mesh, marked „Volleinspeisung“.
import { afterEach, describe, expect, it, vi } from "vitest";
import { hasStaleInput, inputsFromSnapshot, wires } from "./allocation";
import { FlowLayout } from "./layout";
import type { PlantCapabilities } from "./model";
import { COLORS, edgeColor } from "./palette";
import { nodeText, readingOrder } from "./presentation";
import { FEED_IN_CAPABILITIES, feedInSnapshot } from "./testPlant";
import { texts } from "./texts";
import { FULL_FEED_IN, Topology } from "./topology";
import { createView } from "./view";

const topology = new Topology(FEED_IN_CAPABILITIES);

/** Three generators, those in `feedIn` with full feed-in. */
function three(feedIn: string[]): Topology {
  const pv = (id: string) => ({ id, kind: "pv", name: "", balance: feedIn.includes(id) ? FULL_FEED_IN : null,
                                points: [{ id: "power", unit: "W", role: "authoritative" }], strings: [] });
  const caps: PlantCapabilities = { ...FEED_IN_CAPABILITIES, modules: [FEED_IN_CAPABILITIES.modules[0]!, pv("pv_1"), pv("pv_2"),
                                                                       pv("pv_3")] };
  return new Topology(caps);
}

function points(layout: FlowLayout, id: string): number[][] {
  return (layout.edges.get(id)?.points ?? []).map((p) => [Math.round(p.x * 100) / 100, Math.round(p.y * 100) / 100]);
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("full feed-in", () => {
  it("is a PV generator outside the home's generators", () => {
    expect(topology.pvs.map((m) => m.id)).toEqual(["pv_1"]);
    expect(topology.feedInPvs.map((m) => m.id)).toEqual(["pv_2"]);
    expect(topology.allPvs.map((m) => m.id)).toEqual(["pv_1", "pv_2"]);
  });

  it("takes the left places above the grid whatever its slot, the wire straight into the grid", () => {
    const l = new FlowLayout(topology, 440);
    expect(l.nodes.get("pv_2")?.center).toEqual({ x: 110, y: 188 });
    expect(l.nodes.get("pv_1")?.center).toEqual({ x: 330, y: 188 });
    expect(points(l, "pv_2")).toEqual([[110, 248], [110, 410]]);
    expect(l.nodes.get("pv_2")?.role).toEqual({ kind: "pv", index: 1 });       // the colour stays with the slot
    expect(l.meshPvIndex).toBe(0);
    const threeOne = new FlowLayout(three(["pv_2"]), 440);
    expect(["pv_1", "pv_2", "pv_3"].map((id) => threeOne.nodes.get(id)?.center.x)).toEqual([220, 90, 350]);
    expect(points(threeOne, "pv_2")).toEqual([[90, 240], [90, 413.56]]);
    const threeTwo = new FlowLayout(three(["pv_1", "pv_2"]), 440);
    expect(points(threeTwo, "pv_2")).toEqual([[220, 240], [220, 254], [135, 254], [135, 415.66]]);
    expect(threeTwo.meshPvIndex).toBe(2);
  });

  it("draws the generation mesh in the colour of the first generator feeding home", () => {
    const l = new FlowLayout(three(["pv_1"]), 440);
    expect(edgeColor("GN", l, "dark")).toBe(COLORS.dark.pv[1]);
    expect(edgeColor("pv_1", l, "light")).toBe(COLORS.light.pv[0]);
    expect(edgeColor("GC", new FlowLayout(topology, 440), "dark")).toBe(COLORS.dark.pv[0]);
  });

  it("carries only its own power to the grid; unknown or stale it touches only its own wire", () => {
    const x = inputsFromSnapshot(feedInSnapshot(), topology);
    expect(x.pv).toEqual({ pv_1: 3000 });
    expect(x.feedIn).toEqual({ pv_2: 4800 });
    const w = wires(x);
    expect(w.pv_2).toEqual({ powerW: 4800, shares: { pv2: 1 } });
    expect(w.GN).toEqual({ powerW: 1200, shares: { pv1: 1 } });               // the home's mix without it
    expect(Object.keys(w.home?.shares ?? {}).sort()).toEqual(["battery", "pv1"]);    // no full feed-in in home
    const unknown = wires(inputsFromSnapshot(feedInSnapshot({ value: null, state: "unknown" }), topology));
    expect(unknown.pv_2).toBeUndefined();
    expect(unknown.GN).toEqual(w.GN);
    const stale = feedInSnapshot({ value: 4800, state: "stale" });
    expect(hasStaleInput("pv_2", stale, topology)).toBe(true);
    for (const id of ["GC", "GB", "GN", "NC", "BC", "BN", "pv_1", "home"]) expect(hasStaleInput(id, stale, topology)).toBe(false);
  });

  it("is marked in German and English and read after the grid", () => {
    expect(nodeText("pv_2", topology, feedInSnapshot(), false, "de").detail).toBe("Volleinspeisung");
    expect(nodeText("pv_2", topology, feedInSnapshot(), false, "en").detail).toBe("full feed-in");
    expect(nodeText("pv_1", topology, feedInSnapshot(), false, "de").detail).toBeNull();
    const order = readingOrder(topology);
    expect(order.indexOf("pv_2")).toBe(order.indexOf("grid") + 1);
  });

  it("is drawn by the card with its marker", () => {
    vi.stubGlobal("requestAnimationFrame", () => 0);
    const host = document.createElement("div");
    document.body.append(host);
    const root = host.attachShadow({ mode: "open" });
    const t = texts({ callWS: vi.fn(), language: "de" });
    const view = createView(root, t, vi.fn(), () => false);
    const plant = { build: "b", capabilities: FEED_IN_CAPABILITIES, sensors: {} };
    view.render({ plant, snapshot: feedInSnapshot(), stale: false, problem: null }, topology, t);
    const text = root.querySelector("svg")?.textContent ?? "";
    expect(text).toContain("Scheune");
    expect(text).toContain("Volleinspeisung");
    expect(text).toContain("4,80 kW");
    const labels = [...root.querySelectorAll(".sv-hit")].map((h) => h.getAttribute("aria-label") ?? "");
    expect(labels.findIndex((l) => l.startsWith("Scheune"))).toBe(labels.findIndex((l) => l.startsWith("Netz")) + 1);
    view.destroy();
  });
});
