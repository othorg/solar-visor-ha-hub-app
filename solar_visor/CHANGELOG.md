# Changelog

## 0.6.0

- **Remote access over Home Assistant**: the new integration *Solar Visor* (in this repository, install it through HACS
  as an *Integration*) makes the hub reachable for the apps at Home Assistant's own address,
  `https://<your Home Assistant>/api/solar_visor` – wherever Home Assistant is reachable (Home Assistant Cloud, Tailscale
  Funnel, your own reverse proxy). The API ports can stay off. The integration only forwards: no entities, no services,
  the apps' device tokens remain the only authentication.
- **Pairing from anywhere**: the pairing text (QR code, copied text) now carries a one-time invitation besides the code.
  Over Home Assistant pairing works only with the pairing text; the 8-digit code is for typing in at home. Wrong codes,
  invitations and watch pairings have separate limits, so a flood of wrong codes no longer blocks pairing.
- Web interface, *Devices & diagnostics* → *Addresses for the apps*: suggests the address over Home Assistant (Home
  Assistant Cloud, else the external URL from *Settings* → *System* → *Network*) and hints when the integration has not
  been seen yet.
- Texts mention the Android app.

## 0.5.0

- **Full feed-in**: a PV generator whose power goes through its own meter straight into the public grid can be marked
  as such in the web interface (*Plant* → PV generator → *Connection* → *Full feed-in*). It stays visible – flow,
  values, strings, energy and history – but never counts towards generation, consumption, home, self-consumption or
  autarky; its energy appears as the extra category *full feed-in*. Requirement: the plant's grid sensor does not
  measure this generator (otherwise it stays a normal PV generator). At least one PV generator has to feed the home.
- The mark takes effect with the activation for the live values and from the start of the next day for the energy
  history; earlier days are not recalculated with it, not even by *Recalculate*. Set it right at the setup – a plant
  switched later shows the generator in the generation up to the switch and in the full feed-in from then on.
- Update the Solar Visor apps before switching it on: older apps still count the generator into the home.
- Setup: with full feed-in, grid counters from the energy dashboard are not taken over automatically (they may include
  the feed-in meter); choose them only if they measure the home's connection alone, otherwise the grid power is used.
- Note: 0.4.x cannot load a plant with full feed-in (unknown field – it starts in setup mode). Go back only with a Home
  Assistant backup that restores the version and its data together.

## 0.4.1

- Dashboard card and the web interface's overview follow Home Assistant's light or dark display. Both use the surface
  of Home Assistant's cards (theme colours) instead of plain black; in light mode the colours of the Solar Visor apps'
  widgets on a light background.
- Dashboard card: tapping *Home* shows its calculated power of today next to the total consumption, as in the Solar
  Visor apps (home has no sensor of its own). The other devices keep opening the history of their sensors.

## 0.4.0

- Web interface, *Overview* → *Now*: the energy flow of the dashboard card instead of a table of live values. Tapping a
  device shows its current values below the diagram (strings, voltages, charge level …); one line below keeps the
  connection to Home Assistant, the time zone and the balance residual.
- Dashboard card: every data point has an English name now (English Home Assistant).

## 0.3.0

- Dashboard card `custom:solar-visor-card`: the energy flow of the Solar Visor apps as a Home Assistant dashboard card –
  live values, flowing balls, battery level. Tapping a device opens the Home Assistant history of its sensors.
  Install it with HACS (custom repository, type *Dashboard*) or from the release file `solar-visor-card.js` (see
  README). The card takes its data from this app through Home Assistant's Ingress and updates itself with the app.
- Every Home Assistant user can use the card. The **Solar Visor** sidebar entry is therefore visible to all users;
  users who are not administrators see a short notice there instead of the web interface.

## 0.2.1

- Dashboard symbols: every module can get its own symbol in the web interface (*Plant* → *Dashboard symbol*):
  solar panel, house, battery, power pylon, wallbox, motorhome, plug. The Solar Visor apps show it in their dashboard.
- Note: a plant with a chosen symbol cannot be loaded by 0.2.0 any more.

## 0.2.0

First public release.

- Plant model set up in the web interface: grid, up to three PV generators, battery, wallbox, extra consumers.
- Data point checks, live overview, history from the Home Assistant recorder and the app's own archive.
- Licenses of the app and of the bundled web dependencies inside the image.
- Read API and pairing for the Solar Visor apps; both ports off by default.
