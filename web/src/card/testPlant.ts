// A small synthetic plant for tests (no data of a real installation): grid, one PV generator with two strings, a
// battery; the snapshot the hub would send for it.
import type { CardPlant } from "./api";
import type { PlantCapabilities, PlantMetric, PlantSnapshot } from "./model";

const point = (id: string, unit: string, role = "authoritative") => ({ id, unit, role });

export const TEST_CAPABILITIES: PlantCapabilities = {
  api_version: "2", hub_version: "0.0.0", plant_revision: 3, timezone: "Europe/Berlin",
  modules: [
    { id: "grid", kind: "grid", name: "Netz", balance: null, points: [point("power", "W")], strings: [] },
    { id: "pv_1", kind: "pv", name: "Dach", balance: null, points: [point("power", "W")],
      strings: [{ id: "string_1", name: "Ost", points: [point("power", "W", "detail")] },
                { id: "string_2", name: "West", points: [point("power", "W", "detail")] }] },
    { id: "battery", kind: "battery", name: "Speicher", balance: null,
      points: [point("power", "W"), point("soc", "%", "detail")], strings: [] },
  ],
  metrics: ["grid.power", "pv_1.power", "pv_1.string_1.power", "pv_1.string_2.power", "battery.power", "battery.soc",
            "generation.power", "consumption.power", "home.power"],
  features: {},
};

function metric(id: string, value: number | null, unit = "W", state: PlantMetric["state"] = "ok"): PlantMetric {
  return { metric_id: id, module: id.split(".")[0]!, value, unit, role: "authoritative", state, reasons: [],
           derived: ["generation", "consumption", "home"].includes(id.split(".")[0]!), observed_at: "2026-11-17T10:00:00Z" };
}

export function testSnapshot(revision = 3): PlantSnapshot {
  const metrics = [
    metric("grid.power", -1200), metric("pv_1.power", 3000), metric("pv_1.string_1.power", 1800),
    metric("pv_1.string_2.power", null, "W", "unknown"), metric("battery.power", -400), metric("battery.soc", 64, "%"),
    metric("generation.power", 3000), metric("consumption.power", 1400), metric("home.power", 1400),
  ];
  return {
    snapshot_id: `s${revision}`, generated_at: "2026-11-17T10:00:00Z", plant_revision: revision, connected: true,
    metrics: Object.fromEntries(metrics.map((m) => [m.metric_id, m])), edges: [], balance_residual_w: 0,
    balance_comparable: true, modules: {},
  };
}

/** The second variant: a second PV generator with full feed-in (its own meter, edge `pv_2 → grid`), at noon above
 *  the home's generator – the home's numbers (generation, consumption, grid) do not contain it. */
export const FEED_IN_CAPABILITIES: PlantCapabilities = {
  ...TEST_CAPABILITIES,
  modules: [...TEST_CAPABILITIES.modules,
            { id: "pv_2", kind: "pv", name: "Scheune", balance: "full_feed_in", points: [point("power", "W")], strings: [] }],
  metrics: [...TEST_CAPABILITIES.metrics, "pv_2.power"],
};

export function feedInSnapshot(pv2: { value: number | null; state: PlantMetric["state"] } = { value: 4800, state: "ok" }):
    PlantSnapshot {
  const base = testSnapshot();
  const edge = (id: string, from: string, to: string, power: number | null, state: PlantMetric["state"] = "ok") =>
    ({ edge_id: id, from_node: from, to_node: to, power_w: power, state, derived: false });
  return {
    ...base,
    metrics: { ...base.metrics, "pv_2.power": metric("pv_2.power", pv2.value, "W", pv2.state) },
    edges: [edge("pv_1", "pv_1", "generation", 3000), edge("pv_2", "pv_2", "grid", pv2.value, pv2.state),
            edge("battery", "bus", "battery", -400), edge("grid", "grid", "bus", -1200),
            edge("consumption", "bus", "consumption", 1400), edge("home", "consumption", "home", 1400)],
  };
}

export function testPlant(build = "b1", revision = 3): CardPlant {
  return { build, capabilities: { ...TEST_CAPABILITIES, plant_revision: revision },
           sensors: { pv_1: [{ entity_id: "sensor.pv", metric_id: "pv_1.power" }] } };
}

/** fetch of the hub's card routes for this plant; other paths answer 404. */
export function cardFetch(plant: () => CardPlant = () => testPlant(), snapshot: () => PlantSnapshot = () => testSnapshot()) {
  return async (input: RequestInfo | URL): Promise<Response> => {
    const url = String(input);
    if (url.endsWith("card/v1/plant")) return Response.json(plant());
    if (url.endsWith("card/v1/snapshot")) return Response.json(snapshot());
    return new Response("", { status: 404 });
  };
}
