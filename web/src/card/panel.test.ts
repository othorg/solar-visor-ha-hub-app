// The flow panel shared by the card and the web interface (plan web-overview-dashboard): data, clicks, build change,
// teardown – on a synthetic plant.
import { afterEach, describe, expect, it, vi } from "vitest";
import { CardApi } from "./api";
import { FlowPanel, type CardState } from "./panel";
import { COLORS, nodeColor, sourceColor } from "./palette";
import { create, themeOf } from "./renderer";
import { cardFetch, testPlant } from "./testPlant";
import { textsFor } from "./texts";
import { VIEW_CSS } from "./view";

function mount(opts: Partial<ConstructorParameters<typeof FlowPanel>[1]> = {}, fetcher = cardFetch()) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = host.attachShadow({ mode: "open" });
  const fetchMock = vi.fn(fetcher);
  const updates: CardState[] = [];
  const onNode = vi.fn();
  const panel = new FlowPanel(root, {
    api: new CardApi({ apiBase: "", renewSession: async () => undefined }, fetchMock as unknown as typeof fetch),
    texts: textsFor("de"), onNode, onUpdate: (s) => updates.push(s), ...opts,
  });
  return { root, panel, fetchMock, updates, onNode };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("flow panel", () => {
  it("loads the plant and the snapshot, draws the flow and reports what it shows", async () => {
    const { root, panel, fetchMock, updates } = mount();
    await vi.waitFor(() => expect(updates.at(-1)?.snapshot?.snapshot_id).toBe("s3"));
    expect(fetchMock.mock.calls.map((c) => String(c[0]))).toEqual(["card/v1/plant", "card/v1/snapshot"]);
    expect(updates.at(-1)?.stale).toBe(false);
    expect(root.querySelector("svg")?.textContent).toContain("3,00 kW");
    expect(panel.currentTopology?.name("pv_1")).toBe("Dach");
    panel.destroy();
  });

  it("makes nodes clickable as asked – by default only the ones with Home Assistant sensors", async () => {
    const card = mount();
    await vi.waitFor(() => expect(card.root.querySelector("svg")).not.toBeNull());
    expect(card.root.querySelectorAll('[role="button"]')).toHaveLength(1);
    card.panel.destroy();
    const web = mount({ clickable: () => true });
    await vi.waitFor(() => expect(web.root.querySelector("svg")).not.toBeNull());
    const buttons = [...web.root.querySelectorAll('[role="button"]')];
    expect(buttons.length).toBeGreaterThan(3);
    (buttons[0] as SVGElement).dispatchEvent(new MouseEvent("click"));
    expect(web.onNode).toHaveBeenCalledWith("generation");
    web.panel.destroy();
  });

  it("stops on another hub build only when it was loaded for one", async () => {
    const onBuildChange = vi.fn();
    const card = mount({ build: "old", onBuildChange });
    await vi.waitFor(() => expect(onBuildChange).toHaveBeenCalledTimes(1));
    expect(card.fetchMock).toHaveBeenCalledTimes(1);       // no snapshot after the plant said "new build"
    card.panel.destroy();
    const web = mount({}, cardFetch(() => testPlant("any build")));
    await vi.waitFor(() => expect(web.updates.at(-1)?.snapshot).not.toBeNull());
    web.panel.destroy();
  });

  it("leaves nothing behind and can be torn down twice (React StrictMode mounts twice)", async () => {
    const { root, panel, updates } = mount();
    await vi.waitFor(() => expect(updates.at(-1)?.snapshot).not.toBeNull());
    panel.destroy();
    panel.destroy();
    expect(root.childNodes).toHaveLength(0);
  });
});

describe("light and dark like Home Assistant", () => {
  const inks = (root: ShadowRoot) => new Set([...root.querySelectorAll("svg text")].map((t) => t.getAttribute("fill")));

  it("takes the apps' on-light colours on a light surface", () => {
    expect(nodeColor({ kind: "pv", index: 0 } as never, "light")).toBe("#D39914");
    expect(nodeColor({ kind: "pv", index: 0 } as never)).toBe("#F7B733");          // dark stays the app dashboard
    expect(nodeColor({ kind: "consumption" } as never, "light")).toBe("#6B7480");
    expect(sourceColor("unattributed", "light")).toBe(COLORS.light.unattributed);
  });

  it("draws on Home Assistant's card surface in both modes – black or white only without a theme", () => {
    const rule = (sel: string) => VIEW_CSS.slice(VIEW_CSS.indexOf(`${sel} {`), VIEW_CSS.indexOf("}", VIEW_CSS.indexOf(`${sel} {`)));
    expect(rule(".sv")).toContain("--sv-surface: var(--ha-card-background, var(--card-background-color, #000))");
    expect(rule(".sv.light")).toContain("--sv-surface: var(--ha-card-background, var(--card-background-color, #fff))");
    expect(rule(".sv")).toContain("border: var(--ha-card-border-width, 1px) solid var(--sv-border)");
  });

  it("draws light or dark and switches without a new request", async () => {
    const { root, panel, fetchMock, updates } = mount({ theme: "light" });
    await vi.waitFor(() => expect(updates.at(-1)?.snapshot).not.toBeNull());
    expect(root.querySelector(".sv")?.classList.contains("light")).toBe(true);
    expect(inks(root).has(COLORS.light.ink)).toBe(true);
    expect(inks(root).has(COLORS.dark.ink)).toBe(false);
    const calls = fetchMock.mock.calls.length;
    panel.setTheme("dark");
    expect(root.querySelector(".sv")?.classList.contains("light")).toBe(false);
    expect(inks(root).has(COLORS.dark.ink)).toBe(true);
    expect(fetchMock.mock.calls.length).toBe(calls);
    panel.destroy();
  });

  it("follows Home Assistant's dark mode in the card – dark when it does not say", async () => {
    expect(themeOf({ callWS: vi.fn(), themes: { darkMode: false } })).toBe("light");
    expect(themeOf({ callWS: vi.fn(), themes: { darkMode: true } })).toBe("dark");
    expect(themeOf({ callWS: vi.fn() })).toBe("dark");
    vi.stubGlobal("fetch", vi.fn(cardFetch(() => testPlant("b1"))));
    const host = document.createElement("div");
    document.body.append(host);
    const root = host.attachShadow({ mode: "open" });
    const hass = { callWS: vi.fn(), language: "de", themes: { darkMode: false } };
    const card = create(root, { apiBase: "", build: "b1", hass, config: { type: "custom:solar-visor-card" },
                                reload: vi.fn(), renewSession: async () => undefined });
    expect(root.querySelector(".sv")?.classList.contains("light")).toBe(true);
    card.update({ ...hass, themes: { darkMode: true } }, { type: "custom:solar-visor-card" });
    expect(root.querySelector(".sv")?.classList.contains("light")).toBe(false);
    card.destroy();
    vi.unstubAllGlobals();
  });
});
