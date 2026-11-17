# Solar Visor Hub

[English](README.md) · **Deutsch**

Eine rein lesende Home-Assistant-App (früher „Add-on“) für Photovoltaikanlagen. Sie liest die Werte Ihrer PV-Anlage aus
Home Assistant, berechnet die Energiebilanz und ihre Historie und zeigt alles auf einer eigenen Seite in der
Seitenleiste von Home Assistant. Außerdem versorgt sie die Solar-Visor-Apps für iPhone, Apple Watch und Mac.

Sie **schaltet nie etwas**: keine Dienstaufrufe, keine Schreibzugriffe auf Home Assistant, keine Cloud, keine Telemetrie.

[![Repository zu Home Assistant hinzufügen](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fothorg%2Fsolar-visor-ha-hub-app)

## Funktionen

- **Anlagenmodell**: Netz, bis zu drei PV-Erzeuger (mit Strings), Batterie, Wallbox und weitere Verbraucher – jeder
  Datenpunkt wird in der Web-Oberfläche einem Home-Assistant-Sensor zugeordnet, mit Prüfung von Einheiten, Vorzeichen
  und fehlenden Werten.
- **Übersicht**: der Energiefluss der Anlage wie in den Solar-Visor-Apps – ein Tipp auf ein Gerät zeigt seine
  aktuellen Werte – und die Verbindung zu Home Assistant.
- **Dashboard-Symbole**: ein Symbol je Modul (Solarmodul, Haus, Batterie, Strommast, Wallbox, Wohnmobil, Stecker) für
  das Dashboard der Solar-Visor-Apps und die Dashboard-Karte.
- **Dashboard-Karte**: der Energiefluss der Solar-Visor-Apps als Karte für Home-Assistant-Dashboards – ein Tipp auf
  ein Gerät zeigt den Verlauf seiner Sensoren (siehe unten).
- **Historie**: Die App bewahrt abgeschlossene Tage in einem eigenen Archiv auf (auch wenn der Recorder sie gelöscht
  hat) und berechnet daraus und aus dem Home-Assistant-Recorder die Energie je Tag, Woche, Monat und Jahr – angezeigt
  in den Solar-Visor-Apps; die Web-Seite zeigt Quellen und Archiv und kann Tage neu berechnen. Unbekanntes bleibt
  unbekannt – nie 0.
- **Apps**: eine tokengeschützte Lese-API für die Solar-Visor-Apps; Kopplung per Einmalcode oder QR-Code.
- **Sicher aufgebaut**: Die Web-Oberfläche ist nur über Home Assistant und nur für Administratoren erreichbar; die
  API-Ports sind standardmäßig aus. Die Dashboard-Karte liest nur.

## Voraussetzungen

- Home Assistant OS oder Home Assistant Supervised (Apps brauchen den Supervisor).
- 64-Bit-System: `amd64` oder `aarch64` (zum Beispiel Raspberry Pi 4/5 oder Home Assistant Green/Yellow).
- Getestet mit Home Assistant Core 2026.11.1, Supervisor 2026.11.0, Home Assistant OS 18.4.
- Die Sensoren Ihrer Anlage in Home Assistant (Leistung in W/kW, Energiezähler in Wh/kWh empfohlen).

## Installation

1. Die Schaltfläche oben anklicken – oder in Home Assistant **Einstellungen → Apps** öffnen, den App-Store öffnen, im
   ⋮-Menü **Repositories** wählen und `https://github.com/othorg/solar-visor-ha-hub-app` hinzufügen.
2. **Solar Visor Hub** installieren und starten.
3. **Solar Visor** in der Seitenleiste öffnen.

## Einrichtung

Alles wird in der Web-Oberfläche eingerichtet – die App hat keine Optionen:

1. **Anlage** – die Module Ihrer Anlage einschalten, jedem Datenpunkt einen Home-Assistant-Sensor zuordnen (Vorschläge
   kommen aus Ihrem Energie-Dashboard), ein Symbol wählen, die installierte PV-Leistung eintragen, prüfen und aktivieren.
2. **Datenpunkte** – alle zugeordneten Sensoren mit Live-Wert und Prüfergebnis.
3. **Historie** – Datenquellen, Archiv der abgeschlossenen Tage, Neuberechnung.
4. **Geräte & Diagnose** – Kopplung der Apps, gekoppelte Geräte, Verbindungstests.

## Dashboard-Karte

Die Dashboard-Karte zeigt den Energiefluss wie die Solar-Visor-Apps: Live-Werte, fließende Kugeln, den Ladestand der
Batterie. Ein Tipp auf ein Gerät öffnet den Home-Assistant-Verlauf seiner Sensoren. Die App muss installiert sein und
laufen – die Karte holt ihre Daten von ihr.

Die Karte auf einem von zwei Wegen installieren:

- **HACS**: *HACS* → ⋮ → *Benutzerdefinierte Repositories*, `https://github.com/othorg/solar-visor-ha-hub-app` mit dem
  Typ **Dashboard** hinzufügen, dann **Solar Visor Card** herunterladen. HACS legt die Dashboard-Ressource selbst an.
- **Von Hand**: `solar-visor-card.js` aus dem
  [neuesten Release](https://github.com/othorg/solar-visor-ha-hub-app/releases/latest) herunterladen, nach
  `/config/www/solar-visor-card.js` kopieren und unter *Einstellungen → Dashboards* → ⋮ → *Ressourcen* als
  `/local/solar-visor-card.js?v=0.3.0` (Typ *JavaScript-Modul*) hinzufügen. Nach dem Ersetzen der Datei den `v=`-Wert
  erhöhen.

Dann die Karte einem Dashboard hinzufügen (*Karte hinzufügen* → *Solar Visor* oder in YAML):

```yaml
type: custom:solar-visor-card
hours_to_show: 24   # optional: Zeitraum des Verlaufs in Stunden (Standard 24)
# addon: 1a2b3c4d_solar_visor   # optional: nur falls die Karte die App nicht findet (Slug aus der Adresse der App)
```

Jeder Home-Assistant-Benutzer kann die Karte nutzen: Sie erreicht die App über Home Assistant (Ingress) mit den Rechten
des angemeldeten Benutzers und liest nur. Deshalb ist der Seitenleisten-Eintrag **Solar Visor** für alle Benutzer
sichtbar; wer kein Administrator ist, sieht dort statt der Web-Oberfläche einen kurzen Hinweis. Die Karte selbst kommt
von der App und aktualisiert sich mit ihr – die Datei in `/config/www` lädt sie nur.

## Ports

Beide Ports sind **standardmäßig aus**. Schalten Sie sie im Abschnitt *Netzwerk* der App nur ein, wenn Sie die Apps
nutzen:

| Port | Zweck |
|---|---|
| 8080 | Lese-API für die Apps, Widgets und die Watch (tokengeschützt). Für den Zugriff von außerhalb des Heimnetzes ein VPN oder einen Reverse Proxy mit TLS verwenden. |
| 8081 | Kopplung und Verwaltung für die Apps. **Nur im Heimnetz – diesen Port nie ins Internet freigeben.** Nach dem Koppeln aller Geräte wieder ausschalten. |

## Apps

Die Solar-Visor-Apps für iPhone, Apple Watch und Mac sind nicht im App Store. Sie sind auf Anfrage über TestFlight
erhältlich – bitte ein Issue in diesem Repository eröffnen.

## Inspiration

Solar Visor wurde von der App AutarkieManager von [OTRKY](https://www.otrky.com/de/) inspiriert. Es ist ein
unabhängiges Projekt und nicht mit OTRKY verbunden.

## Haftungsausschluss

Solar Visor ist nicht mit Home Assistant, E3/DC, KOSTAL oder openWB verbunden. Alle Produktnamen gehören ihren
Inhabern. Die Software wird ohne Gewähr bereitgestellt (siehe Lizenz).

## Lizenz

Apache License 2.0 – siehe [LICENSE](LICENSE) und [NOTICE](NOTICE).
