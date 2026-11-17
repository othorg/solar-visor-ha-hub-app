// The hub's curve of home in the card (user decision 2026-11-17): axis, steps and gaps, legend, dialog, the tap on home.
import { afterEach, describe, expect, it, vi } from "vitest";
import { atLocalHour, CURVE_CSS, CURVE_NODES, curveChart, kwTicks, openCurve, seriesColor, stepDecimals, stepPath,
         type CurveRequest } from "./curve";
import { closeHistory } from "./history";
import type { PlantPowerResponse, PlantPowerSeries } from "./model";
import { COLORS } from "./palette";
import { create } from "./renderer";
import { cardFetch, testPlant } from "./testPlant";

const START = "2026-11-16T23:00:00Z";                      // Berlin midnight

function series(id: string, means: (number | null)[], derived = false): PlantPowerSeries {
  return { series_id: id, unit: "W", derived, resolution_s: 300, source: "statistic_mean",
           points: means.map((mean, i) => ({ t: new Date(Date.parse(START) + i * 300_000).toISOString(), mean,
                                             min: mean, max: mean, coverage: mean === null ? 0 : 1 })) };
}

const RESP: PlantPowerResponse = {
  start: START, end: "2026-11-16T23:20:00Z", timezone: "Europe/Berlin", calculation_version: 13,
  series: [series("consumption.power", [1200, 1500, null, 900]), series("home.power", [800, 1100, null, 500], true)],
};

const REQ: CurveRequest = {
  title: "Zuhause · heute", note: "Vom Hub berechnet", close: "Schließen", loading: "Lade …", failed: "nicht verfügbar",
  empty: "Keine Daten", lang: "de", theme: "dark", label: (id) => (id === "home.power" ? "Zuhause" : "Verbrauch gesamt"),
  now: Date.parse("2026-11-16T23:20:00Z"),
};

