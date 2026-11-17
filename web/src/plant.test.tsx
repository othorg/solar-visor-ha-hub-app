import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import codes from "../../contracts/ui_codes.json";
import { DICTIONARIES } from "./i18n";
import { I18nProvider } from "./i18n";
import {
  addString,
  applySuggestions,
  asPlant,
  emptyPlant,
  isoToLocal,
  localToIso,
  removeString,
  setPointExtra,
  parsePvPeak,
  setPvPeak,
  setRscp,
  setStringName,
  setStringPointEntity,
  formatValue,
  metricOfPath,
  otherVariantPoints,
  requiredPoints,
  setModuleField,
  setPointEntity,
  setPointField,
  hasFullFeedIn,
  setFullFeedIn,
  type Catalog,
  type ModuleSpec,
  type RawPlant,
} from "./plant";
import { PlantSection, RESTART_WAIT } from "./PlantSection";
import App from "./App";

const power = { unit: "W", roles: ["authoritative"], counter: false, nonnegative: false };
const counter = { unit: "Wh", roles: ["cross_check"], counter: true, nonnegative: false };
const GRID: ModuleSpec = {
  id: "grid", kind: "grid", always_on: true, strings: false,
  required: { signed: ["power"], split: ["import_power", "export_power"] },
  points: { power, import_power: power, export_power: power, import_energy: counter },
};
const PV1: ModuleSpec = { id: "pv_1", kind: "pv", always_on: true, strings: true, required: { "": ["power"] },
                          points: { power, energy_total: counter } };
const PV2: ModuleSpec = { ...PV1, id: "pv_2", always_on: false };
const CATALOG: Catalog = { modules: [GRID, PV1, PV2], string_points: {}, max_strings: 6, max_name: 40,
                           grid_variants: ["signed", "split"], generic_balances: [], pv_balances: ["full_feed_in"],
                           wallbox_trust: [], icons: ["solar", "camper"], default_icons: { pv: "solar" } };

