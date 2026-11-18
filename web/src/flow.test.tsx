// Overview „Jetzt“ (user decision 2026-11-17): the energy flow instead of the table, a click on a node shows its
// values, one status line below – on a synthetic plant.
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cardFetch, FEED_IN_CAPABILITIES, feedInSnapshot, testSnapshot, TEST_CAPABILITIES } from "./card/testPlant";
import { Topology } from "./card/topology";
import { metricLabel, nodeMetrics, nodeValue } from "./FlowDashboard";
import { I18nProvider } from "./i18n";
import { format } from "./i18n";
import { LiveCard, problemLine, sinceText } from "./LiveCard";
import { de, en, type TextKey } from "./texts";
import { currentTheme, haDarkMode } from "./theme";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("node values", () => {
  it("lists a node's own metrics – power first, strings last – and generation per generator", () => {
    const topology = new Topology(TEST_CAPABILITIES);
    const snap = testSnapshot();
    expect(nodeMetrics("pv_1", snap, topology)).toEqual(["pv_1.power", "pv_1.string_1.power", "pv_1.string_2.power"]);
    expect(nodeMetrics("battery", snap, topology)).toEqual(["battery.power", "battery.soc"]);
    expect(nodeMetrics("generation", snap, topology)).toEqual(["generation.power", "pv_1.power"]);
    expect(nodeMetrics("home", snap, topology)).toEqual(["home.power"]);
    expect(metricLabel("pv_1.power", "pv_1", topology)).toBe("Leistung");
    expect(metricLabel("pv_1.power", "generation", topology)).toBe("Dach · Leistung");
    expect(nodeValue(-1200, "W", "de", ["ja", "nein"])).toBe("-1,20 kW");
    expect(nodeValue(0.2, "A", "de", ["ja", "nein"])).toBe("0,2 A");
    expect(nodeValue(0.2, "A", "en", ["yes", "no"])).toBe("0.2 A");
    expect(nodeValue("not_connected", "text", "de", ["ja", "nein"])).toBe("not_connected");
    expect(nodeValue(false, "bool", "de", ["ja", "nein"])).toBe("nein");
    // a generator with full feed-in is no part of generation: its own node only
    const feedIn = new Topology(FEED_IN_CAPABILITIES);
    expect(nodeMetrics("generation", feedInSnapshot(), feedIn)).toEqual(["generation.power", "pv_1.power"]);
    expect(nodeMetrics("pv_2", feedInSnapshot(), feedIn)).toEqual(["pv_2.power"]);
    const en = new Topology(TEST_CAPABILITIES, "en");               // every point in English, none in German
    for (const point of ["ac_power", "import_energy", "capacity", "voltage", "charging"]) {
      expect(en.metricName(`grid.${point}`)).not.toMatch(/[äöüß]|Leistung|Bezug|Kapazität|Spannung|Laden/);
    }
  });
});

describe("overview „Jetzt“", () => {
  function stub() {
    const card = cardFetch();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "api/overview") {
        return Response.json({ mode: "plant", ha_connected: true, timezone: "Europe/Berlin", balance_residual_w: 35,
                               not_ok: ["pv_1.string_2.power"],
                               live: [{ metric_id: "grid.power", name: null, value: -1200, unit: "W", state: "ok", reasons: [] }] });
      }
      return card(input);
    }));
  }

  it("shows the energy flow and the status line instead of the table of values", async () => {
    stub();
    const { container } = render(<I18nProvider language="de"><LiveCard /></I18nProvider>);
    const host = container.querySelector(".flow-host")!;
    await waitFor(() => expect(host.shadowRoot?.querySelector("svg")).not.toBeNull());
    expect(await screen.findByText("verbunden")).toBeTruthy();
    expect(screen.getByText(/Zeitzone: Europe\/Berlin/)).toBeTruthy();
    expect(screen.getByText(/Bilanzrest Haus: 35 W/)).toBeTruthy();
    expect(screen.getByText(/Ohne gültigen Wert: pv_1.string_2.power/)).toBeTruthy();
    expect(screen.queryByText("-1.20 kW")).toBeNull();                 // no table of live values any more
    expect(screen.getByText(/Einen Knoten antippen/)).toBeTruthy();
  });

  it("shows a node's values on a click and hides them on a second one", async () => {
    stub();
    const { container } = render(<I18nProvider language="de"><LiveCard /></I18nProvider>);
    const root = container.querySelector(".flow-host")!.shadowRoot!;
    await waitFor(() => expect(root.querySelector("svg")).not.toBeNull());
    const pv = [...root.querySelectorAll('[role="button"]')].find((b) => b.getAttribute("aria-label")?.startsWith("Dach"))!;
    expect(pv.getAttribute("aria-label")).toContain("zeigt die Messwerte");
    act(() => void pv.dispatchEvent(new MouseEvent("click")));
    expect(await screen.findByText("String Ost · String 1 · Leistung")).toBeTruthy();
    expect(screen.getByText(/1,80 kW/)).toBeTruthy();
    expect(screen.getByText("unbekannt")).toBeTruthy();                 // string 2 without a value
    act(() => void pv.dispatchEvent(new MouseEvent("click")));
    await waitFor(() => expect(screen.queryByText("String Ost · String 1 · Leistung")).toBeNull());
  });
});

