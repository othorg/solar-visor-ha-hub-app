import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { CrashCard } from "./CrashCard";
import { I18nProvider } from "./i18n";
import { applyTheme, currentTheme, ThemeProvider } from "./theme";
import "./styles.css";

applyTheme(currentTheme());                 // before the first paint: no light flash in a dark Home Assistant
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <I18nProvider>
        <CrashCard>
          <App />
        </CrashCard>
      </I18nProvider>
    </ThemeProvider>
  </StrictMode>,
);
