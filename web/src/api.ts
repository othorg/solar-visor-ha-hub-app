// Client of the hub's interface API. Every path is relative to the page (Home Assistant Ingress serves it under a
// changing prefix). Writes carry a custom header: a browser cannot send it cross-site without a preflight that Home
// Assistant does not grant – the hub refuses writes without it (CSRF protection over the HA session).

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
  }
}

export const WRITE_HEADER = "X-Solar-Visor";

async function parse<T>(res: Response): Promise<T> {
  const text = await res.text();
  const body: unknown = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : body;
    throw new ApiError(res.status, detail);
  }
  return body as T;
}

export async function get<T>(path: string, fetcher: typeof fetch = fetch): Promise<T> {
  return parse<T>(await fetcher(`api/${path}`, { headers: { Accept: "application/json" } }));
}

export async function send<T>(
  method: "POST" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
  fetcher: typeof fetch = fetch,
): Promise<T> {
  return parse<T>(
    await fetcher(`api/${path}`, {
      method,
      headers: { Accept: "application/json", "Content-Type": "application/json", [WRITE_HEADER]: "1" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}

// --- shapes of the API (mirrors hub/src/solar_visor_hub/ui.py) ----------------------------------------------------

export interface Session {
  user: string | null;
  mode: string;
  version: string;
  running_revision: number | null;
  stored_revision: number | null;
  startup_error: string | null;
}

export interface Problem {
  path: string;
  code: string;
  message: string;
}

export interface PlantView {
  mode: string;
  active: Record<string, unknown> | null;
  active_revision: number;
  running_revision: number | null;
  draft: { id: string; base_revision: number; saved_at: string; plant: Record<string, unknown> } | null;
  problems: Problem[];
  has_previous: boolean;
  startup_error: string | null;
}

export interface Activated {
  revision: number;
  restarting: boolean;
}

export interface Addresses {
  read_url: string | null;
  admin_url: string | null;
  suggested_read_url: string | null;
  suggested_admin_url: string | null;
  /** over Home Assistant (integration „Solar Visor“) – read and admin alike; works wherever HA is reachable */
  suggested_ha_url: string | null;
  /** the integration forwarded a request recently */
  bridge_seen: boolean;
}
