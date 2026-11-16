"""Runtime settings from environment (no secrets in values – only file paths).

Two runtime profiles share one code base (plan ha-addon 4.3):

- ``standalone`` (default): LXC / systemd as before – HA URL and paths from the environment, tokens in files.
- ``addon``: Home Assistant add-on – HA through the Supervisor proxy with ``SUPERVISOR_TOKEN``, state under ``/data``;
  the InfluxDB connection from the web interface (``/data/influx.json``), else from the former add-on options
  (``/data/options.json``, taken over once at start). Selected by ``SOLAR_VISOR_RUNTIME=addon``, which the add-on image
  sets; never guessed from the presence of a token.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import profile
from .config import ConfigError, SecretFiles
from .influx_config import FILE_NAME as INFLUX_FILE
from .influx_config import InfluxConfig, InfluxConfigStore

RUNTIME_ENV_VAR = "SOLAR_VISOR_RUNTIME"
STANDALONE, ADDON = "standalone", "addon"
SUPERVISOR_TOKEN_ENV = "SUPERVISOR_TOKEN"
SUPERVISOR_CORE_URL = "http://supervisor/core"
SUPERVISOR_CORE_WS = "ws://supervisor/core/websocket"      # not <url>/api/websocket: the proxy has its own path


def runtime(env: dict[str, str] | None = None) -> str:
    value = (os.environ if env is None else env).get(RUNTIME_ENV_VAR, STANDALONE)
    if value not in (STANDALONE, ADDON):
        raise ConfigError(f"{RUNTIME_ENV_VAR} must be '{STANDALONE}' or '{ADDON}'")
    return value


INFLUX_URL, INFLUX_DB, INFLUX_RP = profile.INFLUX_DEFAULT   # the plant profile's InfluxDB (empty: none)

@dataclass(frozen=True)
class Settings:
    ha_url: str
    data_dir: Path
    secrets: SecretFiles
    influx_url: str = profile.INFLUX_DEFAULT[0]       # the plant profile's defaults; empty: not configured
    influx_db: str = profile.INFLUX_DEFAULT[1]
    influx_rp: str | None = profile.INFLUX_DEFAULT[2] or None
    runtime: str = STANDALONE
    ha_ws_url: str | None = None        # full WebSocket URL when it is not <ha_url>/api/websocket

    @property
    def devices_db(self) -> Path:
        return self.data_dir / "devices.sqlite3"

    @property
    def journal_db(self) -> Path:
        return self.data_dir / "journal.sqlite3"

    @property
    def mapping_overrides(self) -> Path:
        return self.data_dir / "mapping_overrides.json"

    @property
    def plant_path(self) -> Path:
        return self.data_dir / "plant.json"

    def read_ha_token(self) -> str:
        """Token for Home Assistant: the Supervisor token in the add-on (environment, never stored), else the file.
        Raises like a missing token file so callers treat both the same way."""
        if self.runtime == ADDON:
            token = os.environ.get(SUPERVISOR_TOKEN_ENV, "").strip()
            if not token:
                raise ConfigError("Supervisor token missing (homeassistant_api not granted?)")
            return token
        return self.secrets.read_ha_token()


def addon_options(path: Path) -> dict[str, str]:
    """Add-on options written by the Supervisor (former InfluxDB settings); empty strings count as not set."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"add-on options unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("add-on options must be an object")
    return {k: v.strip() for k, v in raw.items() if isinstance(v, str) and v.strip()}


def _retention_policy(value: str) -> str | None:
    """The add-on options cannot tell an empty field from an unset one, so "none" states a database without policy."""
    return None if value.strip().lower() in ("", "none") else value


def _secret_files(secrets_dir: Path) -> SecretFiles:
    return SecretFiles(ha_token=secrets_dir / "ha_token", influx_token=secrets_dir / "influx_token",
                       # own directory: the only part of the secrets the service may write (app configuration)
                       rscp_user=secrets_dir / "rscp" / "rscp_user",
                       rscp_password=secrets_dir / "rscp" / "rscp_password",
                       rscp_key=secrets_dir / "rscp" / "rscp_key")


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    if runtime(env) == ADDON:
        data_dir = Path(env.get("SOLAR_VISOR_DATA_DIR", "/data"))
        options = addon_options(data_dir / "options.json")

        def pick(option: str, variable: str, default: str) -> str:
            # an option that is set wins; otherwise the environment as in the standalone profile (empty = none)
            return options[option] if option in options else env.get(variable, default)

        # the web interface's setting wins; the former options (or the environment) only until it exists
        influx = InfluxConfigStore(data_dir / INFLUX_FILE).current or InfluxConfig(
            url=pick("influx_url", "SOLAR_VISOR_INFLUX_URL", INFLUX_URL),
            database=pick("influx_db", "SOLAR_VISOR_INFLUX_DB", INFLUX_DB),
            retention_policy=_retention_policy(pick("influx_rp", "SOLAR_VISOR_INFLUX_RP", INFLUX_RP)))
        return Settings(
            ha_url=SUPERVISOR_CORE_URL,
            ha_ws_url=SUPERVISOR_CORE_WS,
            data_dir=data_dir,
            secrets=_secret_files(Path(env.get("SOLAR_VISOR_SECRETS_DIR", str(data_dir / "secrets")))),
            influx_url=influx.url,
            influx_db=influx.database,
            influx_rp=influx.retention_policy,
            runtime=ADDON,
        )
    return Settings(
        ha_url=env.get("SOLAR_VISOR_HA_URL", profile.STANDALONE_HA_URL),
        data_dir=Path(env.get("SOLAR_VISOR_DATA_DIR", "/var/lib/solar-visor")),
        secrets=_secret_files(Path(env.get("SOLAR_VISOR_SECRETS_DIR", "/etc/solar-visor/secrets"))),
        influx_url=env.get("SOLAR_VISOR_INFLUX_URL", INFLUX_URL),
        influx_db=env.get("SOLAR_VISOR_INFLUX_DB", INFLUX_DB),
        influx_rp=env.get("SOLAR_VISOR_INFLUX_RP", INFLUX_RP) or None,
    )