describe("values without a valid state (user decision 2026-11-17)", () => {
  const tDe = (key: TextKey, params?: Record<string, string | number>) => format(de[key], params);
  const tEn = (key: TextKey, params?: Record<string, string | number>) => format(en[key], params);
  const now = new Date("2026-11-17T20:50:00Z");

  it("names the value, the reason and since when – in the plant's time zone, with the date on another day", () => {
    const stalled = { metric_id: "grid.import_energy", name: "Netz", state: "stale", reasons: ["counter_stalled"],
                      since: "2026-11-17T10:23:33+00:00" };
    expect(problemLine(stalled, "Europe/Berlin", "de", tDe, now)).toBe(
      "Netz · Zähler Bezug: veraltet – Zähler läuft nicht weiter, obwohl laut Netzleistung Strom fließt · seit 11:23");
    expect(problemLine({ ...stalled, reasons: ["unavailable"], state: "unknown" }, "Europe/Berlin", "en", tEn, now)).toBe(
      "Netz · Import meter: unknown – unavailable in Home Assistant · since 11:23");
    expect(sinceText("2026-11-16T22:30:00Z", "Europe/Berlin", "de", now)).toBe("16.11. 23:30");
    expect(problemLine({ metric_id: "pv_1.power", name: null, state: "unknown", reasons: ["unit_mismatch:kWh->W", "new_code"],
                         since: null }, null, "de", tDe, now)).toBe(
      "PV-Erzeuger 1 · Leistung: unbekannt – Einheit passt nicht (kWh->W) · new_code");   // no name: the module's text
  });

  it("lists them below the flow", async () => {
    const card = cardFetch();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "api/overview") {
        return Response.json({ mode: "plant_legacy", ha_connected: true, timezone: "Europe/Berlin", balance_residual_w: 35,
                               not_ok: ["grid.import_energy"], live: [],
                               problems: [{ metric_id: "grid.import_energy", name: "Netz", state: "stale",
                                            reasons: ["counter_stalled"], since: new Date().toISOString() }] });
      }
      return card(input);
    }));
    render(<I18nProvider language="de"><LiveCard /></I18nProvider>);
    expect(await screen.findByText(/^Netz · Zähler Bezug: veraltet – Zähler läuft nicht weiter.* · seit \d\d:\d\d$/)).toBeTruthy();
    expect(screen.queryByText(/Ohne gültigen Wert: grid/)).toBeNull();
  });
});

describe("theme of the interface", () => {
  const framed = (darkMode: unknown) => {
    const parent = { document: { querySelector: () => ({ hass: { themes: { darkMode } } }) } };
    return { parent } as unknown as Window;
  };

  it("reads Home Assistant's display setting from the surrounding page", () => {
    expect(haDarkMode(framed(true))).toBe(true);
    expect(haDarkMode(framed(false))).toBe(false);
    expect(currentTheme(framed(true))).toBe("dark");
    expect(currentTheme(framed(false))).toBe("light");
    expect(haDarkMode(framed(undefined))).toBeNull();
  });

  it("falls back to the system setting on its own or in a foreign frame", () => {
    const alone = {} as Window & { parent?: Window };
    alone.parent = alone;
    expect(haDarkMode(alone)).toBeNull();
    const foreign = { get parent(): Window { throw new DOMException("cross-origin", "SecurityError"); } } as unknown as Window;
    expect(haDarkMode(foreign)).toBeNull();
  });
});
