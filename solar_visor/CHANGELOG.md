# Changelog

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
