# Solar Visor Hub

**English** – Deutsch below.

Read-only app for photovoltaic systems: it reads your plant's values from Home Assistant, calculates the energy
balance and its history and serves the Solar Visor apps. It never switches anything in Home Assistant.

## Setup

Open **Solar Visor** in the sidebar (or *Open web UI*). Everything is set up there – the app has no options:

- **Overview** – the energy flow of the plant (tap a device for its current values), connection to Home Assistant.
- **Plant** – modules, the Home Assistant sensor of every data point, installed PV power; check and activate.
- **Data points** – every assigned sensor with its live value and check.
- **History** – data sources, the archive of finished days, recalculation (the energy per period is shown in the
  Solar Visor apps).
- **Devices & diagnostics** – pairing code and QR code for the apps, paired devices, connection tests.

## Full feed-in

A PV generator whose power goes through its own meter straight into the public grid is marked under *Plant* → PV
generator → *Connection* → *Full feed-in*. It keeps its node, values, strings, energy and history but is never part of
the home: generation, consumption, self-consumption and autarky are those of the home's generators, its energy is the
extra category *full feed-in*.

- Only if the plant's grid sensor does **not** measure the generator – otherwise leave it a normal PV generator.
- At least one PV generator has to feed the home. The E3/DC details (RSCP) never belong to a full feed-in generator.
- Live values follow at once, the energy history from the start of the next day; earlier days keep the plant of
  their time (also after *Recalculate*). Best set it at the first setup.
- Grid counters (import/export energy) only if they measure the home's connection alone, not the feed-in meter.
- Update the Solar Visor apps first; 0.4.x cannot load such a plant.

## Dashboard card

`custom:solar-visor-card` shows the energy flow in any dashboard, light or dark like Home Assistant; tapping a device
opens the history of its sensors (*Home*: its calculated power of today).
Install it with HACS (custom repository, type *Dashboard*) or from the release file `solar-visor-card.js` – see the
README of the repository. Every Home Assistant user can use the card, so the sidebar entry is visible to all users;
users who are not administrators see a notice there.

## Ports

Both ports are off by default. Switch them on under *Network* only for the Solar Visor apps:

- **8080** – read API (token-protected). From outside the home network only through a VPN or a TLS reverse proxy.
- **8081** – pairing and administration. Home network only, never forward it to the internet.

The apps are available on request via TestFlight (see the repository).

---

# Solar Visor Hub (Deutsch)

Rein lesende App für Photovoltaikanlagen: Sie liest die Werte Ihrer Anlage aus Home Assistant, berechnet die
Energiebilanz und ihre Historie und versorgt die Solar-Visor-Apps. Sie schaltet nie etwas in Home Assistant.

## Einrichtung

**Solar Visor** in der Seitenleiste öffnen (oder *Web-UI öffnen*). Dort wird alles eingerichtet – die App hat keine
Optionen:

- **Übersicht** – der Energiefluss der Anlage (ein Tipp auf ein Gerät zeigt seine aktuellen Werte), Verbindung zu
  Home Assistant.
- **Anlage** – Module, der Home-Assistant-Sensor jedes Datenpunkts, installierte PV-Leistung; prüfen und aktivieren.
- **Datenpunkte** – alle zugeordneten Sensoren mit Live-Wert und Prüfung.
- **Historie** – Datenquellen, Archiv der abgeschlossenen Tage, Neuberechnung (die Energie je Zeitraum zeigen die
  Solar-Visor-Apps).
- **Geräte & Diagnose** – Kopplungscode und QR-Code für die Apps, gekoppelte Geräte, Verbindungstests.

## Volleinspeisung

Ein PV-Erzeuger, dessen Leistung über einen eigenen Zähler direkt ins öffentliche Netz geht, wird unter *Anlage* →
PV-Erzeuger → *Einbindung* → *Volleinspeisung* markiert. Er behält Knoten, Werte, Strings, Energie und Historie, gehört
aber nie zum Haus: Erzeugung, Verbrauch, Eigenverbrauch und Autarkie sind die der Erzeuger im Haus, seine Energie ist
die eigene Kategorie *Volleinspeisung*.

- Nur wenn der Netzsensor der Anlage den Erzeuger **nicht** misst – sonst bleibt er ein normaler PV-Erzeuger.
- Mindestens ein PV-Erzeuger muss ins Haus speisen. Die E3/DC-Details (RSCP) gehören nie zu einem Volleinspeiser.
- Live-Werte folgen sofort, die Energiehistorie ab dem nächsten Tagesbeginn; frühere Tage behalten die Anlage ihrer
  Zeit (auch nach *Neu berechnen*). Am besten gleich bei der Ersteinrichtung setzen.
- Netz-Zähler (Bezug/Einspeisung) nur, wenn sie allein den Hausanschluss messen, nicht den Zähler der Volleinspeisung.
- Vorher die Solar-Visor-Apps aktualisieren; 0.4.x kann eine solche Anlage nicht laden.

## Dashboard-Karte

`custom:solar-visor-card` zeigt den Energiefluss in jedem Dashboard, hell oder dunkel wie Home Assistant; ein Tipp auf
ein Gerät öffnet den Verlauf seiner Sensoren (*Zuhause*: seine berechnete Leistung von heute). Installation über HACS (benutzerdefiniertes Repository, Typ *Dashboard*) oder aus der Release-Datei
`solar-visor-card.js` – siehe README des Repositorys. Jeder Home-Assistant-Benutzer kann die Karte nutzen, deshalb ist
der Seitenleisten-Eintrag für alle Benutzer sichtbar; wer kein Administrator ist, sieht dort einen Hinweis.

## Ports

Beide Ports sind standardmäßig aus. Unter *Netzwerk* nur für die Solar-Visor-Apps einschalten:

- **8080** – Lese-API (tokengeschützt). Von außerhalb des Heimnetzes nur über ein VPN oder einen Reverse Proxy mit TLS.
- **8081** – Kopplung und Verwaltung. Nur im Heimnetz, nie ins Internet freigeben.

Die Apps gibt es auf Anfrage über TestFlight (siehe Repository).
