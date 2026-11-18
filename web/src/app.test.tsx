import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { DevicesSection } from "./DevicesSection";
import { PointsSection } from "./PointsSection";
import { type Addresses, get, send, WRITE_HEADER } from "./api";
import { DICTIONARIES, format, I18nProvider, initialLanguage } from "./i18n";
import css from "./styles.css?raw";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("styles", () => {
  it("keeps the page in its own layer (HA iOS app: empty page after the Ingress fade-in otherwise)", () => {
    const app = css.match(/^\.app \{([^}]*)\}/m)?.[1] ?? "";
    expect(app).toMatch(/will-change:\s*opacity;/);
    expect(app).not.toMatch(/will-change:\s*transform/);      // would trap the fixed dialog backdrop in the page
  });
});

describe("i18n", () => {
  it("has every text in German and English", () => {
    expect(Object.keys(DICTIONARIES.en).sort()).toEqual(Object.keys(DICTIONARIES.de).sort());
    for (const lang of ["de", "en"] as const) {
      for (const [key, text] of Object.entries(DICTIONARIES[lang])) expect(text, `${lang}:${key}`).not.toBe("");
    }
  });

  it("follows the browser until a language is chosen", () => {
    expect(initialLanguage(null, "de-DE")).toBe("de");
    expect(initialLanguage(null, "fr-FR")).toBe("en");
    expect(initialLanguage("de", "en-US")).toBe("de");
    expect(format("Revision {n}", { n: 3 })).toBe("Revision 3");
  });
});

describe("api", () => {
  it("reads relative to the page and writes with the CSRF header and JSON", async () => {
    const fetcher = vi.fn(async () => new Response('{"ok": true}', { status: 200 }));
    await get("session", fetcher as unknown as typeof fetch);
    await send("PUT", "addresses", { read_url: null }, fetcher as unknown as typeof fetch);
    const calls = fetcher.mock.calls as unknown as [string, RequestInit][];
    expect(calls[0]![0]).toBe("api/session");
    const [url, init] = calls[1]!;
    expect(url).toBe("api/addresses");
    const headers = init.headers as Record<string, string>;
    expect(headers[WRITE_HEADER]).toBe("1");
    expect(headers["Content-Type"]).toBe("application/json");
  });

  it("turns an error answer into an ApiError with the hub's detail", async () => {
    const fetcher = vi.fn(async () => new Response('{"detail": "admin only"}', { status: 403 }));
    await expect(get("session", fetcher as unknown as typeof fetch)).rejects.toMatchObject({ status: 403, message: "admin only" });
  });
});

