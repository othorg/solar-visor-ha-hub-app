// Overview „Jetzt“ (user decision 2026-11-17): the energy flow instead of the table, a click on a node shows its
// values, one status line below – on a synthetic plant.
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cardFetch, FEED_IN_CAPABILITIES, feedInSnapshot, testSnapshot, TEST_CAPABILITIES } from "./card/testPlant";
import { Topology } from "./card/topology";
import { metricLabel, nodeMetrics, nodeValue } from "./FlowDashboard";
import { I18nProvider } from "./i18n";
import { LiveCard } from "./LiveCard";
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
