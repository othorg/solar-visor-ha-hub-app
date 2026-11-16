// Interface between the card loader (installed once under /config/www) and the renderer the hub serves over Ingress
// (plan lovelace-card). The loader stays put for years – change this only compatibly; a breaking change gets a new
// CONTRACT number and the loader refuses a renderer it does not know.

export const CONTRACT = 1;

/** The parts of Home Assistant's `hass` object the card uses. */
export interface Hass {
  callWS<T>(msg: Record<string, unknown>): Promise<T>;
  language?: string;
  locale?: { language?: string };
  panels?: Record<string, { component_name?: string; url_path?: string; title?: string | null; config?: { addon?: string } | null }>;
  user?: { id?: string; is_admin?: boolean };
  themes?: { darkMode?: boolean };
}

export interface CardConfig {
  type: string;
  /** Slug of the Solar Visor app (`local_solar_visor`, `<repo>_solar_visor`); found in the panels when left out. */
  addon?: string;
  /** Hours of the history graph a click opens (default 24). */
  hours_to_show?: number;
}

export interface RendererContext {
  /** `/api/hassio_ingress/<token>/` – the hub's Ingress address (ends with a slash). */
  apiBase: string;
  /** The hub build the renderer was loaded for; a different build in /card/v1/plant → `reload()`. */
  build: string;
  hass: Hass;
  config: CardConfig;
  /** The loader drops this renderer and loads the hub's current one. */
  reload(): void;
  /** A fresh Ingress session (after a 401/403 of the hub's address). */
  renewSession(): Promise<void>;
}

export interface RendererInstance {
  update(hass: Hass, config: CardConfig): void;
  destroy(): void;
}

export interface RendererModule {
  contract: number;
  create(root: ShadowRoot, ctx: RendererContext): RendererInstance;
}
