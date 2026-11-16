// Solar Visor card for Home Assistant dashboards – the loader (plan lovelace-card). Installed once as a Lovelace
// resource; it defines `custom:solar-visor-card`, opens an Ingress session to the Solar Visor app with the viewer's
// Home Assistant login and loads the renderer the hub serves – so the card is always the hub's own version. Reads only.
import { CONTRACT, type CardConfig, type Hass, type RendererInstance, type RendererModule } from "./contract";

const TAG = "solar-visor-card";
const SESSION_REFRESH_MS = 60_000;      // as Home Assistant's own Ingress panels
const RETRY_MS = 30_000;
const DETACH_GRACE_MS = 2_000;          // a dashboard re-layout detaches and re-attaches cards: keep the renderer
const TIMEOUT_MS = 15_000;

/** A Home Assistant call that cannot hang the card (the WebSocket call itself cannot be cancelled). */
function within<T>(promise: Promise<T>, ms = TIMEOUT_MS): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error("timeout")), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function german(hass: Hass | undefined): boolean {
  return (hass?.locale?.language ?? hass?.language ?? "de").startsWith("de");
}

/** The app's slug: configured, else the panel Home Assistant registered for it. */
export function findSlug(hass: Hass, config: CardConfig): string | null {
  if (config.addon) return config.addon;
  for (const panel of Object.values(hass.panels ?? {})) {
    const slug = panel.config?.addon;
    if ((panel.component_name === "app" || panel.component_name === "hassio") && slug?.endsWith("_solar_visor")) {
      return slug;
    }
  }
  return null;
}

function setCookie(session: string): void {
  const secure = location.protocol === "https:" ? ";Secure" : "";
  document.cookie = `ingress_session=${session};path=/api/hassio_ingress/;SameSite=Strict${secure}`;
}

/** One Ingress session per app and page for the current user, shared by every card on it and kept alive while one is
 *  shown. The cookie is one per browser: when another user appears on the same page, every session of the previous
 *  user is dropped – a late answer for it never writes the cookie again (Codex review). */
class Session {
  private session: string | null = null;
  private pending: Promise<void> | null = null;
  private timer: ReturnType<typeof setInterval> | undefined;
  private users = 0;
  private hass: Hass | null = null;
  private dead = false;

  constructor(private readonly slug: string) {}

  async acquire(hass: Hass): Promise<void> {
    this.hass = hass;
    this.users += 1;
    this.timer ??= setInterval(() => void this.refresh(), SESSION_REFRESH_MS);
    if (this.session) setCookie(this.session);           // ours, whatever another tab or panel left there
    await this.ensure(false);
  }

  release(): void {
    this.users = Math.max(0, this.users - 1);
    if (this.users === 0) this.stopTimer();
  }

  /** The user changed: this session is no longer anybody's. */
  kill(): void {
    this.dead = true;
    this.session = null;
    this.stopTimer();
  }

  update(hass: Hass): void {
    this.hass = hass;
  }

  /** A new session (after the hub's address refused the old one); concurrent callers share one request. */
  renew(): Promise<void> {
    return this.ensure(true);
  }

  private stopTimer(): void {
    if (this.timer !== undefined) clearInterval(this.timer);
    this.timer = undefined;
  }

  private ensure(renew: boolean): Promise<void> {
    if (this.dead) return Promise.reject(new Error("session of another user"));
    if (this.session && !renew) return Promise.resolve();
    this.pending ??= this.create().finally(() => (this.pending = null));
    return this.pending;
  }

  private async create(): Promise<void> {
    const hass = this.hass;
    if (!hass) throw new Error("no hass");
    const { session } = await within(
      hass.callWS<{ session: string }>({ type: "supervisor/ingress/session", slug: this.slug }));
    if (this.dead) throw new Error("session of another user");
    setCookie(session);
    this.session = session;
  }

  private async refresh(): Promise<void> {
    if (!this.hass || !this.session || this.dead) return;
    try {
      await within(this.hass.callWS({ type: "supervisor/ingress/validate_session", session: this.session }));
    } catch {
      if (this.dead) return;
      this.session = null;
      await this.ensure(true).catch(() => undefined);     // the next request retries
    }
  }
}

const sessions = new Map<string, Session>();      // slug → session of `sessionUser`
let sessionUser: string | undefined;

/** Called with every `hass` a card gets: another user drops the previous user's sessions and cookie at once. */
function noteUser(user: string | undefined): void {
  if (user === sessionUser) return;
  for (const old of sessions.values()) old.kill();
  if (sessions.size > 0) {
    // the previous user's cookie must not outlive them, even if the new user's session cannot be opened
    document.cookie = "ingress_session=;path=/api/hassio_ingress/;max-age=0;SameSite=Strict";
  }
  sessions.clear();
  sessionUser = user;
}

function sessionFor(slug: string, user: string | undefined): Session {
  noteUser(user);
  let session = sessions.get(slug);
  if (!session) sessions.set(slug, (session = new Session(slug)));
  return session;
}

class SolarVisorCard extends HTMLElement {
  private hassValue: Hass | undefined;
  private config: CardConfig | undefined;
  private renderer: RendererInstance | null = null;
  private session: Session | null = null;
  private acquired = false;             // this card counts as a user of `session` (released exactly once)
  private abort = new AbortController();
  private run = 0;                      // a start that was overtaken (config change, removal) drops its result
  private retry: ReturnType<typeof setTimeout> | undefined;
  private detached: ReturnType<typeof setTimeout> | undefined;
  private readonly root: ShadowRoot;

  constructor() {
    super();
    this.root = this.attachShadow({ mode: "open" });
  }