function dialogRoot(): ShadowRoot {
  HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
  const host = document.createElement("div");
  document.body.append(host);
  return host.attachShadow({ mode: "open" });
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("home curve", () => {
  it("scales kW with 0 inside and at most five round ticks", () => {
    expect(kwTicks([0.2, 0.95])).toEqual([0, 0.25, 0.5, 0.75, 1]);
    expect(kwTicks([-1.2, 3])).toEqual([-2, 0, 2, 4]);
    expect(kwTicks([])).toEqual([0, 0.1]);
  });

  it("labels a tick step exactly", () => {
    expect([0.25, 0.5, 2.5, 2].map(stepDecimals)).toEqual([2, 1, 1, 0]);
    const labels = [...curveChart({ ...RESP, series: [series("home.power", [200, 950])] }, REQ).querySelectorAll("text")]
      .map((t) => t.textContent);
    expect(labels).toEqual(expect.arrayContaining(["0,25", "0,75"]));
  });

  it("finds local hours on days of 23 and 25 hours (daylight saving, Berlin)", () => {
    const spring = Date.parse("2026-03-28T23:00:00Z"), autumn = Date.parse("2026-10-24T22:00:00Z");
    expect(new Date(atLocalHour(spring, "Europe/Berlin", 24)).toISOString()).toBe("2026-03-29T22:00:00.000Z");   // 23 h
    expect(new Date(atLocalHour(autumn, "Europe/Berlin", 24)).toISOString()).toBe("2026-10-25T23:00:00.000Z");   // 25 h
    expect(new Date(atLocalHour(spring, "Europe/Berlin", 6)).toISOString()).toBe("2026-03-29T04:00:00.000Z");
    expect(new Date(atLocalHour(Date.parse(START), "Europe/Berlin", 12)).toISOString()).toBe("2026-11-17T11:00:00.000Z");
  });

  it("never draws beyond now", () => {
    const x = (ms: number) => (ms - Date.parse(START)) / 60_000;
    const d = curveChart(RESP, { ...REQ, now: Date.parse(START) + 7 * 60_000 }).querySelector("path")?.getAttribute("d");
    const right = Math.max(...[...(d ?? "").matchAll(/[ML]([\d.]+),/g)].map((m) => Number(m[1])));
    const atNow = 46 + (x(Date.parse(START) + 7 * 60_000) / (24 * 60)) * (680 - 46 - 12);
    expect(right).toBeCloseTo(atNow, 0);
  });

  it("draws a level per column, joins neighbours, leaves unknown columns open and stops at now", () => {
    const x = (ms: number) => (ms - Date.parse(START)) / 60_000, y = (kw: number) => kw;
    const d = stepPath(series("home.power", [800, 1100, null, 500]), x, y, Date.parse(START) + 17 * 60_000);
    expect(d).toBe("M0.0,0.8L5.0,0.8L5.0,1.1L10.0,1.1M15.0,0.5L17.0,0.5");
  });

  it("shows both series in the theme's colours, the calculated one dashed with ≈", () => {
    const box = curveChart(RESP, REQ);
    const paths = [...box.querySelectorAll("path")];
    expect(paths.map((p) => p.getAttribute("style"))).toEqual(["stroke: var(--sv-c-home)", "stroke: var(--sv-c-home-line)"]);
    expect(paths.map((p) => p.hasAttribute("stroke-dasharray"))).toEqual([false, true]);
    expect(box.querySelector(".sv-curve-legend")?.textContent).toBe("Verbrauch gesamt≈ Zuhause");
    expect([...box.querySelectorAll("text")].map((t) => t.textContent)).toContain("00:00");
    expect(seriesColor("home.power")).toBe("var(--sv-c-home-line)");
    for (const theme of ["dark", "light"] as const) {
      expect(CURVE_CSS).toContain(COLORS[theme].home);
      expect(CURVE_CSS).toContain(COLORS[theme].homeLine);
    }
  });

  it("opens at once, then shows the curve – or why there is none", async () => {
    const root = dialogRoot();
    openCurve(root, REQ, async () => RESP);
    expect(root.querySelector(".sv-curve-status")?.textContent).toBe("Lade …");
    await vi.waitFor(() => expect(root.querySelector(".sv-curve svg")).not.toBeNull());
    openCurve(root, REQ, async () => Promise.reject(new Error("503")));
    expect(root.querySelectorAll("dialog")).toHaveLength(1);                 // one dialog at a time
    await vi.waitFor(() => expect(root.querySelector(".sv-curve-status")?.textContent).toBe("nicht verfügbar"));
  });

  it("stops loading however the dialog goes – closed, replaced or torn down with the card", () => {
    const root = dialogRoot();
    const signals: AbortSignal[] = [];
    const load = (s: AbortSignal) => {
      signals.push(s);
      return new Promise<PlantPowerResponse>(() => undefined);
    };
    openCurve(root, REQ, load);
    root.querySelector("dialog")?.close();
    openCurve(root, REQ, load);
    openCurve(root, REQ, load);                                               // replaces the second
    closeHistory(root);                                                       // the renderer is torn down
    expect(signals.map((s) => s.aborted)).toEqual([true, true, true]);
    expect(root.querySelector("dialog")).toBeNull();
  });

  it("says when the day has no known value instead of an empty chart", async () => {
    const root = dialogRoot();
    openCurve(root, REQ, async () => ({ ...RESP, series: [series("home.power", [null, null])] }));
    await vi.waitFor(() => expect(root.querySelector(".sv-curve-status")?.textContent).toBe("Keine Daten"));
    expect(root.querySelector(".sv-curve svg")).toBeNull();
  });

  it("makes home tappable in the card although it has no sensor, and loads its curve from the hub", async () => {
    expect(CURVE_NODES).toEqual(["home"]);
    const fetcher = vi.fn(async (input: RequestInfo | URL) =>
      String(input).endsWith("card/v1/history/home") ? Response.json(RESP) : cardFetch(() => testPlant("b1"))(input));
    vi.stubGlobal("fetch", fetcher);
    const root = dialogRoot();
    const hass = { callWS: vi.fn(), language: "de", themes: { darkMode: true } };
    const card = create(root, { apiBase: "", build: "b1", hass, config: { type: "custom:solar-visor-card" },
                                reload: vi.fn(), renewSession: async () => undefined });
    await vi.waitFor(() => expect(root.querySelector("svg.sv-flow")).not.toBeNull());
    const home = [...root.querySelectorAll('[role="button"]')].find((b) => b.getAttribute("aria-label")?.startsWith("Zuhause"));
    expect(home).toBeDefined();
    home?.dispatchEvent(new MouseEvent("click"));
    await vi.waitFor(() => expect(root.querySelector("dialog .sv-curve svg")).not.toBeNull());
    expect(root.querySelector("dialog h3")?.textContent).toBe("Zuhause · heute");
    expect(fetcher.mock.calls.some((c) => String(c[0]) === "card/v1/history/home")).toBe(true);
    expect(root.querySelector("dialog")?.classList.contains("light")).toBe(false);
    card.update({ ...hass, themes: { darkMode: false } }, { type: "custom:solar-visor-card" });   // HA switches
    expect(root.querySelector("dialog")?.classList.contains("light")).toBe(true);
    card.destroy();
  });
});
