// Card loader and API client (plan lovelace-card P0/P1): slug discovery, session renewal, 304 as a successful check.
import { describe, expect, it, vi } from "vitest";
import { CardApi, NotConfigured } from "./api";
import { findSlug } from "./loader";

function reply(status: number, body: unknown = {}, headers: Record<string, string> = {}): Response {
  return new Response(status === 304 ? null : JSON.stringify(body), { status, headers });
}

describe("card loader", () => {
  it("finds the Solar Visor app among the panels, or takes the configured one", () => {
    const hass = {
      callWS: vi.fn(),
      panels: {
        lovelace: { component_name: "lovelace" },
        a0d7b954_vscode: { component_name: "app", config: { addon: "a0d7b954_vscode" } },
        local_solar_visor: { component_name: "app", config: { addon: "local_solar_visor" } },
      },
    };
    expect(findSlug(hass, { type: "custom:solar-visor-card" })).toBe("local_solar_visor");
    expect(findSlug(hass, { type: "custom:solar-visor-card", addon: "abc123_solar_visor" })).toBe("abc123_solar_visor");
    expect(findSlug({ callWS: vi.fn(), panels: {} }, { type: "custom:solar-visor-card" })).toBeNull();
  });
});

describe("card API", () => {
  it("renews a refused Ingress session once and repeats the request", async () => {
    const renewSession = vi.fn(async () => undefined);
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(reply(401))
      .mockResolvedValueOnce(reply(200, { build: "b", capabilities: {}, sensors: {} }));
    const api = new CardApi({ apiBase: "/api/hassio_ingress/t/", renewSession }, fetcher);
    expect((await api.plant()).build).toBe("b");
    expect(renewSession).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0]?.[0]).toBe("/api/hassio_ingress/t/card/v1/plant");
  });

  it("takes an unchanged snapshot (304) as the last one and sends its ETag", async () => {
    const snap = { snapshot_id: "s1", plant_revision: 4 };
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(reply(200, snap, { ETag: 'W/"abc"' }))
      .mockResolvedValueOnce(reply(304, null, { ETag: 'W/"abc"' }));
    const api = new CardApi({ apiBase: "/x/", renewSession: async () => undefined }, fetcher);
    expect((await api.snapshot()).snapshot_id).toBe("s1");
    expect((await api.snapshot()).snapshot_id).toBe("s1");
    const headers = fetcher.mock.calls[1]?.[1]?.headers as Record<string, string>;
    expect(headers["If-None-Match"]).toBe('W/"abc"');
  });

  it("tells a plant that is not configured apart from other errors", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(reply(503, { detail: "plant_not_configured" }));
    const api = new CardApi({ apiBase: "/x/", renewSession: async () => undefined }, fetcher);
    await expect(api.plant()).rejects.toBeInstanceOf(NotConfigured);
    fetcher.mockResolvedValue(reply(502));
    await expect(api.plant()).rejects.not.toBeInstanceOf(NotConfigured);
  });
});
