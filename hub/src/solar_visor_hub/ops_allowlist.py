"""Allowed outbound operations (plan §2.1 "technische Lesesperre", P0 criterion 3).

Every outbound call of the hub must pass these guards. Anything not listed is
rejected at runtime; a static test additionally scans the source tree.
"""

from __future__ import annotations

import re

# Home Assistant WebSocket message types the hub may send.
HA_WS_ALLOWED: frozenset[str] = frozenset(
    {
        "auth",
        "auth/current_user",
        "get_states",
        "subscribe_entities",
        "render_template",
        "unsubscribe_events",
        "ping",
        # home detail sheet (2026-10-06): the HA energy dashboard's consumers and their statistics – both read-only
        "energy/get_prefs",
        "recorder/statistics_during_period",
        # plan ha-addon P2: time zone of the plant – read-only
        "get_config",
        # plan ha-addon P3: history of any plant from the recorder – read-only
        "history/history_during_period",
        "recorder/list_statistic_ids",
        # plan ha-addon P4: web interface – who is an administrator (Ingress), what the sensors are – read-only
        "config/auth/list",
        "config/entity_registry/list",
        "config/device_registry/list",
        "config/area_registry/list",
        # plan ha-remote-access P1: Home Assistant's own addresses (cloud, external) for the apps – read-only
        "network/url",
    }
)

# Fragments that must never appear in hub source code (checked by tests).
FORBIDDEN_SOURCE_FRAGMENTS: tuple[str, ...] = (
    "call_service",
    "/api/services",
    "/api/template",
    "fire_event",
    "execute_script",
    "/api/v2/write",
    "openWB/set",
    # pye3dc write methods – the hub only uses the read getters (RSCP send guard is the runtime check)
    "set_power_limits", "set_powersave", "set_weather_regulated_charge", "set_idle_periods", "set_wallbox",
    "toggle_wallbox", "set_battery_to_car_mode", "set_switch_onoff", "sendWallboxSetRequest", "sendCommand",
)

# InfluxDB: only the v1 query endpoint with read statements.
INFLUX_ALLOWED_PATHS: frozenset[str] = frozenset({"/query", "/health", "/ping"})
_INFLUX_READ_STATEMENT = re.compile(r"^\s*(SELECT|SHOW)\b", re.IGNORECASE)
_INFLUX_FORBIDDEN = re.compile(r"\b(INTO|DELETE|DROP|CREATE|ALTER|GRANT|REVOKE|KILL)\b", re.IGNORECASE)


