// The plant: modules switched on and named, a sensor for every data point, checked live against Home Assistant.
// Every change goes into a draft; "Activate" saves it, the hub checks it as a whole, keeps the previous plant and
// restarts. Another tab's change is never overwritten (the hub answers 409).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, get, send, type Activated, type PlantView, type Problem, type Session } from "./api";
import { DICTIONARIES, useI18n, type TextKey } from "./i18n";
import { AddressesCard, PairingCard } from "./DevicesSection";
import {
  applySuggestions,
  asPlant,
  emptyPlant,
  otherVariantPoints,
  formatValue,
  isEnabled,
  metricOfPath,
  moduleOf,
  requiredPoints,
  sameJson,
  addString,
  isoToLocal,
  localToIso,
  removeString,
  RSCP_KEY,
  setModuleField,
  setPointEntity,
  setPointExtra,
  setPointField,
  parsePvPeak,
  setPvPeak,
  setRscp,
  setStringName,
  setStringPointEntity,
  setStringPointField,
  type Catalog,
  type CheckResult,
  type ModuleSpec,
  type PointSpec,
  type RawModule,
  type RawPlant,
  type RawPoint,
  type Suggestion,
} from "./plant";
import { SensorPicker } from "./SensorPicker";

type Phase = "idle" | "saving" | "activating" | "restarting";
type PickTarget = { module: string; point: string; string?: string; spec: PointSpec };
const STEPS: { key: "grid" | "pv" | "more" | "check" | "pair"; modules: string[] }[] = [
  { key: "grid", modules: ["grid"] },
  { key: "pv", modules: ["pv_1", "pv_2", "pv_3"] },
  { key: "more", modules: ["battery", "wallbox", "generic_1", "generic_2", "generic_3", "consumption"] },
  { key: "check", modules: [] },
  { key: "pair", modules: [] },
];
type Conflict = { kind: "draft" } | { kind: "plant"; revision: number } | null;

function has(key: string): key is TextKey {
  return key in DICTIONARIES.de;
}

/** The active sensor of a point whose sensor the edit changes (null: unchanged or new). */
function changedEntity(active: RawPlant | null, plant: RawPlant, metric: string): string | null {
  const [mid, pid] = metric.split(".") as [string, string];
  const before = active?.modules[mid]?.points?.[pid]?.entity;
  const now = plant.modules[mid]?.points?.[pid]?.entity;
  return before && now && before !== now ? before : null;
}