  static getStubConfig(): CardConfig {
    return { type: `custom:${TAG}` };
  }

  setConfig(config: CardConfig): void {
    if (!config || typeof config !== "object") throw new Error("invalid configuration");
    const changedApp = this.config !== undefined && this.config.addon !== config.addon;
    this.config = config;
    if (changedApp) this.restart();
    else this.renderer?.update(this.hassValue as Hass, config);
  }

  set hass(hass: Hass) {
    noteUser(hass.user?.id);
    const first = this.hassValue === undefined;
    const otherUser = !first && this.hassValue?.user?.id !== hass.user?.id;
    this.hassValue = hass;
    this.session?.update(hass);
    if (otherUser) this.restart();
    else if (first) this.start();
    else this.renderer?.update(hass, this.config as CardConfig);
  }

  getCardSize(): number {
    return 10;
  }

  getGridOptions(): Record<string, number> {
    return { columns: 12, min_columns: 6 };
  }

  connectedCallback(): void {
    clearTimeout(this.detached);
    this.detached = undefined;
    this.start();
  }

  /** Detached for good only if it stays detached: Home Assistant moves cards between columns on every re-layout. */
  disconnectedCallback(): void {
    clearTimeout(this.detached);
    this.detached = setTimeout(() => {
      this.detached = undefined;
      if (!this.isConnected) this.stop();
    }, DETACH_GRACE_MS);
  }

  private restart(): void {
    this.stop();
    this.start();
  }

  private stop(): void {
    this.run += 1;
    clearTimeout(this.retry);
    this.abort.abort();
    this.renderer?.destroy();
    this.renderer = null;
    this.dropSession();
  }

  private dropSession(): void {
    if (this.acquired) this.session?.release();
    this.acquired = false;
    this.session = null;
  }

  private start(): void {
    if (!this.isConnected || !this.hassValue || !this.config || this.renderer || this.session) return;
    void this.load(++this.run);
  }

  private async load(run: number): Promise<void> {
    const hass = this.hassValue as Hass, config = this.config as CardConfig;
    const de = german(hass);
    const slug = findSlug(hass, config);
    if (!slug) {
      this.message(de ? "Solar Visor ist in Home Assistant nicht installiert (oder „addon“ in der Karte angeben)."
        : "Solar Visor is not installed in Home Assistant (or set “addon” in the card).");
      return;
    }
    const session = sessionFor(slug, hass.user?.id);
    this.session = session;
    this.abort = new AbortController();
    const signal = AbortSignal.any([this.abort.signal, AbortSignal.timeout(TIMEOUT_MS)]);
    try {
      const info = await within(hass.callWS<{ ingress_url: string | null; state: string | null }>(
        { type: "supervisor/ingress/info", slug }));
      if (run !== this.run) return;
      if (!info.ingress_url || info.state !== "started") {
        this.later(run, de ? "Solar Visor läuft gerade nicht." : "Solar Visor is not running.");
        return;
      }
      this.acquired = true;
      await session.acquire(hass);
      if (run !== this.run) return;
      const apiBase = info.ingress_url.endsWith("/") ? info.ingress_url : info.ingress_url + "/";
      const get = () => fetch(apiBase + "card/v1/plant", { credentials: "same-origin", cache: "no-store", signal });
      let plant = await get();
      if (plant.status === 401 || plant.status === 403) {
        await session.renew();
        plant = await get();
      }
      if (run !== this.run) return;
      if (!plant.ok) {
        this.later(run, plant.status === 503
          ? (de ? "Die Anlage ist im Hub noch nicht eingerichtet." : "The plant is not set up in the hub yet.")
          : (de ? `Hub antwortet nicht (${plant.status}).` : `The hub does not answer (${plant.status}).`));
        return;
      }
      const { build } = (await plant.json()) as { build: string };
      const url = `${apiBase}card/v1/card.js?v=${encodeURIComponent(build)}`;
      const mod = (await within(import(/* @vite-ignore */ url))) as RendererModule;
      if (run !== this.run) return;
      if (mod.contract !== CONTRACT) {
        this.message(de ? "Diese Kartenversion passt nicht zum Hub – bitte die Datei solar-visor-card.js erneuern."
          : "This card version does not match the hub – please replace solar-visor-card.js.");
        return;
      }
      this.root.replaceChildren();
      this.renderer = mod.create(this.root, {
        apiBase, build, hass, config,
        reload: () => this.restart(),
        renewSession: () => session.renew(),
      });
    } catch (e) {
      if (run !== this.run) return;
      this.later(run, (de ? "Solar Visor nicht erreichbar: " : "Solar Visor unreachable: ") + String((e as Error)?.message ?? e));
    }
  }

  /** A message, and a new attempt in a while. */
  private later(run: number, text: string): void {
    this.message(text);
    this.dropSession();
    this.retry = setTimeout(() => {
      if (run === this.run) this.start();
    }, RETRY_MS);
  }

  private message(text: string): void {
    const box = document.createElement("div");
    box.style.cssText = "padding:16px;background:#000;color:#bbb;font:14px system-ui,sans-serif;border-radius:var(--ha-card-border-radius,12px)";
    box.textContent = text;
    this.root.replaceChildren(box);
  }
}

if (!customElements.get(TAG)) {
  customElements.define(TAG, SolarVisorCard);
  const w = window as unknown as { customCards?: unknown[] };
  w.customCards = w.customCards ?? [];
  w.customCards.push({ type: TAG, name: "Solar Visor", description: "Energiefluss der Anlage (Solar Visor Hub)" });
}
