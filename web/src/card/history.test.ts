// History dialog (plan lovelace-card): Home Assistant's card events stay inside it – a dashboard that hears
// `ll-upgrade` lays its cards out anew and detaches this card (seen on the own Home Assistant, 2026-11-16).
import { afterEach, describe, expect, it, vi } from "vitest";
import { openHistory } from "./history";

afterEach(() => {
  delete window.loadCardHelpers;
  document.body.replaceChildren();
});

describe("history dialog", () => {
  it("keeps ll-upgrade inside and rebuilds the graph on ll-rebuild", async () => {
    const created: HTMLElement[] = [];
    window.loadCardHelpers = async () => ({
      createCardElement: () => {
        const el = document.createElement("div");
        created.push(el);
        return el;
      },
    });
    HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
      this.setAttribute("open", "");
    };
    const host = document.createElement("div");
    document.body.append(host);
    const root = host.attachShadow({ mode: "open" });
    const heard = vi.fn();
    document.addEventListener("ll-upgrade", heard);
    document.addEventListener("ll-rebuild", heard);
    const hass = { callWS: vi.fn() };
    const update = await openHistory(root, hass, { title: "Batterie", entities: [{ entity: "sensor.b" }], hours: 24,
                                                   note: "n", close: "x" }, () => true);
    expect(root.querySelector("dialog")).not.toBeNull();
    created[0]?.dispatchEvent(new Event("ll-upgrade", { bubbles: true, composed: true }));
    created[0]?.dispatchEvent(new Event("ll-rebuild", { bubbles: true, composed: true }));
    expect(heard).not.toHaveBeenCalled();
    expect(created).toHaveLength(2);
    expect(root.querySelector("dialog")?.contains(created[1] as Node)).toBe(true);
    update?.({ callWS: vi.fn() });
    expect((created[1] as unknown as { hass: unknown }).hass).not.toBe(hass);
  });

  it("opens nothing for a card that was replaced meanwhile", async () => {
    window.loadCardHelpers = async () => ({ createCardElement: () => document.createElement("div") });
    const host = document.createElement("div");
    document.body.append(host);
    const root = host.attachShadow({ mode: "open" });
    await openHistory(root, { callWS: vi.fn() }, { title: "t", entities: [], hours: 24, note: "", close: "" }, () => false);
    expect(root.querySelector("dialog")).toBeNull();
  });
});
