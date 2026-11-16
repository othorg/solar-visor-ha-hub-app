// Devices: the addresses the apps use and a one-time pairing code (10 minutes) with the pairing text built from them.
// The text carries the code, never a device token; scanning it in the apps follows in P5.
import { useState, type FormEvent } from "react";
import { ApiError, get, send, type Addresses, type Session } from "./api";
import { ErrorNote, useAdopt, useLoad } from "./common";
import { useI18n } from "./i18n";

interface Pairing {
  code: string;
  scope: string;
  valid_minutes: number;
  read_url: string | null;
  admin_url: string | null;
  payload: string | null;
  qr_svg: string | null;
}

interface Device {
  id: number;
  name: string;
  scope: string;
  created_at: string;
  last_used_at: string | null;
}

export function PairingCard() {
  const { t } = useI18n();
  const [scope, setScope] = useState<"admin" | "read">("admin");
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [error, setError] = useState(false);

  async function create() {
    try {
      setPairing(await send<Pairing>("POST", "pairing", { scope }));
      setError(false);
    } catch {
      setError(true);
    }
  }

  return (
    <section className="card" aria-labelledby="pair-title">
      <h2 id="pair-title">{t("pairing.title")}</h2>
      <p className="hint">{t("pairing.hint")}</p>
      <div className="form">
        <label>
          {t("pairing.scope")}
          <select value={scope} onChange={(e) => setScope(e.target.value as "admin" | "read")}>
            <option value="admin">{t("pairing.scope.admin")}</option>
            <option value="read">{t("pairing.scope.read")}</option>
          </select>
        </label>
        <div className="actions">
          <button type="button" onClick={() => void create()}>{t("pairing.create")}</button>
        </div>
      </div>
      {error && <p className="error" role="alert">{t("error.unavailable")}</p>}
      {pairing && (
        <div className="pairing" aria-live="polite">
          <p>
            {t("pairing.code")}: <strong className="code">{pairing.code}</strong>
          </p>
          {pairing.payload ? (
            <>
              {pairing.qr_svg && (
                <img className="qr" alt={t("pairing.text")}
                     src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(pairing.qr_svg)}`} />
              )}
              <p className="hint">{t("pairing.qr_hint")}</p>
              <label>
                {t("pairing.text")}
                <textarea readOnly rows={3} value={pairing.payload} onFocus={(e) => e.target.select()} />
              </label>
            </>
          ) : (
            <p className="note">{t("pairing.no_addresses")}</p>
          )}
        </div>
      )}
    </section>
  );
}

export function AddressesCard() {
  const { t } = useI18n();
  const loaded = useLoad<Addresses>("addresses");
  const [read, setRead] = useState("");
  const [admin, setAdmin] = useState("");
  const [state, setState] = useState<"idle" | "saving" | "saved">("idle");
  const [error, setError] = useState<unknown>(null);
  useAdopt(loaded.data, (d) => {
    setRead(d.read_url ?? "");
    setAdmin(d.admin_url ?? "");
  });

  async function save(e: FormEvent) {
    e.preventDefault();
    setState("saving");
    try {
      await send<Addresses>("PUT", "addresses", { read_url: read || null, admin_url: admin || null });
      setError(null);
      setState("saved");
    } catch (err) {
      setError(err);
      setState("idle");
    }
  }

  const a = loaded.data;
  return (
    <section className="card" aria-labelledby="addr-title">
      <h2 id="addr-title">{t("addresses.title")}</h2>
      <p className="hint">{t("addresses.hint")}</p>
      <ErrorNote error={loaded.error ?? error} />
      <form onSubmit={save} className="form">
        <label>
          {t("addresses.read")}
          <input type="url" value={read} onChange={(e) => (setRead(e.target.value), setState("idle"))}
                 placeholder={a?.suggested_read_url ?? ""} />
          {a?.suggested_read_url && <small>{t("addresses.suggestion", { value: a.suggested_read_url })}</small>}
        </label>
        <label>
          {t("addresses.admin")}
          <input type="url" value={admin} onChange={(e) => (setAdmin(e.target.value), setState("idle"))}
                 placeholder={a?.suggested_admin_url ?? ""} />
          {a?.suggested_admin_url && <small>{t("addresses.suggestion", { value: a.suggested_admin_url })}</small>}
        </label>
        <div className="actions">
          <button type="submit" disabled={state === "saving" || !a}>{t("common.save")}</button>
          {state === "saved" && <span role="status">{t("common.saved")}</span>}
        </div>
      </form>
    </section>
  );
}

function DevicesCard() {
  const { t } = useI18n();
  const list = useLoad<Device[]>("devices");
  const [confirm, setConfirm] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);

  async function revoke(id: number) {
    try {
      await send("DELETE", `devices/${id}`);
      setConfirm(null);
      list.reload();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <section className="card" aria-labelledby="devices-title">
      <h2 id="devices-title">{t("devices.title")}</h2>
      <ErrorNote error={list.error ?? error} />
      {list.data && list.data.length === 0 && <p>{t("devices.none")}</p>}
      <ul className="devices">
        {list.data?.map((d) => (
          <li key={d.id}>
            <span>
              <strong>{d.name}</strong> <small>({d.scope}) · {d.last_used_at
                ? t("devices.last_used", { time: new Date(d.last_used_at).toLocaleString() }) : t("devices.never")}</small>
            </span>
            {confirm === d.id ? (
              <button type="button" className="danger" onClick={() => void revoke(d.id)}>{t("devices.confirm")}</button>
            ) : (
              <button type="button" className="secondary" onClick={() => setConfirm(d.id)}>{t("devices.revoke")}</button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function TestsCard({ influx }: { influx: boolean }) {
  const { t } = useI18n();
  const [result, setResult] = useState<Record<string, string>>({});
  async function run(kind: "ha" | "influx") {
    try {
      const r = await send<{ status: string; detail: string }>("POST", `test/${kind}`);
      setResult((old) => ({ ...old, [kind]: t("tests.result", r) }));
    } catch (e) {
      setResult((old) => ({ ...old, [kind]: e instanceof ApiError ? `${e.status}` : t("error.unavailable") }));
    }
  }
  return (
    <section className="card" aria-labelledby="tests-title">
      <h2 id="tests-title">{t("tests.title")}</h2>
      <div className="actions">
        <button type="button" className="secondary" onClick={() => void run("ha")}>{t("tests.ha")}</button>
        {influx && <button type="button" className="secondary" onClick={() => void run("influx")}>{t("tests.influx")}</button>}
      </div>
      {Object.entries(result).map(([k, v]) => <p key={k} role="status">{v}</p>)}
    </section>
  );
}

interface Influx {
  url: string;
  database: string;
  retention_policy: string | null;
  token_set: boolean;
}

/** InfluxDB connection and token – formerly the add-on's Home Assistant options (user decision 2026-11-15). One form:
 *  a stored token goes to a new address only when it is entered again (as for the E3/DC credentials). */
function InfluxCard() {
  const { t } = useI18n();
  const cfg = useLoad<Influx>("influx");             // 503 outside the add-on: only the token
  const secrets = useLoad<{ influx_token: boolean }>("secrets");
  const [url, setUrl] = useState("");
  const [database, setDatabase] = useState("");
  const [rp, setRp] = useState("");
  const [token, setToken] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  useAdopt(cfg.data, (d) => {
    setUrl(d.url);
    setDatabase(d.database);
    setRp(d.retention_policy ?? "");
  });

  async function save(e: FormEvent) {
    e.preventDefault();
    try {
      if (cfg.data) {
        await send("PUT", "influx", { url, database, retention_policy: rp, ...(token ? { token } : {}) });
        cfg.reload();
      } else {
        await send("PUT", "secrets/influx", { value: token });
      }
      setToken("");
      setMessage(t("common.saved"));
      secrets.reload();
    } catch (err) {
      const code = err instanceof ApiError ? (err.detail as { code?: string } | null)?.code : null;
      setMessage(code === "invalid_influx" ? t("influx.invalid") : code === "token_required" ? t("influx.token_required")
                 : t("common.error"));
    }
  }

  const tokenSet = cfg.data?.token_set ?? secrets.data?.influx_token;
  // 503: this hub has no InfluxDB connection to set (not the add-on) – only the token; any other failure is shown
  const unsupported = cfg.error instanceof ApiError && cfg.error.status === 503;
  if (cfg.error && !unsupported) {
    return (
      <section className="card" aria-labelledby="influx-title">
        <h2 id="influx-title">{t("secrets.title")}</h2>
        <ErrorNote error={cfg.error} />
        <button type="button" className="secondary" onClick={() => cfg.reload()}>{t("common.reload")}</button>
      </section>
    );
  }
  if (!cfg.data && !unsupported) return null;        // still loading
  return (
    <section className="card" aria-labelledby="influx-title">
      <h2 id="influx-title">{t("secrets.title")}</h2>
      <p className="hint">{t("secrets.hint")}</p>
      <ErrorNote error={secrets.error} />
      <form className="form" onSubmit={save}>
        {cfg.data && (
          <>
            <label>{t("influx.url")}<input type="url" value={url} onChange={(e) => setUrl(e.target.value)} /></label>
            <label>{t("influx.database")}<input value={database} onChange={(e) => setDatabase(e.target.value)} /></label>
            <label>{t("influx.rp")}<input value={rp} onChange={(e) => setRp(e.target.value)} /></label>
          </>
        )}
        <label>
          {t("secrets.influx_new")}{tokenSet !== undefined && <small> ({t(tokenSet ? "secrets.influx_set" : "secrets.influx_missing")})</small>}
          <input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
        </label>
        <div className="actions">
          <button type="submit" disabled={!cfg.data && !token}>{t("common.save")}</button>
          {message && <span role="status">{message}</span>}
        </div>
      </form>
    </section>
  );
}

interface Rscp {
  enabled: boolean;
  host: string;
  port: number;
  user_set: boolean;
  password_set: boolean;
  key_set: boolean;
  state: string;
}

function RscpConfigCard() {
  const { t } = useI18n();
  const cfg = useLoad<Rscp>("rscp");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [key, setKey] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  useAdopt(cfg.data, (d) => {
    setHost(d.host);
    setPort(String(d.port));
  });
  if (cfg.error) return null;                       // no E3/DC in this hub (503) or not reachable

  async function save(e: FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = { host, port: Number(port) };
    if (user) body.user = user;
    if (password) body.password = password;
    if (key) body.key = key;
    try {
      await send("PUT", "rscp", body);
      setUser("");
      setPassword("");
      setKey("");
      setMessage(t("common.saved"));
      cfg.reload();
    } catch (err) {
      const code = err instanceof ApiError ? (err.detail as { code?: string } | null)?.code : null;
      setMessage(code === "password_required" ? t("rscp.password_required") : t("common.error"));
    }
  }

  const set = (flag: boolean | undefined) => t(flag ? "rscp.set" : "rscp.unset");
  return (
    <section className="card" aria-labelledby="rscpcfg-title">
      <h2 id="rscpcfg-title">{t("rscp.config_title")}</h2>
      {cfg.data && <p className="hint">{t("rscp.state", { state: cfg.data.state })}</p>}
      <form className="form" onSubmit={save}>
        <label>{t("rscp.host")}<input value={host} onChange={(e) => setHost(e.target.value)} /></label>
        <label>{t("rscp.port")}<input type="number" value={port} onChange={(e) => setPort(e.target.value)} /></label>
        <label>{t("rscp.user")} <small>({set(cfg.data?.user_set)})</small>
          <input autoComplete="off" value={user} onChange={(e) => setUser(e.target.value)} /></label>
        <label>{t("rscp.password")} <small>({set(cfg.data?.password_set)})</small>
          <input type="password" autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        <label>{t("rscp.key")} <small>({set(cfg.data?.key_set)})</small>
          <input type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} /></label>
        <div className="actions">
          <button type="submit">{t("common.save")}</button>
          {message && <span role="status">{message}</span>}
        </div>
      </form>
    </section>
  );
}

function TransferCard() {
  const { t } = useI18n();
  const [message, setMessage] = useState<string | null>(null);
  async function importFile(file: File) {
    let plant: unknown;
    try {
      plant = JSON.parse(await file.text());
    } catch {
      setMessage(t("transfer.invalid"));
      return;
    }
    try {
      const view = await get<{ draft: { id: string } | null }>("plant");
      await send("POST", "plant/import", { plant, draft_id: view.draft?.id ?? null });
      setMessage(t("transfer.imported"));
    } catch {
      setMessage(t("transfer.invalid"));
    }
  }
  return (
    <section className="card" aria-labelledby="transfer-title">
      <h2 id="transfer-title">{t("transfer.title")}</h2>
      <p className="hint">{t("transfer.hint")}</p>
      <div className="actions">
        <a className="button secondary" href="api/plant/export" download>{t("transfer.export")}</a>
        <label className="button secondary">
          {t("transfer.import")}
          <input type="file" accept="application/json,.json" className="visually-hidden"
                 onChange={(e) => e.target.files?.[0] && void importFile(e.target.files[0])} />
        </label>
      </div>
      {message && <p role="status">{message}</p>}
    </section>
  );
}

export function DevicesSection() {
  const session = useLoad<Session>("session");
  const influx = session.data?.mode === "plant_legacy" || session.data?.mode === "mapping";
  return (
    <>
      <PairingCard />
      <DevicesCard />
      <AddressesCard />
      <TestsCard influx={influx} />
      {influx && <InfluxCard />}
      <RscpConfigCard />
      <TransferCard />
    </>
  );
}
