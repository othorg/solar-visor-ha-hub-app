// A node's Home Assistant history (user decision 2026-11-16): Home Assistant's own history-graph card in a dialog –
// never the more-info dialog (it offers controls for number/input_number entities). Reads only.
import type { Hass } from "./contract";

interface CardHelpers {
  createCardElement(config: Record<string, unknown>): HTMLElement & { hass?: Hass };
}

declare global {
  interface Window {
    loadCardHelpers?: () => Promise<CardHelpers>;
  }
}

export interface HistoryRequest {
  title: string;
  entities: { entity: string; name?: string }[];
  hours: number;
  note: string;
  close: string;
}

/** Opens the dialog in `root`; returns a function that updates its `hass` (null when helpers are missing). */
export async function openHistory(root: ShadowRoot, hass: Hass, req: HistoryRequest,
                                  alive: () => boolean): Promise<((h: Hass) => void) | null> {
  if (!window.loadCardHelpers) return null;
  const helpers = await window.loadCardHelpers();
  if (!alive() || !root.host.isConnected) return () => undefined;   // the card was replaced or removed meanwhile
  const config = { type: "history-graph", title: req.title, hours_to_show: req.hours, entities: req.entities };
  let graph = helpers.createCardElement(config);
  let current = hass;
  graph.hass = hass;

  root.querySelector("dialog.sv-history")?.remove();
  const dialog = document.createElement("dialog");
  dialog.className = "sv-history";
  const head = document.createElement("div");
  head.className = "sv-history-head";
  const note = document.createElement("p");
  note.textContent = req.note;
  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "✕";
  close.setAttribute("aria-label", req.close);
  close.addEventListener("click", () => dialog.close());
  head.append(note, close);
  dialog.append(head, graph);
  // Home Assistant's card events stay in the dialog: a lazily loaded graph announces `ll-upgrade`, and a dashboard
  // view that hears it lays all its cards out anew – detaching this card and closing the dialog (seen 2026-11-16).
  // `ll-rebuild` asks for a fresh element, which the dialog does itself.
  dialog.addEventListener("ll-upgrade", (e) => e.stopPropagation());
  dialog.addEventListener("ll-rebuild", (e) => {
    e.stopPropagation();
    const fresh = helpers.createCardElement(config);
    fresh.hass = current;
    graph.replaceWith(fresh);
    graph = fresh;
  });
  dialog.addEventListener("close", () => dialog.remove());
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) dialog.close();                 // a click on the backdrop
  });
  root.append(dialog);
  try {
    dialog.showModal();
  } catch {
    dialog.remove();
    return () => undefined;
  }
  return (h: Hass) => {
    current = h;
    if (dialog.isConnected) graph.hass = h;
  };
}

/** Closes an open history dialog (the renderer is torn down). */
export function closeHistory(root: ShadowRoot): void {
  root.querySelector("dialog.sv-history")?.remove();
}

export const HISTORY_CSS = `
dialog.sv-history { width: min(720px, calc(100vw - 32px)); max-height: calc(100vh - 48px); padding: 12px;
  border: none; border-radius: var(--ha-card-border-radius, 12px); background: var(--card-background-color, #1c1c1c);
  color: var(--primary-text-color, #e1e1e1); box-sizing: border-box; overflow: auto; }
dialog.sv-history::backdrop { background: rgba(0, 0, 0, 0.6); }
.sv-history-head { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 8px; }
.sv-history-head p { flex: 1; margin: 0; font-size: 12px; color: var(--secondary-text-color, #9b9b9b); }
.sv-history-head button { border: none; background: none; color: inherit; font-size: 18px; cursor: pointer;
  padding: 4px 8px; min-width: 44px; min-height: 44px; }
`;
