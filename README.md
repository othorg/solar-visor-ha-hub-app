# Solar Visor Hub

**English** · [Deutsch](README.de.md)

A read-only Home Assistant app (formerly called add-on) for photovoltaic systems. It reads the values of your PV plant
from Home Assistant, calculates the energy balance and its history, and shows everything in its own page in the Home
Assistant sidebar. It also serves the Solar Visor apps for iPhone, Apple Watch and Mac.

It **never switches anything**: no service calls, no writes to Home Assistant, no cloud, no telemetry.

[![Add the repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fothorg%2Fsolar-visor-ha-hub-app)

## Features

- **Plant model**: grid, up to three PV generators (with strings), battery, wallbox and extra consumers – each data
  point is assigned to a Home Assistant sensor in the web interface, with checks for units, signs and missing values.
- **Overview**: live values of the plant and the connection to Home Assistant.
- **History**: the app keeps finished days in its own archive (also after the recorder has purged them) and
  calculates energy per day, week, month and year from it and the Home Assistant recorder – shown in the Solar Visor
  apps; the web page shows the sources and the archive and can recalculate days. Unknown stays unknown – never 0.
- **Apps**: a token-protected read API for the Solar Visor apps; pairing with a one-time code or QR code.
- **Safe by design**: the web interface is only reachable through Home Assistant for administrators; the API ports are
  switched off by default.

## Requirements

- Home Assistant OS or Home Assistant Supervised (apps need the Supervisor).
- 64-bit system: `amd64` or `aarch64` (for example a Raspberry Pi 4/5 or Home Assistant Green/Yellow).
- Tested with Home Assistant Core 2026.11.1, Supervisor 2026.11.0, Home Assistant OS 18.4.
- The sensors of your plant in Home Assistant (power in W/kW, energy counters in Wh/kWh recommended).

## Installation

1. Click the button above, or in Home Assistant open **Settings → Apps**, open the app store, choose
   **Repositories** in the ⋮ menu and add `https://github.com/othorg/solar-visor-ha-hub-app`.
2. Install **Solar Visor Hub** and start it.
3. Open **Solar Visor** in the sidebar.

## Setup

Everything is set up in the web interface – there are no app options:

1. **Plant** – switch the modules of your plant on, assign a Home Assistant sensor to each data point (suggestions come
   from your energy dashboard), enter the installed PV power, check and activate.
2. **Data points** – all assigned sensors with their live value and the result of their check.
3. **History** – data sources, the archive of finished days, recalculation.
4. **Devices & diagnostics** – pairing for the apps, paired devices, connection tests.

## Ports

Both ports are **off by default**. Switch them on in the *Network* section of the app only if you use the apps:

| Port | Purpose |
|---|---|
| 8080 | Read API for the apps, widgets and the watch (token-protected). For access from outside your home network use a VPN or a reverse proxy with TLS. |
| 8081 | Pairing and administration for the apps. **Home network only – never forward this port to the internet.** Switch it off again once all devices are paired. |

## Apps

The Solar Visor apps for iPhone, Apple Watch and Mac are not in the App Store. They are available on request via
TestFlight – please open an issue in this repository.

## Inspiration

Solar Visor was inspired by the app AutarkieManager by [OTRKY](https://www.otrky.com/). It is an independent project
and not affiliated with OTRKY.

## Disclaimer

Solar Visor is not affiliated with Home Assistant, E3/DC, KOSTAL or openWB. All product names belong to their owners.
The software is provided as is, without warranty (see the license).

## License

Apache License 2.0 – see [LICENSE](LICENSE) and [NOTICE](NOTICE).