describe("module symbol", () => {
  it("stores a chosen symbol and removes it again with the default", () => {
    const p: RawPlant = { schema: 1, modules: { pv_1: { enabled: true, name: "Dach" } } };
    const chosen = setModuleField(p, "pv_1", "icon", "camper");
    expect(chosen.modules.pv_1!.icon).toBe("camper");
    expect("icon" in setModuleField(chosen, "pv_1", "icon", "").modules.pv_1!).toBe(false);
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("every code the hub reports has a text", () => {
  it("in German and English", () => {
    for (const lang of ["de", "en"] as const) {
      for (const c of codes.problems) expect(DICTIONARIES[lang], `${lang}: problem.${c}`).toHaveProperty(`problem.${c}`);
      for (const c of codes.checks) expect(DICTIONARIES[lang], `${lang}: check.${c}`).toHaveProperty(`check.${c}`);
    }
  });
});

describe("plant helpers", () => {
  it("assign, keep sign and role, remove", () => {
    let p = emptyPlant({ grid: "Netz", pv: "PV" });
    p = setPointEntity(p, "grid", "power", power, "sensor.grid");
    expect(p.modules.grid!.points!.power).toEqual({ entity: "sensor.grid", unit: "W", role: "authoritative", sign: 1,
                                                    valid_from: null });
    p = setPointField(p, "grid", "power", "sign", -1);
    p = setPointEntity(p, "grid", "power", power, "sensor.other");
    expect(p.modules.grid!.points!.power!.sign).toBe(-1);                       // a new sensor keeps the sign
    p = setPointEntity(p, "grid", "power", power, null);
    expect(p.modules.grid!.points!.power).toBeUndefined();
  });

  it("switching the grid variant drops the points of the other one", () => {
    let p = setPointEntity(emptyPlant({ grid: "Netz", pv: "PV" }), "grid", "power", power, "sensor.grid");
    p = setPointEntity(p, "grid", "import_energy", counter, "sensor.meter");
    p = setModuleField(p, "grid", "variant", "split");
    expect(Object.keys(p.modules.grid!.points!)).toEqual(["import_energy"]);
    expect(requiredPoints(GRID, p.modules.grid!)).toEqual(["import_power", "export_power"]);
    expect(otherVariantPoints(GRID, p.modules.grid!)).toEqual(["power"]);
    expect(otherVariantPoints(PV1, {})).toEqual([]);
  });

  it("strings, optional fields and the E3/DC details", () => {
    let p = emptyPlant({ grid: "Netz", pv: "PV" });
    p = addString(addString(p, "pv_1", 2), "pv_1", 2);
    expect(p.modules.pv_1!.strings!.map((s) => s.id)).toEqual(["string_1", "string_2"]);
    expect(addString(p, "pv_1", 2)).toEqual(p);                                    // at most max strings
    p = setStringName(p, "pv_1", "string_2", "Süd");
    p = setStringPointEntity(p, "pv_1", "string_2", "power", { ...power, roles: ["detail"] }, "sensor.s2");
    expect(p.modules.pv_1!.strings![1]).toEqual({ id: "string_2", name: "Süd", points: {
      power: { entity: "sensor.s2", unit: "W", role: "detail", sign: 1, valid_from: null } } });
    p = removeString(removeString(p, "pv_1", "string_1"), "pv_1", "string_2");
    expect(p.modules.pv_1!.strings).toBeUndefined();
    p = setPointEntity(p, "grid", "power", power, "sensor.grid");
    p = setPointExtra(p, "grid", "power", "stale_after_s", 300);
    p = setPointExtra(p, "grid", "power", "nonnegative", true);
    expect(p.modules.grid!.points!.power).toMatchObject({ stale_after_s: 300, nonnegative: true });
    p = setPointExtra(setPointExtra(p, "grid", "power", "stale_after_s", undefined), "grid", "power", "nonnegative", false);
    expect(p.modules.grid!.points!.power).not.toHaveProperty("stale_after_s");
    expect(p.modules.grid!.points!.power).not.toHaveProperty("nonnegative");
    expect(setRscp(p, "pv_1").modules.e3dc_rscp).toEqual({ pv: "pv_1" });
    expect(setRscp(setRscp(p, "pv_1"), null).modules).not.toHaveProperty("e3dc_rscp");
    expect(setPvPeak(p, "9,8").pv_peak_w).toBe(9800);
    expect(setPvPeak(setPvPeak(p, "12.5"), "")).not.toHaveProperty("pv_peak_w");
    expect(setPvPeak(p, "abc")).not.toHaveProperty("pv_peak_w");
    // a typo keeps the stored value (review P5)
    expect(setPvPeak(setPvPeak(p, "9,8"), "23,6 kWp").pv_peak_w).toBe(9800);
    expect(setPvPeak(setPvPeak(p, "9,8"), "1.234,5").pv_peak_w).toBe(9800);
    expect(parsePvPeak("14.5")).toBe(14500);
    expect(parsePvPeak("1.234,5")).toBeNull();
    const iso = localToIso("2026-11-14T08:30")!;
    expect(isoToLocal(iso)).toBe("2026-11-14T08:30");
    expect(localToIso("")).toBeNull();
  });

  it("takes over suggestions without overwriting a choice", () => {
    const BAT: ModuleSpec = { id: "battery", kind: "battery", always_on: false, strings: false, required: { "": ["power"] },
                              points: { power, charge_energy: counter } };
    const catalog = { ...CATALOG, modules: [GRID, PV1, PV2, BAT] };
    let p = setPointEntity(emptyPlant({ grid: "Netz", pv: "PV" }), "grid", "power", power, "sensor.mine");
    p = applySuggestions(p, catalog, {
      "grid.power": [{ entity_id: "sensor.other", source: "same_device" }],
      "grid.import_energy": [{ entity_id: "sensor.import", source: "energy_dashboard" }],
      "pv_1.power": [{ entity_id: "sensor.pv1", source: "same_device" }],
      "pv_2.energy_total": [{ entity_id: "sensor.pv2_total", source: "energy_dashboard" }],   // no power: stays off
      "battery.power": [{ entity_id: "sensor.bat", source: "same_device" }],
      "battery.charge_energy": [{ entity_id: "sensor.bat_in", source: "energy_dashboard" }],
    }, { battery: "Batterie" });
    expect(p.modules.grid!.points!.power!.entity).toBe("sensor.mine");                 // the user's choice stays
    expect(p.modules.grid!.points!.import_energy!.entity).toBe("sensor.import");
    expect(p.modules.pv_1!.points!.power!.entity).toBe("sensor.pv1");
    expect(p.modules.pv_2).toBeUndefined();
    expect(p.modules.battery).toMatchObject({ enabled: true, name: "Batterie" });
    expect(Object.keys(p.modules.battery!.points!).sort()).toEqual(["charge_energy", "power"]);
  });

  it("finds the point of a problem and formats values", () => {
    expect(metricOfPath("modules.pv_2.points.power.role")).toBe("pv_2.power");
    expect(metricOfPath("modules.grid.variant")).toBeNull();
    expect(formatValue(1234.5, "W")).toBe("1.23 kW");
    expect(formatValue(-300, "W")).toBe("-300 W");
    expect(formatValue(null, "W")).toBe("–");
    expect(formatValue(true, "bool", ["an", "aus"])).toBe("an");
  });
});

describe("full feed-in", () => {
  const dashboard = { "grid.import_energy": [{ entity_id: "sensor.meter_in", source: "energy_dashboard" as const }],
                      "grid.export_energy": [{ entity_id: "sensor.meter_out", source: "energy_dashboard" as const }] };

  it("is stored only when set – removing it removes the key", () => {
    const p: RawPlant = { schema: 1, modules: { pv_1: { enabled: true, name: "PV" }, pv_2: { enabled: true, name: "WR2" } } };
    const on = setFullFeedIn(p, "pv_2", true, {}).plant;
    expect(on.modules.pv_2!.balance).toBe("full_feed_in");
    expect(hasFullFeedIn(on, CATALOG)).toBe(true);
    const off = setFullFeedIn(on, "pv_2", false, {}).plant;
    expect("balance" in off.modules.pv_2!).toBe(false);                 // else the edit would look unsaved for ever
    expect(off).toEqual(p);
    expect(hasFullFeedIn(setModuleField(on, "pv_2", "enabled", false), CATALOG)).toBe(false);   // switched off
  });

  it("drops grid counters that came from the energy dashboard, keeps a sensor chosen otherwise", () => {
    let p = setPointEntity(emptyPlant({ grid: "Netz", pv: "PV" }), "grid", "import_energy", counter, "sensor.meter_in");
    p = setPointEntity(p, "grid", "export_energy", counter, "sensor.house_only_out");
    p = setPointEntity(p, "grid", "power", power, "sensor.grid");
    const r = setFullFeedIn(setModuleField(p, "pv_2", "enabled", true), "pv_2", true, dashboard);
    expect(r.dropped).toEqual(["grid.import_energy"]);
    expect(Object.keys(r.plant.modules.grid!.points!).sort()).toEqual(["export_energy", "power"]);
    expect(setFullFeedIn(p, "pv_2", false, dashboard).dropped).toEqual([]);
  });

  it("leaves the grid counters empty when suggestions are taken over", () => {
    const p = setFullFeedIn(setModuleField(emptyPlant({ grid: "Netz", pv: "PV" }), "pv_2", "enabled", true), "pv_2", true, {}).plant;
    const after = applySuggestions(p, CATALOG, { ...dashboard, "grid.power": [{ entity_id: "sensor.grid", source: "energy_dashboard" }] }, {});
    expect(after.modules.grid!.points).toEqual({ power: expect.objectContaining({ entity: "sensor.grid" }) });
    const without = applySuggestions(emptyPlant({ grid: "Netz", pv: "PV" }), CATALOG, dashboard, {});
    expect(without.modules.grid!.points!.import_energy!.entity).toBe("sensor.meter_in");      // no feed-in: as before
    // a marked generator that a suggestion is about to switch on counts already
    const parked = setModuleField(p, "pv_2", "enabled", false);
    const switched = applySuggestions(parked, CATALOG, { ...dashboard, "pv_2.power": [{ entity_id: "sensor.wr2", source: "same_device" }] }, {});
    expect(switched.modules.pv_2!.enabled).toBe(true);
    expect(switched.modules.grid!.points ?? {}).not.toHaveProperty("import_energy");
  });

  function stubPlant(mode: string, plant: RawPlant, problems: unknown[] = []) {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "api/catalog") return Response.json(CATALOG);
      if (url === "api/plant") return Response.json({ mode, active: plant, active_revision: 1, running_revision: 1, draft: null,
                                                       problems: [], has_previous: false, startup_error: null });
      if (url === "api/suggestions") return Response.json({ points: dashboard });
      if (url === "api/check") return Response.json({ points: {} });
      if (url === "api/plant/validate") return Response.json({ problems });
      return new Response("", { status: 404 });
    }));
  }

  const ACTIVE: RawPlant = { schema: 1, revision: 1, modules: {
    grid: { enabled: true, name: "Netz", variant: "signed", points: {
      power: { entity: "sensor.grid", unit: "W", role: "authoritative" },
      import_energy: { entity: "sensor.meter_in", unit: "Wh", role: "cross_check" } } },
    pv_1: { enabled: true, name: "PV", points: { power: { entity: "sensor.pv", unit: "W", role: "authoritative" } } },
    pv_2: { enabled: true, name: "WR2", points: { power: { entity: "sensor.wr2", unit: "W", role: "authoritative" } } } } };

  it("is offered per generator with the hint, drops the dashboard counter and names the new problem", async () => {
    stubPlant("plant", ACTIVE, [{ path: "modules.pv_1.balance", code: "full_feed_in_needs_house_pv", message: "x" }]);
    render(<I18nProvider language="de"><PlantSection /></I18nProvider>);
    const pv2 = await screen.findByRole("region", { name: /WR2/ });
    expect(within(pv2).getAllByText(/Der Netzsensor der Anlage darf diesen Erzeuger nicht messen/)).toHaveLength(1);
    const mark = within(pv2).getByRole("radio", { name: /Volleinspeisung \(eigener Zähler/ }) as HTMLInputElement;
    await waitFor(() => expect(mark.disabled).toBe(false));                         // once the suggestions are read
    expect((within(pv2).getByRole("radio", { name: "Eigenverbrauch (speist ins Haus)" }) as HTMLInputElement).checked).toBe(true);
    const grid = screen.getByRole("region", { name: /Netz/ });
    expect(within(grid).queryByText(/Mit Volleinspeisung/)).toBeNull();
    fireEvent.click(mark);
    expect(await within(pv2).findByText(/Die Netz-Zähler aus dem Energie-Dashboard wurden entfernt/)).toBeTruthy();
    expect(within(grid).getByText(/Mit Volleinspeisung: Zähler Bezug\/Einspeisung nur wählen/)).toBeTruthy();
    expect(within(grid).queryByText("sensor.meter_in")).toBeNull();
    expect(screen.getByText("Ungespeicherte Änderungen")).toBeTruthy();
    expect(await screen.findByText(/Mindestens ein eingeschalteter PV-Erzeuger muss ins Haus speisen/)).toBeTruthy();
  });

  it("is hidden for the own plant and never offered for the E3/DC details", async () => {
    stubPlant("plant_legacy", ACTIVE);
    render(<I18nProvider language="en"><PlantSection /></I18nProvider>);
    const pv2 = await screen.findByRole("region", { name: /WR2/ });
    expect(within(pv2).queryByText("Connection")).toBeNull();
    cleanup();
    stubPlant("plant", setRscp(setFullFeedIn(ACTIVE, "pv_2", true, {}).plant, "pv_1"));
    render(<I18nProvider language="en"><PlantSection /></I18nProvider>);
    const rscp = await screen.findByRole("region", { name: /E3\/DC details/ });
    expect(within(rscp).getAllByRole("option").map((o) => o.textContent)).toEqual(["PV"]);
    cleanup();
    stubPlant("plant", setRscp(setFullFeedIn(ACTIVE, "pv_2", true, {}).plant, "pv_2"));      // marked after the choice
    render(<I18nProvider language="en"><PlantSection /></I18nProvider>);
    const stale = await screen.findByRole("region", { name: /E3\/DC details/ });
    expect((within(stale).getByRole("combobox") as HTMLSelectElement).value).toBe("");      // nothing preselected
    expect((within(await screen.findByRole("region", { name: /WR2/ })).getByRole("radio", { name: /Full feed-in/ }) as HTMLInputElement).checked).toBe(true);
  });
});

describe("plant section", () => {
  function hub(active: unknown = null) {
    const calls: { url: string; method: string; body: unknown }[] = [];
    let revision = active ? 1 : 0;
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      calls.push({ url, method, body });
      if (url === "api/catalog") return Response.json(CATALOG);
      if (url === "api/plant")
        return Response.json({ mode: active ? "plant" : "setup", active, active_revision: revision, running_revision: null, draft: null,
                               problems: [], has_previous: false, startup_error: null });
      if (url === "api/suggestions")
        return Response.json({ points: { "pv_1.power": [{ entity_id: "sensor.pv", source: "same_device" }] } });
      if (url.startsWith("api/entities"))
        return Response.json({ total: 1, entities: [{ entity_id: "sensor.pv", name: "PV Leistung", state: "1.2", unit: "kW",
          device_class: "power", state_class: "measurement", device: "WR", area: null, statistic: "mean", recorder_excluded: null }] });
      if (url === "api/check") {
        const points = body.plant.modules.pv_1?.points?.power
          ? { "pv_1.power": { status: "ok", codes: [], value: 1200, unit: "W", sensor: null } } : {};
        return Response.json({ points });
      }
      if (url === "api/plant/validate") {
        const missing = !body.plant.modules.grid?.points?.power;
        return Response.json({ problems: missing ? [{ path: "modules.grid.points.power", code: "point_missing", message: "x" }] : [] });
      }
      if (url === "api/plant/draft") return Response.json({ draft_id: "d1", problems: [] });
      if (url === "api/plant/activate") {
        revision = 1;
        return Response.json({ revision: 1, restarting: true });
      }
      if (url === "api/session") return Response.json({ running_revision: revision });
      return new Response("", { status: 404 });
    });
    vi.stubGlobal("fetch", fetcher);
    return calls;
  }

  it("guides a first setup: steps, sensor with live value, no activation while a required point is missing", async () => {
    const calls = hub();
    render(<I18nProvider language="en"><PlantSection /></I18nProvider>);
    expect(await screen.findByRole("heading", { name: "First setup" })).toBeTruthy();
    expect(screen.getByRole("region", { name: /Grid/ })).toBeTruthy();
    expect(screen.queryByRole("region", { name: /PV generator 1/ })).toBeNull();      // one step at a time
    fireEvent.click(screen.getByRole("button", { name: "Take over suggestions from the energy dashboard" }));
    expect(await screen.findByText("1 data points taken over – please check them.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    const pv = await screen.findByRole("region", { name: /PV generator 1/ });
    expect(calls.some((c) => c.url === "api/plant/draft")).toBe(true);                // the step was kept as draft
    expect(await within(pv).findByText("1.20 kW")).toBeTruthy();                       // the suggestion, live
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    expect(await screen.findAllByText("This data point is required.")).toHaveLength(1);  // grid power missing
    expect((screen.getByRole("button", { name: "Activate" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("offers no activation while nothing differs from the active plant", async () => {
    const plant = { schema: 1, revision: 1, modules: {
      grid: { enabled: true, name: "Netz", variant: "signed", points: { power: { entity: "sensor.grid", unit: "W", role: "authoritative" } } },
      pv_1: { enabled: true, name: "PV", points: { power: { entity: "sensor.pv", unit: "W", role: "authoritative" } } } } };
    hub(plant);
    render(<I18nProvider language="en"><PlantSection /></I18nProvider>);
    await screen.findByRole("region", { name: /PV generator 1/ });
    const activate = () => screen.getByRole("button", { name: "Activate" }) as HTMLButtonElement;
    expect(activate().disabled).toBe(true);                                       // a restart for nothing
    fireEvent.change(screen.getAllByLabelText("Name")[1]!, { target: { value: "Dach" } });
    expect(activate().disabled).toBe(false);
  });
});

describe("retroactive sensor change", () => {
  it("is offered for a changed sensor and sent with the activation", async () => {
    const calls: { url: string; body: unknown }[] = [];
    const active = { schema: 1, revision: 1, modules: {
      grid: { enabled: true, name: "Netz", variant: "signed", points: { power: { entity: "sensor.grid", unit: "W", role: "authoritative" } } },
      pv_1: { enabled: true, name: "PV", points: { power: { entity: "sensor.old", unit: "W", role: "authoritative" } } } } };
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      calls.push({ url, body });
      if (url === "api/catalog") return Response.json(CATALOG);
      if (url === "api/plant") return Response.json({ mode: "plant", active, active_revision: 1, running_revision: 1, draft: null,
                                                       problems: [], has_previous: false, startup_error: null });
      if (url === "api/suggestions") return Response.json({ points: {} });
      if (url.startsWith("api/entities")) return Response.json({ total: 1, entities: [{ entity_id: "sensor.pv", name: "PV Leistung",
        state: "1", unit: "kW", device_class: "power", state_class: "measurement", device: null, area: null, statistic: "mean",
        recorder_excluded: null }] });
      if (url === "api/check") return Response.json({ points: {} });
      if (url === "api/plant/validate") return Response.json({ problems: [] });
      if (url === "api/plant/draft") return Response.json({ draft_id: "d1", problems: [] });
      if (url === "api/plant/activate") return Response.json({ revision: 2, restarting: true });
      if (url === "api/session") return Response.json({ running_revision: 2 });
      return new Response("", { status: 404 });
    }));
    render(<I18nProvider language="en"><PlantSection /></I18nProvider>);
    const pv = await screen.findByRole("region", { name: /PV generator 1/ });
    fireEvent.click(within(pv).getByRole("button", { name: "Change" }));
    fireEvent.click(await within(await screen.findByRole("dialog")).findByRole("button", { name: /PV Leistung/ }));
    expect(await within(pv).findByText("before: sensor.old")).toBeTruthy();
    fireEvent.click(within(pv).getByRole("radio", { name: /retroactively from/ }));
    const day = (within(pv).getByLabelText("retroactively from", { selector: "input[type=date]" }) as HTMLInputElement).value;
    expect(day).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    fireEvent.click(screen.getByRole("button", { name: "Activate" }));
    await vi.waitFor(() => expect(calls.some((c) => c.url === "api/plant/activate")).toBe(true));
    const activation = calls.find((c) => c.url === "api/plant/activate")!.body as { retroactive: unknown };
    expect(activation.retroactive).toEqual([{ metric_id: "pv_1.power", from: day }]);
  });
});

describe("sensor picker", () => {
  it("opens with the focus in the search field and closes with Escape", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ total: 0, entities: [] })));
    const { SensorPicker } = await import("./SensorPicker");
    const onClose = vi.fn();
    render(<I18nProvider language="en"><SensorPicker title="Sensor" unit="W" suggestions={[]} onPick={() => undefined}
                                                     onClose={onClose} /></I18nProvider>);
    const search = screen.getByRole("searchbox");
    expect(document.activeElement).toBe(search);
    fireEvent.keyDown(search, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("dialog").getAttribute("aria-modal")).toBe("true");
  });
});

