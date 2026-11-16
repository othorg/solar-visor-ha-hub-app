// Last line of defence: an error while rendering shows a way out instead of a white page – most likely a draft the
// interface cannot show, so it offers to discard it (Codex P4 review M4).
import { Component, type ReactNode } from "react";
import { get, send } from "./api";
import { useI18n } from "./i18n";

function Fallback() {
  const { t } = useI18n();
  async function discard() {
    try {
      const view = await get<{ draft: { id: string } | null }>("plant");
      if (view.draft) await send("DELETE", `plant/draft?draft_id=${encodeURIComponent(view.draft.id)}`);
    } finally {
      location.reload();
    }
  }
  return (
    <div className="app">
      <section className="card" role="alert">
        <p>{t("error.crash")}</p>
        <button type="button" onClick={() => void discard()}>{t("error.discard_draft")}</button>
      </section>
    </div>
  );
}

export class CrashCard extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  render() {
    return this.state.failed ? <Fallback /> : this.props.children;
  }
}