function yesterday(): string {
  const d = new Date(Date.now() - 86_400_000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export const RESTART_WAIT = { tries: 40, ms: 750 };      // about 30 s (tests shorten it)

async function waitForRevision(revision: number): Promise<boolean> {
  for (let i = 0; i < RESTART_WAIT.tries; i++) {
    await new Promise((r) => setTimeout(r, RESTART_WAIT.ms));
    try {
      const s = await get<Session>("session");
      if (s.running_revision === revision) return true;
    } catch {
      // restarting: the listener is briefly gone
    }
  }
  return false;
}

export function PlantSection() {
  const { t } = useI18n();
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [view, setView] = useState<PlantView | null>(null);
  const [plant, setPlant] = useState<RawPlant | null>(null);
  const [saved, setSaved] = useState<RawPlant | null>(null);
  const [draftId, setDraftId] = useState<string | null>(null);
  const [problems, setProblems] = useState<Problem[]>([]);
  const [checks, setChecks] = useState<Record<string, CheckResult>>({});
  const [suggestions, setSuggestions] = useState<Record<string, Suggestion[]>>({});
  const [phase, setPhase] = useState<Phase>("idle");
  const [conflict, setConflict] = useState<Conflict>(null);
  const [error, setError] = useState<unknown>(null);
  const [activated, setActivated] = useState<number | null>(null);
  const [picker, setPicker] = useState<PickTarget | null>(null);
  const [retro, setRetro] = useState<Record<string, string>>({});
  const [step, setStep] = useState(0);
  const [finished, setFinished] = useState(false);
  const [taken, setTaken] = useState<number | null>(null);
  const [startFailed, setStartFailed] = useState<string | null>(null);
  const [unusableDraft, setUnusableDraft] = useState(false);
  const tRef = useRef(t);
  tRef.current = t;                                      // a language switch must not reload (and drop) the edit

  const load = useCallback(async () => {
    try {
      const [c, v] = await Promise.all([get<Catalog>("catalog"), get<PlantView>("plant")]);
      const draft = asPlant(v.draft?.plant);
      const active = asPlant(v.active);
      const start = draft ?? active ?? emptyPlant({ grid: tRef.current("module.grid"), pv: "PV" });
      setUnusableDraft(Boolean(v.draft) && draft === null);    // kept under its id: the next save replaces it
      setCatalog(c);
      setView({ ...v, active: active as unknown as Record<string, unknown> | null });
      setPlant(start);
      setSaved(draft ?? active ?? null);
      setDraftId(v.draft?.id ?? null);
      setProblems(v.problems);
      setConflict(null);
      setError(null);
      setRetro({});
    } catch (e) {
      setError(e);
    }
    get<{ points: Record<string, Suggestion[]> }>("suggestions").then((s) => setSuggestions(s.points), () => undefined);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // live check and validation of what is on the screen, shortly after the last change
  useEffect(() => {
    if (!plant) return;
    let alive = true;
    const timer = setTimeout(() => {
      send<{ points: Record<string, CheckResult> }>("POST", "check", { plant }).then(
        (r) => alive && setChecks(r.points),
        () => undefined,
      );
      send<{ problems: Problem[] }>("POST", "plant/validate", { plant }).then(
        (r) => alive && setProblems(r.problems),
        () => undefined,
      );
    }, 400);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [plant]);

  const dirty = plant !== null && !sameJson(plant, saved);
  const blocked = problems.length > 0;
  const byMetric = useMemo(() => {
    const out: Record<string, Problem[]> = {};
    for (const p of problems) {
      const m = metricOfPath(p.path);
      if (m) (out[m] ??= []).push(p);
    }
    return out;
  }, [problems]);

  function failed(e: unknown) {
    if (e instanceof ApiError && e.status === 409) {
      const d = e.detail as { code?: string; active_revision?: number };
      setConflict(d.code === "plant_changed" ? { kind: "plant", revision: d.active_revision ?? 0 } : { kind: "draft" });
    } else if (e instanceof ApiError && e.status === 422) {
      const d = e.detail as { problems?: Problem[] };
      setProblems(d.problems ?? []);
    } else {
      setError(e);
    }
    setPhase("idle");
  }

  async function saveDraft(): Promise<string | null> {
    if (!plant) return null;
    setPhase("saving");
    try {
      const r = await send<{ draft_id: string; problems: Problem[] }>("PUT", "plant/draft", { plant, draft_id: draftId });
      setDraftId(r.draft_id);
      setSaved(plant);
      setProblems(r.problems);
      setPhase("idle");
      return r.draft_id;
    } catch (e) {
      failed(e);
      return null;
    }
  }

  async function activate() {
    if (!view) return;
    const id = dirty || !draftId ? await saveDraft() : draftId;
    if (!id) return;
    setPhase("activating");
    try {
      const active = view.active as RawPlant | null;
      const retroactive = Object.entries(retro)
        .filter(([metric]) => plant !== null && changedEntity(active, plant, metric) !== null)
        .map(([metric_id, from]) => ({ metric_id, from }));
      const r = await send<Activated>("POST", "plant/activate", { active_revision: view.active_revision, draft_id: id,
                                                                 retroactive });
      setPhase("restarting");
      setStartFailed(null);
      if (!(await waitForRevision(r.revision))) {
        // stored, but the hub does not run it: say so instead of "active" (Codex P4 review M5)
        const s = await get<Session>("session").catch(() => null);
        setStartFailed(s?.startup_error ?? t("plant.restart_timeout"));
        setPhase("idle");
        return;
      }
      setActivated(r.revision);
      if (!view.active) {
        setFinished(true);                               // the guided first setup goes on with pairing
        setStep(STEPS.length - 1);
      }
      setPhase("idle");
      await load();
    } catch (e) {
      failed(e);
    }
  }

  async function discard() {
    if (!draftId) return;
    try {
      await send("DELETE", `plant/draft?draft_id=${encodeURIComponent(draftId)}`);
      await load();
    } catch (e) {
      failed(e);
    }
  }

  if (error && !catalog) {
    return <p className="card error" role="alert">{error instanceof ApiError && error.status === 403 ? t("error.forbidden") : t("error.unavailable")}</p>;
  }
  if (!catalog || !view || !plant) return <p className="card">{t("common.loading")}</p>;

  const busy = phase !== "idle";
  const card = (spec: ModuleSpec) => (
    <ModuleCard key={spec.id} spec={spec} module={moduleOf(plant, spec.id)} checks={checks} problems={byMetric}
                onChange={setPlant} plant={plant} catalog={catalog} active={view.active as RawPlant | null}
                retroOffered={view.mode === "plant"} retro={retro}
                onRetro={(metric, day) => setRetro(({ [metric]: _old, ...rest }) => (day ? { ...rest, [metric]: day } : rest))}
                onPick={(target) => setPicker({ module: spec.id, ...target })} />
  );
  const pickerDialog = picker && (
    <SensorPicker
      title={t("picker.title", { point: `${moduleOf(plant, picker.module).name || t(`module.${picker.module}` as TextKey)}${picker.string ? ` – ${picker.string.replace("string_", "String ")}` : ""} – ${has(`point.${picker.point}`) ? t(`point.${picker.point}` as TextKey) : picker.point}` })}
      unit={picker.spec.unit}
      suggestions={picker.string ? [] : suggestions[`${picker.module}.${picker.point}`] ?? []}
      onClose={() => setPicker(null)}
      onPick={(entity) => {
        setPlant(picker.string
          ? setStringPointEntity(plant, picker.module, picker.string, picker.point, picker.spec, entity)
          : setPointEntity(plant, picker.module, picker.point, picker.spec, entity));
        setPicker(null);
      }}
    />
  );

  if (!view.active || finished) {                         // no plant yet: the guided first setup
    const current = STEPS[step]!;
    const next = async () => {
      if (dirty && !(await saveDraft())) return;           // every step is kept as draft
      setStep((n) => Math.min(n + 1, STEPS.length - 2));
    };
    const takeOver = () => {
      const names = Object.fromEntries(catalog.modules.map((m) => [m.id, t(`module.${m.id}` as TextKey)]));
      const after = applySuggestions(plant, catalog, suggestions, names);
      const count = (x: RawPlant) => Object.values(x.modules).reduce((n, m) => n + Object.keys((m as RawModule).points ?? {}).length, 0);
      setTaken(count(after) - count(plant));
      setPlant(after);
    };
    return (
      <>
        <section className="card" aria-labelledby="wizard-title">
          <h2 id="wizard-title">{t("wizard.title")}</h2>
          <p className="hint">{t("wizard.intro")}</p>
          <ol className="stepper">
            {STEPS.map((s, i) => (
              <li key={s.key} aria-current={i === step ? "step" : undefined} className={i < step ? "done" : ""}>
                {t(`wizard.step.${s.key}` as TextKey)}
              </li>
            ))}
          </ol>
          {conflict && (
            <p className="error" role="alert">
              {conflict.kind === "draft" ? t("plant.conflict_draft") : t("plant.conflict_plant", { revision: conflict.revision })}{" "}
              <button type="button" onClick={() => void load()}>{t("common.reload")}</button>
            </p>
          )}
          {current.key === "grid" && (
            <div className="actions">
              <button type="button" className="secondary" onClick={takeOver}>{t("wizard.suggest")}</button>
              {taken !== null && <span role="status">{taken > 0 ? t("wizard.suggested", { n: taken }) : t("wizard.no_suggestions")}</span>}
            </div>
          )}
        </section>

        {catalog.modules.filter((m) => current.modules.includes(m.id)).map(card)}
        {current.key === "more" && <PvPeakCard plant={plant} onChange={setPlant} />}
        {current.key === "more" && <RscpCard plant={plant} catalog={catalog} onChange={setPlant} />}

        {current.key === "check" && (
          <>
            <ProblemList problems={problems} />
            <section className="card toolbar" aria-live="polite">
              <p className="hint">{t("wizard.check_hint")}</p>
              {phase === "restarting" && <p role="status">{t("plant.restarting", { revision: 1 })}</p>}
              {startFailed && <p className="error" role="alert">{t("plant.start_failed", { error: startFailed })}</p>}
              <div className="actions">
                <button type="button" onClick={() => void activate()} disabled={busy || blocked || conflict !== null}>
                  {phase === "activating" ? t("plant.activating") : t("plant.activate")}
                </button>
              </div>
            </section>
          </>
        )}

        {current.key === "pair" && (
          <>
            <section className="card"><p className="ok" role="status">{t("wizard.done")}</p></section>
            <AddressesCard />
            <PairingCard />
            <section className="card"><a href="#overview">{t("wizard.to_overview")}</a></section>
          </>
        )}

        {current.key !== "pair" && (
          <section className="card actions">
            {step > 0 && <button type="button" className="secondary" onClick={() => setStep(step - 1)} disabled={busy}>{t("wizard.back")}</button>}
            {current.key !== "check" && <button type="button" onClick={() => void next()} disabled={busy || conflict !== null}>{t("wizard.next")}</button>}
          </section>
        )}
        {pickerDialog}
      </>
    );
  }

  return (
    <>
      <section className="card toolbar" aria-live="polite">
        <p className="hint">{t("plant.edit_hint")}</p>
        {view.mode === "plant_legacy" && <p className="note">{t("plant.legacy_note")}</p>}
        <p>
          {dirty ? <strong>{t("plant.unsaved")}</strong> : view.draft && t("plant.draft_from", {
            time: new Date(view.draft.saved_at).toLocaleString(),
          })}
        </p>
        {conflict && (
          <p className="error" role="alert">
            {conflict.kind === "draft" ? t("plant.conflict_draft") : t("plant.conflict_plant", { revision: conflict.revision })}{" "}
            <button type="button" onClick={() => void load()}>{t("common.reload")}</button>
          </p>
        )}
        {phase === "restarting" && <p role="status">{t("plant.restarting", { revision: (view.active_revision ?? 0) + 1 })}</p>}
        {startFailed && <p className="error" role="alert">{t("plant.start_failed", { error: startFailed })}</p>}
        {unusableDraft && <p className="note">{t("plant.draft_unusable")}</p>}
        {activated !== null && phase === "idle" && <p role="status" className="ok">{t("plant.activated", { revision: activated })}</p>}
        {error !== null && catalog && <p className="error" role="alert">{t("error.unavailable")}</p>}
        <div className="actions">
          <button type="button" onClick={() => void saveDraft()} disabled={busy || !dirty || conflict !== null}>
            {t("plant.save_draft")}
          </button>
          <button type="button" onClick={() => void activate()}
                  disabled={busy || blocked || conflict !== null || (!dirty && !draftId)}
                  title={blocked ? t("plant.blocked") : undefined}>
            {phase === "activating" ? t("plant.activating") : t("plant.activate")}
          </button>
          {draftId && (
            <button type="button" className="secondary" onClick={() => void discard()} disabled={busy}>
              {t("plant.discard")}
            </button>
          )}
        </div>
      </section>

      <ProblemList problems={problems} />

      {catalog.modules.map(card)}

      <PvPeakCard plant={plant} onChange={setPlant} />
      <RscpCard plant={plant} catalog={catalog} onChange={setPlant} />

      {pickerDialog}
    </>
  );
}

function ProblemList({ problems }: { problems: Problem[] }) {
  const { t } = useI18n();
  return (
    <section className="card" aria-labelledby="problems-title">
      <h2 id="problems-title">{t("plant.problems")}</h2>
      {problems.length === 0 ? (
        <p className="ok">{t("plant.no_problems")}</p>
      ) : (
        <ul className="problems">
          {problems.map((p, i) => (
            <li key={`${p.path}-${i}`}>
              <span className="mono">{p.path || "–"}</span>{" "}
              {has(`problem.${p.code}`) ? t(`problem.${p.code}` as TextKey) : p.message}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

interface ModuleProps {
  spec: ModuleSpec;
  module: RawModule;
  plant: RawPlant;
  catalog: Catalog;
  active: RawPlant | null;
  retroOffered: boolean;
  retro: Record<string, string>;
  checks: Record<string, CheckResult>;
  problems: Record<string, Problem[]>;
  onChange: (p: RawPlant) => void;
  onRetro: (metric: string, day: string | null) => void;
  onPick: (target: Omit<PickTarget, "module">) => void;
}

function ModuleCard(props: ModuleProps) {
  const { spec, module, plant, catalog, active, retroOffered, retro, checks, problems, onChange, onRetro, onPick } = props;
  const { t } = useI18n();
  const enabled = isEnabled(spec, module);
  const label = t(`module.${spec.id}` as TextKey);
  const required = requiredPoints(spec, module);
  const otherVariant = otherVariantPoints(spec, module);
  const others = Object.keys(spec.points).filter((pid) => !required.includes(pid) && !otherVariant.includes(pid));
  const assignedOthers = others.filter((pid) => module.points?.[pid]);
  const assigned = Object.keys(module.points ?? {});
  const row = (pid: string) => {
    const metric = `${spec.id}.${pid}`;
    const previous = changedEntity(active, plant, metric);
    return (
      <PointRow key={pid} id={`${spec.id}-${pid}`} kind={spec.kind} pointId={pid} spec={spec.points[pid]!}
                raw={module.points?.[pid]} check={checks[metric]} problems={problems[metric] ?? []}
                previous={previous} retroFrom={retroOffered && previous ? retro[metric] ?? null : undefined}
                onRetro={(day) => onRetro(metric, day)}
                siblings={assigned.filter((other) => other !== pid)}
                onExtra={(field, value) => onChange(setPointExtra(plant, spec.id, pid, field, value))}
                onPick={() => onPick({ point: pid, spec: spec.points[pid]! })}
                onRemove={() => onChange(setPointEntity(plant, spec.id, pid, spec.points[pid]!, null))}
                onSign={(sign) => onChange(setPointField(plant, spec.id, pid, "sign", sign))}
                onRole={(role) => onChange(setPointField(plant, spec.id, pid, "role", role))} />
    );
  };

  return (
    <section className={`card module ${enabled ? "" : "off"}`} aria-labelledby={`mod-${spec.id}`}>
      <header className="module-head">
        <h2 id={`mod-${spec.id}`}>
          {module.name || label}
          {module.name && module.name.toLowerCase() !== label.toLowerCase() && <small> · {label}</small>}
        </h2>
        {!spec.always_on && (
          <label className="switch">
            <input type="checkbox" checked={enabled}
                   onChange={(e) => onChange(setModuleField(plant, spec.id, "enabled", e.target.checked))} />
            {t("plant.enabled")}
          </label>
        )}
      </header>
      {spec.kind === "consumption" && <p className="hint">{t("plant.consumption_hint")}</p>}
      {enabled && (
        <div className="form">
          {spec.kind !== "consumption" && (
            <label>
              {t("plant.name")}
              <input value={module.name ?? ""} maxLength={catalog.max_name}
                     onChange={(e) => onChange(setModuleField(plant, spec.id, "name", e.target.value))} />
            </label>
          )}
          {spec.kind === "grid" && (
            <fieldset>
              <legend>{t("plant.variant")}</legend>
              {catalog.grid_variants.map((v) => (
                <label key={v} className="radio">
                  <input type="radio" name="grid-variant" checked={(module.variant ?? "signed") === v}
                         onChange={() => onChange(setModuleField(plant, spec.id, "variant", v))} />
                  {t(`plant.variant.${v}` as TextKey)}
                </label>
              ))}
            </fieldset>
          )}
          {spec.kind === "generic" && (
            <label>
              {t("plant.balance")}
              <select value={module.balance ?? "part_of_home"}
                      onChange={(e) => onChange(setModuleField(plant, spec.id, "balance", e.target.value))}>
                {catalog.generic_balances.map((b) => <option key={b} value={b}>{t(`plant.balance.${b}` as TextKey)}</option>)}
              </select>
            </label>
          )}
          {spec.kind === "wallbox" && (
            <label>
              {t("plant.trust")}
              <select value={module.trust ?? "none"}
                      onChange={(e) => onChange(setModuleField(plant, spec.id, "trust", e.target.value))}>
                {catalog.wallbox_trust.map((v) => <option key={v} value={v}>{t(`plant.trust.${v}` as TextKey)}</option>)}
              </select>
            </label>
          )}
          <ul className="points">
            {(spec.kind === "consumption" ? ["power"] : required).map(row)}
            {assignedOthers.map(row)}
          </ul>
          {spec.kind !== "consumption" && others.length > assignedOthers.length && (
            <details>
              <summary>{t("plant.more_points")}</summary>
              <ul className="points">{others.filter((pid) => !assignedOthers.includes(pid)).map(row)}</ul>
            </details>
          )}
          {spec.strings && <StringsBlock moduleId={spec.id} module={module} plant={plant} catalog={catalog}
                                         checks={checks} onChange={onChange} onPick={onPick} />}
        </div>
      )}
    </section>
  );
}

function StringsBlock({ moduleId, module, plant, catalog, checks, onChange, onPick }: {
  moduleId: string; module: RawModule; plant: RawPlant; catalog: Catalog; checks: Record<string, CheckResult>;
  onChange: (p: RawPlant) => void; onPick: (target: Omit<PickTarget, "module">) => void;
}) {
  const { t } = useI18n();
  const strings = module.strings ?? [];
  return (
    <details open={strings.length > 0}>
      <summary>{t("strings.title")}</summary>
      {strings.map((s) => (
        <fieldset key={s.id} className="string">
          <legend>{t("strings.label", { n: s.id.replace("string_", "") })}</legend>
          <label>
            {t("strings.name")}
            <input value={s.name ?? ""} maxLength={catalog.max_name}
                   onChange={(e) => onChange(setStringName(plant, moduleId, s.id, e.target.value))} />
          </label>
          <ul className="points">
            {Object.entries(catalog.string_points).map(([pid, spec]) => (
              <PointRow key={pid} id={`${moduleId}-${s.id}-${pid}`} kind="string" pointId={pid} spec={spec}
                        raw={s.points?.[pid]} check={checks[`${moduleId}.${s.id}.${pid}`]} problems={[]}
                        onPick={() => onPick({ point: pid, string: s.id, spec })}
                        onRemove={() => onChange(setStringPointEntity(plant, moduleId, s.id, pid, spec, null))}
                        onSign={(sign) => onChange(setStringPointField(plant, moduleId, s.id, pid, "sign", sign))}
                        onRole={(role) => onChange(setStringPointField(plant, moduleId, s.id, pid, "role", role))} />
            ))}
          </ul>
          <button type="button" className="secondary" onClick={() => onChange(removeString(plant, moduleId, s.id))}>
            {t("strings.remove")}
          </button>
        </fieldset>
      ))}
      {strings.length < catalog.max_strings && (
        <button type="button" className="secondary" onClick={() => onChange(addString(plant, moduleId, catalog.max_strings))}>
          {t("strings.add")}
        </button>
      )}
    </details>
  );
}

function PvPeakCard({ plant, onChange }: { plant: RawPlant; onChange: (p: RawPlant) => void }) {
  const { t } = useI18n();
  const stored = plant.pv_peak_w === undefined ? "" : String(plant.pv_peak_w / 1000);
  const [text, setText] = useState(stored);
  useEffect(() => setText((cur) => (setPvPeak(plant, cur).pv_peak_w === plant.pv_peak_w ? cur : stored)), [stored]);
  const invalid = text.trim() !== "" && parsePvPeak(text) === null;
  return (
    <section className="card" aria-labelledby="peak-title">
      <h2 id="peak-title">{t("peak.title")}</h2>
      <div className="form">
        <label>
          {t("peak.label")}
          <input type="text" inputMode="decimal" value={text} aria-invalid={invalid}
                 onChange={(e) => { setText(e.target.value); onChange(setPvPeak(plant, e.target.value)); }} />
        </label>
        {invalid && <p className="error" role="alert">{t("peak.invalid")}</p>}
        <p className="hint">{t("peak.hint")}</p>
      </div>
    </section>
  );
}

function RscpCard({ plant, catalog, onChange }: { plant: RawPlant; catalog: Catalog; onChange: (p: RawPlant) => void }) {
  const { t } = useI18n();
  const rscp = plant.modules[RSCP_KEY];
  const pvs = catalog.modules.filter((m) => m.kind === "pv" && isEnabled(m, moduleOf(plant, m.id)));
  return (
    <section className="card" aria-labelledby="rscp-title">
      <header className="module-head">
        <h2 id="rscp-title">{t("rscp.title")}</h2>
        <label className="switch">
          <input type="checkbox" checked={Boolean(rscp)}
                 onChange={(e) => onChange(setRscp(plant, e.target.checked ? pvs[0]?.id ?? "pv_1" : null))} />
          {t("rscp.enabled")}
        </label>
      </header>
      {rscp && (
        <div className="form">
          <label>
            {t("rscp.pv")}
            <select value={rscp.pv} onChange={(e) => onChange(setRscp(plant, e.target.value))}>
              {pvs.map((m) => <option key={m.id} value={m.id}>{moduleOf(plant, m.id).name || t(`module.${m.id}` as TextKey)}</option>)}
            </select>
          </label>
          <p className="hint">{t("rscp.hint")}</p>
        </div>
      )}
    </section>
  );
}

type Extra = "stale_after_s" | "valid_from" | "nonnegative" | "freshness_from";

interface PointProps {
  id: string;
  kind: string;
  pointId: string;
  spec: PointSpec;
  raw: RawPoint | undefined;
  check: CheckResult | undefined;
  problems: Problem[];
  previous?: string | null;             // the active sensor, if this edit changes it
  retroFrom?: string | null;            // offered: null = from now on, a day = retroactively (undefined: not offered)
  onRetro?: (day: string | null) => void;
  siblings?: string[];                  // other assigned points of the module (age reference)
  onExtra?: (field: Extra, value: string | number | boolean | undefined) => void;
  onPick: () => void;
  onRemove: () => void;
  onSign: (sign: number) => void;
  onRole: (role: string) => void;
}

function PointRow(p: PointProps) {
  const { id, kind, pointId, spec, raw, check, problems, previous, retroFrom, onRetro, siblings, onExtra } = p;
  const { t } = useI18n();
  const label = has(`point.${pointId}`) ? t(`point.${pointId}` as TextKey) : pointId;
  const signKey = [`point.sign.${kind}.${pointId}`, `point.sign.${pointId}`].find(has);
  const numeric = spec.unit !== "bool" && spec.unit !== "text";
  return (
    <li className="point" aria-labelledby={`${id}-label`}>
      <div className="point-head">
        <span id={`${id}-label`} className="point-label">
          {label} <small>({spec.unit})</small>
          {signKey && <small> · {t(signKey as TextKey)}</small>}
        </span>
        {check && raw && <span className={`chip ${check.status}`}>{t(`status.${check.status}` as TextKey)}</span>}
      </div>
      {raw ? (
        <div className="point-body">
          <div className="sensor">
            <strong>{check?.sensor?.name ?? raw.entity}</strong>
            <span className="mono">{raw.entity}</span>
            {previous && <small>{t("point.previous", { entity: previous })}</small>}
          </div>
          <div className="live">
            {t("point.live")}: <strong>{check ? formatValue(check.value, check.unit, ["on", "off"]) : "…"}</strong>
          </div>
          {retroFrom !== undefined && onRetro && (
            <fieldset>
              <legend>{t("point.applies")}</legend>
              <label className="radio">
                <input type="radio" name={`${id}-applies`} checked={retroFrom === null} onChange={() => onRetro(null)} />
                {t("point.applies.now")}
              </label>
              <label className="radio">
                <input type="radio" name={`${id}-applies`} checked={retroFrom !== null}
                       onChange={() => onRetro(retroFrom ?? yesterday())} />
                {t("point.applies.from")}
                <input type="date" value={retroFrom ?? ""} max={yesterday()} disabled={retroFrom === null}
                       onChange={(e) => onRetro(e.target.value || null)} aria-label={t("point.applies.from")} />
              </label>
              {retroFrom !== null && <p className="hint">{t("point.applies.hint")}</p>}
            </fieldset>
          )}
          {numeric && (
            <label className="inline">
              <input type="checkbox" checked={raw.sign === -1} onChange={(e) => p.onSign(e.target.checked ? -1 : 1)} />
              {t("point.invert")}
            </label>
          )}
          {spec.roles.length > 1 && (
            <label className="inline">
              {t("point.role")}
              <select value={raw.role} onChange={(e) => p.onRole(e.target.value)}>
                {spec.roles.map((r) => <option key={r} value={r}>{t(`role.${r}` as TextKey)}</option>)}
              </select>
            </label>
          )}
          <div className="actions">
            <button type="button" className="secondary" onClick={p.onPick}>{t("picker.change")}</button>
            <button type="button" className="secondary" onClick={p.onRemove}>{t("picker.remove")}</button>
          </div>
          {check && check.codes.length > 0 && (
            <ul className="codes">
              {check.codes.map((c) => (
                <li key={c} className={c === "signed_balance_only" ? "hint" : ""}>
                  {has(`check.${c}`) ? t(`check.${c}` as TextKey) : c}
                </li>
              ))}
            </ul>
          )}
          {onExtra && (
            <details>
              <summary>{t("point.advanced")}</summary>
              <div className="form">
                <label>
                  {t("point.stale_after")}
                  <input type="number" min={5} max={86400} value={raw.stale_after_s ?? ""}
                         onChange={(e) => onExtra("stale_after_s", e.target.value ? Number(e.target.value) : undefined)} />
                </label>
                <label>
                  {t("point.valid_from")}
                  <input type="datetime-local" value={isoToLocal(raw.valid_from)}
                         onChange={(e) => onExtra("valid_from", localToIso(e.target.value) ?? undefined)} />
                </label>
                {numeric && !spec.nonnegative && (
                  <label className="inline">
                    <input type="checkbox" checked={raw.nonnegative === true}
                           onChange={(e) => onExtra("nonnegative", e.target.checked)} />
                    {t("point.nonnegative")}
                  </label>
                )}
                {siblings && siblings.length > 0 && (
                  <label>
                    {t("point.freshness_from")}
                    <select value={raw.freshness_from ?? ""} onChange={(e) => onExtra("freshness_from", e.target.value || undefined)}>
                      <option value="">{t("point.freshness_none")}</option>
                      {siblings.map((s) => <option key={s} value={s}>{has(`point.${s}`) ? t(`point.${s}` as TextKey) : s}</option>)}
                    </select>
                  </label>
                )}
              </div>
            </details>
          )}
        </div>
      ) : (
        <button type="button" onClick={p.onPick}>{t("picker.choose")}</button>
      )}
      {problems.map((pr, i) => (
        <p key={i} className="error">{has(`problem.${pr.code}`) ? t(`problem.${pr.code}` as TextKey) : pr.message}</p>
      ))}
    </li>
  );
}
