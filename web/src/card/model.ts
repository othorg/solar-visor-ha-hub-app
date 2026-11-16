// JSON of the hub's v2 models as the card reads them (snake_case, as `/card/v1/*` sends them; models_v2.py).

export type MetricState = "ok" | "stale" | "unknown" | "implausible";
/** null = unknown – never 0. */
export type MetricValue = number | boolean | string | null;

export interface PlantPoint {
  id: string;
  unit: string;
  role: string;
}

export interface PlantString {
  id: string;
  name: string;
  points: PlantPoint[];
}

export interface PlantModuleInfo {
  id: string;
  kind: string;
  name: string;
  balance: string | null;
  icon?: string | null;
  points: PlantPoint[];
  strings: PlantString[];
}

export interface PlantCapabilities {
  api_version: string;
  hub_version: string;
  plant_revision: number;
  timezone: string | null;
  modules: PlantModuleInfo[];
  metrics: string[];
  features: Record<string, boolean>;
  pv_peak_w?: number | null;
  rscp_pv?: string | null;
}

export interface PlantMetric {
  metric_id: string;
  module: string;
  value: MetricValue;
  unit: string;
  role: string;
  state: MetricState;
  reasons: string[];
  derived: boolean;
  input_metric_ids?: string[];
  source_changed_at?: string | null;
  reported_at?: string | null;
  observed_at: string;
}

export interface FlowEdge {
  edge_id: string;
  from_node: string;
  to_node: string;
  power_w: number | null;
  state: MetricState;
  derived: boolean;
}

export interface PlantSnapshot {
  snapshot_id: string;
  generated_at: string;
  plant_revision: number;
  connected: boolean;
  metrics: Record<string, PlantMetric>;
  edges: FlowEdge[];
  balance_residual_w: number | null;
  balance_comparable: boolean;
  modules: Record<string, { state: string; last_seen_at: string | null }>;
}

/** The number of a metric value, if it is one. */
export function num(v: MetricValue | undefined): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
