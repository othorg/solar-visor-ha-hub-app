// The hub's card routes over Ingress (GET only): the plant with its sensors and the live snapshot.
import type { RendererContext } from "./contract";
import type { PlantCapabilities, PlantSnapshot } from "./model";

export interface CardSensor {
  entity_id: string;
  metric_id: string;
}

export interface CardPlant {
  build: string;
  capabilities: PlantCapabilities;
  sensors: Record<string, CardSensor[]>;
}

export class NotConfigured extends Error {}

export class HttpError extends Error {
  constructor(readonly status: number) {
    super(`HTTP ${status}`);
  }
}

const TIMEOUT_MS = 10_000;

export class CardApi {
  private etag: string | null = null;
  private last: PlantSnapshot | null = null;

  constructor(private readonly ctx: Pick<RendererContext, "apiBase" | "renewSession">,
              private readonly fetcher: typeof fetch = (...a) => fetch(...a)) {}

  plant(signal?: AbortSignal): Promise<CardPlant> {
    return this.json<CardPlant>("card/v1/plant", signal);
  }

  /** The snapshot; an unchanged one (304) is the last one again – a successful freshness check. */
  async snapshot(signal?: AbortSignal): Promise<PlantSnapshot> {
    const headers: Record<string, string> = this.etag && this.last ? { "If-None-Match": this.etag } : {};
    const r = await this.get("card/v1/snapshot", signal, headers);
    if (r.status === 304 && this.last) return this.last;
    const snap = (await r.json()) as PlantSnapshot;
    this.etag = r.headers.get("ETag");
    this.last = snap;
    return snap;
  }

  /** Forget the cached snapshot (another plant revision). */
  reset(): void {
    this.etag = null;
    this.last = null;
  }

  private async json<T>(path: string, signal?: AbortSignal): Promise<T> {
    return (await (await this.get(path, signal)).json()) as T;
  }

  /** GET with a timeout; a refused session (401/403) is renewed once. */
  private async get(path: string, signal?: AbortSignal, headers: Record<string, string> = {}): Promise<Response> {
    let r = await this.once(path, signal, headers);
    if (r.status === 401 || r.status === 403) {
      await this.ctx.renewSession();
      r = await this.once(path, signal, headers);
    }
    if (r.status === 503) {
      const body = (await r.json().catch(() => null)) as { detail?: string } | null;
      if (body?.detail === "plant_not_configured") throw new NotConfigured("plant_not_configured");
    }
    if (!r.ok && r.status !== 304) throw new HttpError(r.status);
    return r;
  }

  private async once(path: string, signal: AbortSignal | undefined, headers: Record<string, string>): Promise<Response> {
    const timeout = AbortSignal.timeout(TIMEOUT_MS);
    const both = signal ? AbortSignal.any([signal, timeout]) : timeout;
    return this.fetcher(this.ctx.apiBase + path, { credentials: "same-origin", cache: "no-store", headers, signal: both });
  }
}
