// Renderer of the Solar Visor card, served by the hub over Ingress and loaded by the card loader (plan lovelace-card).
// Lifecycle as the apps' DashboardModel: the plant at start and on a new revision, the snapshot every 5 s (304 counts as
// a successful check), stale after 35 s without success, paused while the page is hidden; a new hub build → the loader
// loads the new renderer. Clicking a node opens Home Assistant's history of its sensors. Reads only.
import { CardApi, NotConfigured, type CardPlant } from "./api";
import { CONTRACT, type CardConfig, type Hass, type RendererContext, type RendererInstance } from "./contract";
import { closeHistory, HISTORY_CSS, openHistory } from "./history";
import type { PlantSnapshot } from "./model";
import { texts, type Texts } from "./texts";
import { Topology } from "./topology";
import { createView, VIEW_CSS, type View } from "./view";

export const contract = CONTRACT;

const POLL_MS = 5_000;
const STALE_AFTER_MS = 35_000;          // DashboardModel.clientStaleAfter
const PLANT_RECHECK_MS = 300_000;       // a new hub build without a failed poll in between (update between two polls)

export interface CardState {
  plant: CardPlant | null;
  snapshot: PlantSnapshot | null;
  stale: boolean;
  /** Why there is nothing (or nothing current) to show. */
  problem: string | null;
}

export function create(root: ShadowRoot, ctx: RendererContext): RendererInstance {
  return new Renderer(root, ctx);
}

class Renderer implements RendererInstance {
  private hass: Hass;
  private config: CardConfig;
  private t: Texts;
  private readonly api: CardApi;
  private readonly view: View;
  private plant: CardPlant | null = null;
  private topology: Topology | null = null;
  private snapshot: PlantSnapshot | null = null;
  private lastSuccess = 0;
  private plantAt = 0;
  private problem: string | null = null;
  private failing = false;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private abort = new AbortController();
  private destroyed = false;
  private historyHass: ((h: Hass) => void) | null = null;
  private readonly onVisibility = () => this.visibility();

  constructor(private readonly root: ShadowRoot, private readonly ctx: RendererContext) {
    this.hass = ctx.hass;
    this.config = ctx.config;
    this.t = texts(ctx.hass);
    this.api = new CardApi(ctx);
    const style = document.createElement("style");
    style.textContent = VIEW_CSS + HISTORY_CSS;
    root.append(style);
    this.view = createView(root, this.t, (node) => void this.openNode(node));
    document.addEventListener("visibilitychange", this.onVisibility);
    this.render();
    void this.tick();
  }

  update(hass: Hass, config: CardConfig): void {
    this.hass = hass;
    this.config = config;
    const t = texts(hass);
    if (t !== this.t) {
      this.t = t;
      this.topology = this.plant ? new Topology(this.plant.capabilities, t.lang) : null;
      this.render();
    }
    this.historyHass?.(hass);
  }

  destroy(): void {
    this.destroyed = true;
    clearTimeout(this.timer);
    this.abort.abort();
    document.removeEventListener("visibilitychange", this.onVisibility);
    this.historyHass = null;
    closeHistory(this.root);
    this.view.destroy();
  }

  private visibility(): void {
    if (this.destroyed) return;
    clearTimeout(this.timer);
    if (document.visibilityState === "visible") void this.tick();
    else this.abort.abort();
  }

  private async tick(): Promise<void> {
    if (this.destroyed) return;
    this.abort = new AbortController();
    const signal = this.abort.signal;
    try {
      const now = Date.now();
      if (!this.plant || this.failing || now - this.plantAt > PLANT_RECHECK_MS) await this.loadPlant(signal);
      if (this.destroyed || signal.aborted) return;
      let snap = await this.api.snapshot(signal);
      if (this.destroyed || signal.aborted) return;
      if (snap.plant_revision !== this.plant?.capabilities.plant_revision) {
        await this.loadPlant(signal);                     // another plant: its snapshot only with its topology
        this.api.reset();
        snap = await this.api.snapshot(signal);
        if (this.destroyed || signal.aborted) return;
        if (snap.plant_revision !== this.plant?.capabilities.plant_revision) throw new Error("revision");
      }
      this.snapshot = snap;
      this.lastSuccess = Date.now();
      this.problem = null;
      this.failing = false;
    } catch (e) {
      if (this.destroyed || signal.aborted) return;
      this.failing = true;
      this.problem = e instanceof NotConfigured ? this.t.notConfigured : this.t.unreachable;
      if (e instanceof NotConfigured) {
        this.plant = null;
        this.topology = null;
        this.snapshot = null;
      }
    }
    this.render();
    if (!this.destroyed && document.visibilityState === "visible") {
      this.timer = setTimeout(() => void this.tick(), POLL_MS);
    }
  }

  private async loadPlant(signal: AbortSignal): Promise<void> {
    const plant = await this.api.plant(signal);
    if (this.destroyed || signal.aborted) return;
    if (plant.build !== this.ctx.build) {
      this.destroyed = true;                              // the hub was updated: the loader takes over
      this.ctx.reload();
      return;
    }
    if (plant.capabilities.plant_revision !== this.plant?.capabilities.plant_revision) this.snapshot = null;
    this.plant = plant;
    this.topology = new Topology(plant.capabilities, this.t.lang);
    this.plantAt = Date.now();
  }

  private render(): void {
    if (this.destroyed) return;
    const state: CardState = {
      plant: this.plant, snapshot: this.snapshot, problem: this.problem,
      stale: !this.lastSuccess || Date.now() - this.lastSuccess > STALE_AFTER_MS,
    };
    this.view.render(state, this.topology, this.t);
  }

  private async openNode(node: string): Promise<void> {
    const sensors = this.plant?.sensors[node];
    const topology = this.topology;
    if (!sensors?.length || !topology) return;
    const hours = Number(this.config.hours_to_show) > 0 ? Number(this.config.hours_to_show) : 24;
    const update = await openHistory(this.root, this.hass, {
      title: topology.name(node), hours, note: this.t.historyNote, close: this.t.close,
      entities: sensors.map((s) => ({ entity: s.entity_id, name: topology.metricName(s.metric_id) })),
    }, () => !this.destroyed);
    if (this.destroyed) return;
    this.historyHass = update;
    if (!update) this.view.notice(this.t.noHistory);
  }
}
