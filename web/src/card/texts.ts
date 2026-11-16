// Texts of the card in German and English (Home Assistant's language decides).
import type { Hass } from "./contract";

export interface Texts {
  lang: "de" | "en";
  notConfigured: string;
  unreachable: string;
  waiting: string;
  stale: string;
  asOf: string;
  noData: string;
  historyNote: string;
  close: string;
  noHistory: string;
  opensHistory: string;
}

const DE: Texts = {
  lang: "de",
  notConfigured: "Die Anlage ist im Hub noch nicht eingerichtet.",
  unreachable: "Hub nicht erreichbar",
  waiting: "Verbinde mit dem Hub …",
  stale: "Veraltet",
  asOf: "Stand",
  noData: "Keine Daten",
  historyNote: "Rohwerte aus Home Assistant – Vorzeichen und Einheiten können vom Dashboard abweichen.",
  close: "Schließen",
  noHistory: "Der Verlauf von Home Assistant ist hier nicht verfügbar.",
  opensHistory: "öffnet den Verlauf",
};

const EN: Texts = {
  lang: "en",
  notConfigured: "The plant is not set up in the hub yet.",
  unreachable: "Hub unreachable",
  waiting: "Connecting to the hub …",
  stale: "Stale",
  asOf: "As of",
  noData: "No data",
  historyNote: "Raw values from Home Assistant – signs and units may differ from the dashboard.",
  close: "Close",
  noHistory: "Home Assistant's history is not available here.",
  opensHistory: "opens the history",
};

export function texts(hass: Hass | undefined): Texts {
  return (hass?.locale?.language ?? hass?.language ?? "de").startsWith("de") ? DE : EN;
}
