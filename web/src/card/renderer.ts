// Renderer of the Solar Visor card, served by the hub over Ingress and loaded by the card loader (plan lovelace-card).
// The diagram and its data are the FlowPanel (panel.ts, shared with the web interface); here only what belongs to Home
// Assistant: its language and display, a click on a node opens Home Assistant's history of its sensors – home, without
// sensors, the hub's calculated curve (curve.ts) – and a new hub build → the loader loads the new renderer. Reads only.
import { CardApi } from "./api";
import { CONTRACT, type CardConfig, type Hass, type RendererContext, type RendererInstance } from "./contract";
import { CURVE_CSS, CURVE_NODES, openCurve, setCurveTheme } from "./curve";
import { closeHistory, HISTORY_CSS, openHistory } from "./history";
import type { Theme } from "./palette";
import { FlowPanel } from "./panel";
import { texts, type Texts } from "./texts";

export const contract = CONTRACT;

/** Home Assistant's dark or light display (its user setting, „auto“ already resolved); dark when it does not say. */
export function themeOf(hass: Hass): Theme {
  return hass.themes?.darkMode === false ? "light" : "dark";
}

export function create(root: ShadowRoot, ctx: RendererContext): RendererInstance {
  return new Renderer(root, ctx);
}

class Renderer implements RendererInstance {
  private hass: Hass;
  private config: CardConfig;
  private t: Texts;
  private theme: Theme;
  private readonly api: CardApi;
  private readonly panel: FlowPanel;
  private readonly style: HTMLStyleElement;
  private destroyed = false;
  private historyHass: ((h: Hass) => void) | null = null;

  constructor(private readonly root: ShadowRoot, ctx: RendererContext) {
    this.hass = ctx.hass;
    this.config = ctx.config;
    this.t = texts(ctx.hass);
    this.theme = themeOf(ctx.hass);
    this.style = document.createElement("style");
    this.style.textContent = HISTORY_CSS + CURVE_CSS;
    root.append(this.style);
    this.api = new CardApi(ctx);
    this.panel = new FlowPanel(root, {
      api: this.api, texts: this.t, theme: this.theme, onNode: (node) => void this.openNode(node),
      clickable: (id, sensors) => CURVE_NODES.includes(id) || (sensors[id]?.length ?? 0) > 0,
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
    const theme = themeOf(hass);
    if (theme !== this.theme) {
      this.theme = theme;
      this.panel.setTheme(theme);
      setCurveTheme(this.root, theme);
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
    if (!topology) return;
    if (CURVE_NODES.includes(node)) {
      openCurve(this.root, {
        title: `${topology.name(node)} · ${this.t.today}`, note: this.t.curveNote, close: this.t.close,
        loading: this.t.loading, failed: this.t.curveFailed, empty: this.t.noData, lang: this.t.lang, theme: this.theme,
        label: (id) => topology.metricName(id),
      }, (signal) => this.api.nodeHistory(node, signal));
      return;
    }
    if (!sensors?.length) return;
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