# E3/DC RSCP (rscp-plan.md): only the requests of the reviewed read getters (pye3dc 0.10.0) plus their index
# parameters; "REQ" in a name proves nothing – the list is explicit. Writes (*_SET_*, commands) are always refused.
RSCP_READ_TAGS: frozenset[str] = frozenset({
    "RSCP_REQ_AUTHENTICATION", "INFO_REQ_UTC_TIME", "INFO_REQ_SERIAL_NUMBER", "INFO_REQ_MAC_ADDRESS", "INFO_REQ_SW_RELEASE",
    "EMS_REQ_AUTARKY", "EMS_REQ_BAT_SOC", "EMS_REQ_POWER_ADD", "EMS_REQ_POWER_BAT", "EMS_REQ_POWER_GRID",
    "EMS_REQ_POWER_HOME", "EMS_REQ_POWER_PV", "EMS_REQ_POWER_WB_ALL", "EMS_REQ_SELF_CONSUMPTION",
    "EMS_REQ_DERATE_AT_PERCENT_VALUE", "EMS_REQ_DERATE_AT_POWER_VALUE", "EMS_REQ_EXT_SRC_AVAILABLE", "EMS_REQ_GET_SYS_SPECS",
    "EMS_REQ_INSTALLED_PEAK_POWER", "EMS_REQ_SYS_STATUS", "EMS_REQ_GET_POWER_SETTINGS",
    "BAT_REQ_DATA", "BAT_REQ_DCB_COUNT", "BAT_REQ_ASOC", "BAT_REQ_CHARGE_CYCLES", "BAT_REQ_CURRENT",
    "BAT_REQ_DCB_ALL_CELL_TEMPERATURES", "BAT_REQ_DCB_ALL_CELL_VOLTAGES", "BAT_REQ_DCB_INFO", "BAT_REQ_DESIGN_CAPACITY",
    "BAT_REQ_DEVICE_NAME", "BAT_REQ_DEVICE_STATE", "BAT_REQ_EOD_VOLTAGE", "BAT_REQ_ERROR_CODE", "BAT_REQ_FCC",
    "BAT_REQ_INTERNALS", "BAT_REQ_MAX_BAT_VOLTAGE", "BAT_REQ_MAX_CHARGE_CURRENT", "BAT_REQ_MAX_DCB_CELL_TEMPERATURE",
    "BAT_REQ_MAX_DISCHARGE_CURRENT", "BAT_REQ_MIN_DCB_CELL_TEMPERATURE", "BAT_REQ_MODULE_VOLTAGE", "BAT_REQ_RC",
    "BAT_REQ_READY_FOR_SHUTDOWN", "BAT_REQ_RSOC", "BAT_REQ_RSOC_REAL", "BAT_REQ_STATUS_CODE", "BAT_REQ_TERMINAL_VOLTAGE",
    "BAT_REQ_TOTAL_DISCHARGE_TIME", "BAT_REQ_TOTAL_USE_TIME", "BAT_REQ_TRAINING_MODE", "BAT_REQ_USABLE_CAPACITY",
    "BAT_REQ_USABLE_REMAINING_CAPACITY",
    "PVI_REQ_AC_MAX_PHASE_COUNT", "PVI_REQ_DATA", "PVI_REQ_TYPE", "PVI_REQ_USED_STRING_COUNT", "PVI_REQ_AC_APPARENTPOWER",
    "PVI_REQ_AC_CURRENT", "PVI_REQ_AC_ENERGY_ALL", "PVI_REQ_AC_ENERGY_GRID_CONSUMPTION", "PVI_REQ_AC_MAX_APPARENTPOWER",
    "PVI_REQ_AC_POWER", "PVI_REQ_AC_REACTIVEPOWER", "PVI_REQ_AC_VOLTAGE", "PVI_REQ_COS_PHI", "PVI_REQ_DC_CURRENT",
    "PVI_REQ_DC_MAX_STRING_COUNT", "PVI_REQ_DC_POWER", "PVI_REQ_DC_STRING_ENERGY_ALL", "PVI_REQ_DC_VOLTAGE",
    "PVI_REQ_DEVICE_STATE", "PVI_REQ_FREQUENCY_UNDER_OVER", "PVI_REQ_LAST_ERROR", "PVI_REQ_MAX_TEMPERATURE",
    "PVI_REQ_MIN_TEMPERATURE", "PVI_REQ_ON_GRID", "PVI_REQ_POWER_MODE", "PVI_REQ_SERIAL_NUMBER", "PVI_REQ_STATE",
    "PVI_REQ_SYSTEM_MODE", "PVI_REQ_TEMPERATURE", "PVI_REQ_TEMPERATURE_COUNT", "PVI_REQ_VERSION",
    "PVI_REQ_VOLTAGE_MONITORING",
    "PM_REQ_DATA", "PM_REQ_TYPE", "PM_REQ_ACTIVE_PHASES", "PM_REQ_ENERGY_L1", "PM_REQ_ENERGY_L2", "PM_REQ_ENERGY_L3",
    "PM_REQ_MAX_PHASE_POWER", "PM_REQ_MODE", "PM_REQ_POWER_L1", "PM_REQ_POWER_L2", "PM_REQ_POWER_L3",
    "PM_REQ_VOLTAGE_L1", "PM_REQ_VOLTAGE_L2", "PM_REQ_VOLTAGE_L3",
})
RSCP_PARAM_TAGS: frozenset[str] = frozenset(
    {"RSCP_AUTHENTICATION_USER", "RSCP_AUTHENTICATION_PASSWORD", "BAT_INDEX", "PVI_INDEX", "PM_INDEX"})


class ForbiddenOperation(PermissionError):
    pass


def check_rscp(message: object) -> None:
    """Every RSCP message (tag, type, value) incl. nested containers, before it is encoded."""
    if not isinstance(message, tuple) or len(message) != 3:
        raise ForbiddenOperation("RSCP message malformed")
    tag, _type, value = message
    name = getattr(tag, "name", tag)
    if not isinstance(name, str) or "_SET_" in name or "COMMAND" in name or (
            name not in RSCP_READ_TAGS and name not in RSCP_PARAM_TAGS):
        raise ForbiddenOperation(f"RSCP tag not allowed: {name!r}")
    if isinstance(value, list):
        for child in value:
            check_rscp(child)


def check_ha_ws(message_type: str) -> None:
    if message_type not in HA_WS_ALLOWED:
        raise ForbiddenOperation(f"HA WebSocket message type not allowed: {message_type!r}")


def check_influx(path: str, statement: str | None = None) -> None:
    if path not in INFLUX_ALLOWED_PATHS:
        raise ForbiddenOperation(f"InfluxDB path not allowed: {path!r}")
    if path == "/query":
        if not statement or not _INFLUX_READ_STATEMENT.match(statement):
            raise ForbiddenOperation("InfluxQL statement must start with SELECT or SHOW")
        if _INFLUX_FORBIDDEN.search(statement) or ";" in statement:
            raise ForbiddenOperation("InfluxQL statement contains a forbidden keyword or multiple statements")