describe("review findings of P4", () => {
  const active = { schema: 1, revision: 1, modules: {
    grid: { enabled: true, name: "Netz", variant: "signed", points: { power: { entity: "sensor.grid", unit: "W", role: "authoritative" } } },
    pv_1: { enabled: true, name: "PV", points: { power: { entity: "sensor.pv", unit: "W", role: "authoritative" } } } } };

  function stub(draftPlant: unknown, session: Record<string, unknown> = { running_revision: 1, startup_error: null }) {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "api/catalog") return Response.json(CATALOG);
      if (url === "api/plant") return Response.json({ mode: "plant", active, active_revision: 1, running_revision: 1,
        draft: draftPlant === undefined ? null : { id: "d1", base_revision: 1, saved_at: "2026-11-15T10:00:00Z", plant: draftPlant },
        problems: [], has_previous: true, startup_error: null });
      if (url === "api/suggestions") return Response.json({ points: {} });
      if (url === "api/check") return Response.json({ points: {} });
      if (url === "api/plant/validate") return Response.json({ problems: [] });
      if (url === "api/plant/draft") return Response.json({ draft_id: "d2", problems: [] });
      if (url === "api/plant/activate") return Response.json({ revision: 2, restarting: true });
      if (url === "api/session") return Response.json({ user: "Ada", mode: "plant", version: "x", stored_revision: 2, ...session });
      return new Response("", { status: 404 });
    }));
  }

  it("asPlant accepts only the form of a plant", () => {
    expect(asPlant({ schema: 1 })).toBeNull();
    expect(asPlant({ modules: [] })).toBeNull();
    expect(asPlant({ modules: { grid: null } })).toBeNull();
    expect(asPlant({ modules: {} })).toEqual({ modules: {}, schema: 1 });
  });

  it("a draft that is not a plant does not break the page", async () => {
    stub({ schema: 1 });
    render(<I18nProvider language="en"><PlantSection /></I18nProvider>);
    expect(await screen.findByText(/The saved draft is not a plant/)).toBeTruthy();
    expect(screen.getByRole("region", { name: /PV generator 1/ })).toBeTruthy();       // the active plant is shown
  });

  it("a restart that does not come back is reported, not called active", async () => {
    stub(undefined, { running_revision: 1, startup_error: "Start mit der gespeicherten Anlage fehlgeschlagen (KeyError)" });
    RESTART_WAIT.tries = 2;
    RESTART_WAIT.ms = 1;
    render(<I18nProvider language="en"><PlantSection /></I18nProvider>);
    const pv = await screen.findByRole("region", { name: /PV generator 1/ });
    fireEvent.change(within(pv).getByLabelText("Name"), { target: { value: "Dach" } });
    fireEvent.click(screen.getByRole("button", { name: "Activate" }));
    expect(await screen.findByText(/does not run with it \(Start mit der gespeicherten Anlage fehlgeschlagen/)).toBeTruthy();
    expect(screen.queryByText("Revision 2 is active.")).toBeNull();
    RESTART_WAIT.tries = 40;
    RESTART_WAIT.ms = 750;
  });

  it("switching the language keeps the edit", async () => {
    stub(undefined);
    render(<I18nProvider language="en"><App /></I18nProvider>);
    fireEvent.click(await screen.findByRole("link", { name: "Plant" }));
    fireEvent(window, new HashChangeEvent("hashchange"));
    const pv = await screen.findByRole("region", { name: /PV generator 1/ });
    fireEvent.change(within(pv).getByLabelText("Name"), { target: { value: "Dach" } });
    fireEvent.change(screen.getByLabelText("Language"), { target: { value: "de" } });
    const pvDe = await screen.findByRole("region", { name: /PV-Erzeuger 1/ });
    expect((within(pvDe).getByLabelText("Name") as HTMLInputElement).value).toBe("Dach");
  });
});
