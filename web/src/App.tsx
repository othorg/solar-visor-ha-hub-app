// Shell of the interface: header, sections (state + location hash – no client routes, the Ingress prefix changes),
// error display. The sections fill in step by step (P4 detail plan §5).
import { useEffect, useState } from "react";
import { send, type Activated, type PlantView, type Session } from "./api";
import { ErrorNote, useLoad } from "./common";
import { DevicesSection } from "./DevicesSection";
import { HistorySection } from "./HistorySection";
import { LiveCard } from "./LiveCard";
import { useI18n, type Language, type TextKey } from "./i18n";
import { PlantSection } from "./PlantSection";
import { PointsSection } from "./PointsSection";

const SECTIONS = ["overview", "plant", "points", "history", "devices"] as const;
type Section = (typeof SECTIONS)[number];

function sectionFromHash(hash: string): Section {
  const s = hash.replace(/^#/, "");
  return (SECTIONS as readonly string[]).includes(s) ? (s as Section) : "overview";
}

function OverviewSection() {
  const { t } = useI18n();
  const session = useLoad<Session>("session");
  const s = session.data;
  const modeKey = s ? (`mode.${s.mode}` as TextKey) : null;
  return (
    <>
      <section className="card" aria-labelledby="hub-title">
        <h2 id="hub-title">{t("overview.hub")}</h2>
        <ErrorNote error={session.error} />
        {s ? (
          <dl className="facts">
            <dt>{t("overview.user")}</dt>
            <dd>{s.user ?? "–"}</dd>
            <dt>{t("overview.version")}</dt>
            <dd>{s.version}</dd>
            <dt>{t("overview.mode")}</dt>
            <dd>{modeKey && t(modeKey)}</dd>
            <dt>{t("overview.revision")}</dt>
            <dd>{s.running_revision ?? "–"}</dd>
          </dl>
        ) : (
          !session.error && <p>{t("common.loading")}</p>
        )}
        {s && s.stored_revision !== s.running_revision && s.stored_revision !== null && (
          <p className="note">
            {t("overview.revision_pending", { stored: s.stored_revision, running: s.running_revision ?? "–" })}
          </p>
        )}
      </section>
      {s && <PlantStateCard session={s} />}
      {s && s.mode !== "setup" && <LiveCard />}
    </>
  );
}

/** Setup hint, a start that failed with the stored plant, and the way back to the previous plant. */
function PlantStateCard({ session }: { session: Session }) {
  const { t } = useI18n();
  const plant = useLoad<PlantView>("plant");
  const [restarting, setRestarting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (session.mode !== "setup" && !session.startup_error) return null;

  async function restore() {
    if (!plant.data) return;
    try {
      await send<Activated>("POST", "plant/restore", { active_revision: plant.data.active_revision });
      setRestarting(true);
      setTimeout(() => location.reload(), 3000);
    } catch (err) {
      setError(err);
    }
  }

  return (
    <section className="card" aria-live="polite">
      {session.startup_error ? (
        <p className="error">{t("overview.startup_error", { error: session.startup_error })}</p>
      ) : (
        <p>{t("overview.setup_hint")}</p>
      )}
      <ErrorNote error={plant.error ?? error} />
      {restarting ? (
        <p role="status">{t("overview.restarting")}</p>
      ) : (
        plant.data?.has_previous && (
          <button type="button" onClick={restore}>
            {t("overview.restore")}
          </button>
        )
      )}
    </section>
  );
}

export default function App() {
  const { t, language, setLanguage } = useI18n();
  const [section, setSection] = useState<Section>(() => sectionFromHash(location.hash));
  useEffect(() => {
    const onHash = () => setSection(sectionFromHash(location.hash));
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);

  return (
    <div className="app">
      <header className="top">
        <h1>{t("app.title")}</h1>
        <label className="lang">
          <span className="visually-hidden">{t("app.language")}</span>
          <select value={language} onChange={(e) => setLanguage(e.target.value as Language)} aria-label={t("app.language")}>
            <option value="de">Deutsch</option>
            <option value="en">English</option>
          </select>
        </label>
      </header>
      <nav className="tabs" aria-label={t("app.title")}>
        {SECTIONS.map((s) => (
          <a key={s} href={`#${s}`} aria-current={s === section ? "page" : undefined}>
            {t(`nav.${s}` as TextKey)}
          </a>
        ))}
      </nav>
      <main>
        {section === "overview" ? <OverviewSection />
          : section === "plant" ? <PlantSection />
          : section === "points" ? <PointsSection />
          : section === "history" ? <HistorySection />
          : section === "devices" ? <DevicesSection />
          : null}
      </main>
    </div>
  );
}