describe("app", () => {
  function stubHub(addresses: Addresses = { read_url: null, admin_url: null, suggested_read_url: "http://ha.local:8080",
                                 suggested_admin_url: "http://ha.local:8081",
                                 suggested_ha_url: "https://ha.example.org/api/solar_visor",
                                 bridge_seen: false }) {
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "api/session")
        return Response.json({ user: "Ada", mode: "setup", version: "0.1.0.1", running_revision: null, stored_revision: null,
                               startup_error: null });
      if (url === "api/plant")
        return Response.json({ mode: "setup", active: null, active_revision: 0, running_revision: null, draft: null,
                               problems: [], has_previous: false, startup_error: null });
      if (url === "api/addresses" && init?.method === "PUT") return Response.json({ ...addresses, ...JSON.parse(String(init.body)) });
      if (url === "api/addresses") return Response.json(addresses);
      return new Response("", { status: 404 });
    });
    vi.stubGlobal("fetch", fetcher);
    return fetcher;
  }

  it("shows the session and saves the addresses", async () => {
    const fetcher = stubHub();
    render(
      <I18nProvider language="en">
        <App />
      </I18nProvider>,
    );
    expect(await screen.findByText("Ada")).toBeTruthy();
    expect(screen.getByText("Setup")).toBeTruthy();
    expect(screen.getByText(/No plant set up yet/)).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: "Devices & diagnostics" }));
    fireEvent(window, new HashChangeEvent("hashchange"));
    await screen.findByText("Suggestion: http://ha.local:8080");
    const card = screen.getByRole("region", { name: "Addresses for the apps" });
    fireEvent.change(within(card).getByLabelText(/Read address/), { target: { value: "https://solar.example.org" } });
    fireEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Saved"));
    const put = (fetcher.mock.calls as unknown as [string, RequestInit][]).find(([, i]) => i?.method === "PUT")!;
    expect(JSON.parse(String(put[1].body))).toEqual({ read_url: "https://solar.example.org", admin_url: null });
  });

  it("takes over the address over Home Assistant for both fields and hints at a missing integration", async () => {
    const fetcher = stubHub();
    location.hash = "#devices";
    render(
      <I18nProvider language="en">
        <App />
      </I18nProvider>,
    );
    const card = await screen.findByRole("region", { name: "Addresses for the apps" });
    await within(card).findByRole("button", { name: "Use the address over Home Assistant" });
    expect(within(card).getByRole("note").textContent).toMatch(/integration has not reported/);
    fireEvent.click(within(card).getByRole("button", { name: "Use the address over Home Assistant" }));
    fireEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Saved"));
    const put = (fetcher.mock.calls as unknown as [string, RequestInit][]).find(([, i]) => i?.method === "PUT")!;
    expect(JSON.parse(String(put[1].body))).toEqual({ read_url: "https://ha.example.org/api/solar_visor",
                                                     admin_url: "https://ha.example.org/api/solar_visor" });
  });

  it("asks for the address by hand when Home Assistant knows no external one", async () => {
    stubHub({ read_url: null, admin_url: null, suggested_read_url: null, suggested_admin_url: null,
              suggested_ha_url: null, bridge_seen: true });
    location.hash = "#devices";
    render(
      <I18nProvider language="en">
        <App />
      </I18nProvider>,
    );
    const card = await screen.findByRole("region", { name: "Addresses for the apps" });
    await within(card).findByText(/knows no external address/);
    expect(within(card).queryByRole("button", { name: "Use the address over Home Assistant" })).toBeNull();
    expect(within(card).queryByRole("note")).toBeNull();
  });

  it("shows a failed load of the InfluxDB connection instead of hiding its fields", async () => {
    let fail = true;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "api/session")
        return Response.json({ user: "Ada", mode: "plant_legacy", version: "0.1.0.1", running_revision: 1,
                               stored_revision: 1, startup_error: null });
      if (url === "api/influx")
        return fail ? new Response('{"detail": "boom"}', { status: 500 })
                    : Response.json({ url: "http://i:8086", database: "db", retention_policy: null, token_set: false });
      if (url === "api/secrets") return Response.json({ influx_token: false });
      return new Response("", { status: 503 });
    }));
    render(
      <I18nProvider language="en">
        <DevicesSection />
      </I18nProvider>,
    );
    const card = await screen.findByRole("region", { name: "InfluxDB" });
    expect(within(card).queryByLabelText("Database")).toBeNull();
    fail = false;
    fireEvent.click(within(card).getByRole("button", { name: "Reload" }));
    expect(((await within(card).findByLabelText("Database")) as HTMLInputElement).value).toBe("db");
  });

  it("sets the InfluxDB connection in the interface, not in the add-on options", async () => {
    let influx = { url: "http://192.168.1.20:8086", database: "solar", retention_policy: "autogen" as string | null,
                   token_set: true };
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "api/session")
        return Response.json({ user: "Ada", mode: "plant_legacy", version: "0.1.0.1", running_revision: 1,
                               stored_revision: 1, startup_error: null });
      if (url === "api/influx" && init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        influx = { ...influx, ...body, retention_policy: body.retention_policy || null };
        return Response.json(influx);
      }
      if (url === "api/influx") return Response.json(influx);
      if (url === "api/secrets") return Response.json({ influx_token: true });
      return new Response("", { status: 503 });
    });
    vi.stubGlobal("fetch", fetcher);
    render(
      <I18nProvider language="en">
        <DevicesSection />
      </I18nProvider>,
    );
    const card = await screen.findByRole("region", { name: "InfluxDB" });
    const db = await within(card).findByLabelText("Database");
    expect((db as HTMLInputElement).value).toBe("solar");
    expect(within(card).getByText(/token stored/)).toBeTruthy();
    fireEvent.change(db, { target: { value: "homeassistant" } });
    fireEvent.change(within(card).getByLabelText(/Retention policy/), { target: { value: "" } });
    fireEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(card).getByRole("status").textContent).toBe("Saved"));
    const put = (fetcher.mock.calls as unknown as [string, RequestInit][]).find(([u, i]) => u === "api/influx" && i?.method === "PUT")!;
    expect(JSON.parse(String(put[1].body))).toEqual({ url: "http://192.168.1.20:8086", database: "homeassistant", retention_policy: "" });
  });
});

describe("data points", () => {
  const view = { mode: "plant", active: { modules: { grid: { name: "" }, battery: { name: "S10" } } }, active_revision: 1,
                 running_revision: 1, draft: null, problems: [], has_previous: false, startup_error: null };
  const sensor = (id: string) => ({ entity_id: id, name: id, state: "1", unit: "W", device_class: null, state_class: null });
  const points = {
    "grid.power": { status: "info", codes: ["signed_balance_only"], value: 2850, unit: "W", sensor: sensor("sensor.grid") },
    "grid.import_energy": { status: "ok", codes: [], value: 4635.9, unit: "kWh", sensor: sensor("sensor.import") },
    "battery.power": { status: "ok", codes: [], value: -120, unit: "W", sensor: sensor("sensor.battery") },
  };
  const stub = (narrow: boolean) => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: narrow, media: q, addEventListener() {}, removeEventListener() {} }));
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      url === "api/plant" ? Response.json(view) : url === "api/check" ? Response.json({ points }) : new Response("", { status: 503 })));
  };

  it("lists the points grouped by module on a narrow screen (phone, HA companion app)", async () => {
    stub(true);
    render(<I18nProvider language="en"><PointsSection /></I18nProvider>);
    const grid = await screen.findByRole("region", { name: "Grid" });
    expect(within(grid).getAllByRole("listitem").filter((li) => li.className === "point-row")).toHaveLength(2);
    expect(within(grid).getByText("2.85 kW")).toBeTruthy();
    expect(within(grid).getAllByRole("link", { name: "Edit" })).toHaveLength(1);      // once per module, not per point
    expect(screen.getByRole("region", { name: "S10" })).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("keeps the table on a wide screen", async () => {
    stub(false);
    render(<I18nProvider language="en"><PointsSection /></I18nProvider>);
    expect(await screen.findByRole("table")).toBeTruthy();
    expect(screen.getAllByRole("row")).toHaveLength(4);
  });
});
