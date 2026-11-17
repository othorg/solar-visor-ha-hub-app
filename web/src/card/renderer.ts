// Renderer of the Solar Visor card, served by the hub over Ingress and loaded by the card loader (plan lovelace-card).
// The diagram and its data are the FlowPanel (panel.ts, shared with the web interface); here only what belongs to Home
// Assistant: its language, a click on a node opens Home Assistant's history of its sensors, a new hub build → the loader
// loads the new renderer. Reads only.
import { CardApi } from "./api";
import { CONTRACT, type CardConfig, type Hass, type RendererContext, type RendererInstance } from "./contract";
import { closeHistory, HISTORY_CSS, openHistory } from "./history";
import { FlowPanel } from "./panel";
import { texts, type Texts } from "./texts";

export const contract = CONTRACT;

export function create(root: ShadowRoot, ctx: RendererContext): RendererInstance {
  return new Renderer(root, ctx);
}

class Renderer implements RendererInstance {
  private hass: Hass;
  private config: CardConfig;
  private t: Texts;
  private readonly panel: FlowPanel;
  private readonly style: HTMLStyleElement;
  private destroyed = false;
  private historyHass: ((h: Hass) => void) | null = null;

  constructor(private readonly root: ShadowRoot, ctx: RendererContext) {
    this.hass = ctx.hass;
    this.config = ctx.config;
    this.t = texts(ctx.hass);
    this.style = document.createElement("style");
    this.style.textContent = HISTORY_CSS;
    root.append(this.style);
    this.panel = new FlowPanel(root, {
      api: new CardApi(ctx), texts: this.t, onNode: (node) => void this.openNode(node),
      build: ctx.build, onBuildChange: () => ctx.reload(),
    });
  }

  update(hass: Hass, config: CardConfig): void {
    this.hass = hass;
    this.config = config;
    const t = texts(hass);
    if (t !== this.t) {
      this.t = t;
      this.panel.setTexts(t);
    }
    this.historyHass?.(hass);
  }

  destroy(): void {
    this.destroyed = true;
    this.historyHass = null;
    closeHistory(this.root);
    this.panel.destroy();
    this.style.remove();
  }

  private async openNode(node: string): Promise<void> {
    const sensors = this.panel.currentPlant?.sensors[node];
    const topology = this.panel.currentTopology;
    if (!sensors?.length || !topology) return;
    const hours = Number(this.config.hours_to_show) > 0 ? Number(this.config.hours_to_show) : 24;
    const update = await openHistory(this.root, this.hass, {
      title: topology.name(node), hours, note: this.t.historyNote, close: this.t.close,
      entities: sensors.map((s) => ({ entity: s.entity_id, name: topology.metricName(s.metric_id) })),
    }, () => !this.destroyed);
    if (this.destroyed) return;
    this.historyHass = update;
    if (!update) this.panel.notice(this.t.noHistory);
  }
}
