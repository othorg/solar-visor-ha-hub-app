// The flow diagram with its data, without Home Assistant (plan web-overview-dashboard): used by the card's renderer and
// by the web interface's overview. Lifecycle as the apps' DashboardModel: the plant at start, after a failure, on a new
// revision and every 5 min; the snapshot every 5 s (304 counts as a successful check), stale after 35 s without
// success, paused while the page is hidden. Reads only.
import { CardApi, NotConfigured, type CardPlant } from "./api";
import type { PlantSnapshot } from "./model";
import type { Theme } from "./palette";
import type { Texts } from "./texts";
import { Topology } from "./topology";
import { createView, VIEW_CSS, type Clickable, type View } from "./view";

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

export interface PanelOptions {
  api: CardApi;
  texts: Texts;
  /** Dark (default, the app dashboard) or light surface. */
  theme?: Theme;
  onNode(node: string): void;
  /** Which nodes take a click (default: the ones with Home Assistant sensors). */
  clickable?: Clickable;
  /** The build the panel was loaded for: another one in the plant stops it and calls ``onBuildChange``. */
  build?: string;
  onBuildChange?(): void;
  /** After every render: what is shown, with its freshness. */
  onUpdate?(state: CardState, topology: Topology | null): void;
}

export class FlowPanel {
  private t: Texts;
  private readonly view: View;
  private readonly style: HTMLStyleElement;
  private plant: CardPlant | null = null;
  private topology: Topology | null = null;
  private snapshot: PlantSnapshot | null = null;
  private lastSuccess = 0;
  private plantAt = 0;
  private problem: string | null = null;
  private failing = false;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private abort = new AbortController();
  private destroyed = false;                         // stopped: no more requests or renders
  private closed = false;                            // torn down
  private readonly onVisibility = () => this.visibility();

  constructor(root: ShadowRoot, private readonly opts: PanelOptions) {
    this.t = opts.texts;
    this.style = document.createElement("style");
    this.style.textContent = VIEW_CSS;
    root.append(this.style);
    this.view = createView(root, this.t, (node) => opts.onNode(node), opts.clickable, opts.theme);
    document.addEventListener("visibilitychange", this.onVisibility);
    this.render();
    void this.tick();
  }

  get currentPlant(): CardPlant | null {
    return this.plant;
  }

  get currentTopology(): Topology | null {
    return this.topology;
  }

  setTexts(t: Texts): void {
    if (t === this.t) return;
    this.t = t;
    this.topology = this.plant ? new Topology(this.plant.capabilities, t.lang) : null;
    this.render();
  }

  setTheme(theme: Theme): void {
    if (!this.closed) this.view.setTheme(theme);
  }

  notice(text: string): void {
    this.view.notice(text);
  }

  destroy(): void {
    if (this.closed) return;
    this.closed = true;
    this.destroyed = true;
    clearTimeout(this.timer);
    this.abort.abort();
    document.removeEventListener("visibilitychange", this.onVisibility);
    this.view.destroy();
    this.style.remove();
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
      let snap = await this.opts.api.snapshot(signal);
      if (this.destroyed || signal.aborted) return;
      if (snap.plant_revision !== this.plant?.capabilities.plant_revision) {
        await this.loadPlant(signal);                     // another plant: its snapshot only with its topology
        if (this.destroyed || signal.aborted) return;
        this.opts.api.reset();
        snap = await this.opts.api.snapshot(signal);
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
    const plant = await this.opts.api.plant(signal);
    if (this.destroyed || signal.aborted) return;
    if (this.opts.build !== undefined && plant.build !== this.opts.build) {
      this.destroyed = true;                              // the hub was updated: no more requests, the owner takes
      clearTimeout(this.timer);                           // over (the card's loader tears it down and loads anew)
      this.opts.onBuildChange?.();
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
    this.opts.onUpdate?.(state, this.topology);
  }
}
