"""The plant profile in use (plan hub-publish P4): ``own_profile`` where it exists (the author's private package),
otherwise the neutral ``profile_placeholder`` of the public package. Every module reads plant data from here or from
the façades ``epochs``/``plant_events``; ``AVAILABLE`` gates the existing-plant calculation (fail closed)."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType


def _load() -> ModuleType:
    try:
        return import_module(".own_profile", __package__)
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.own_profile":       # a broken own profile must fail, not fall back
            raise
        return import_module(".profile_placeholder", __package__)


_p = _load()

AVAILABLE: bool = _p.AVAILABLE
T_MIN = _p.T_MIN
LEGACY_END = _p.LEGACY_END
DATA_SINCE = _p.DATA_SINCE
SPLIT_SENSORS = _p.SPLIT_SENSORS
IOMETER_FROM = _p.IOMETER_FROM
IOMETER_BASE_FROM = _p.IOMETER_BASE_FROM
DC3_FROM = _p.DC3_FROM
CONSUMPTION_COMPLETE_FROM = _p.CONSUMPTION_COMPLETE_FROM
SOC_STEP_WH: float = _p.SOC_STEP_WH
OPENWB_COUNTER_EPOCHS = _p.OPENWB_COUNTER_EPOCHS
P_MAX: dict[str, float] = _p.P_MAX
SERIES = _p.SERIES
SHELLY_WOMO_FROM = _p.SHELLY_WOMO_FROM
WOMO_HOLD = _p.WOMO_HOLD
WOMO_SERIES = _p.WOMO_SERIES
DETAIL_SERIES = _p.DETAIL_SERIES
EXTRA_SERIES = _p.EXTRA_SERIES
PIPELINE_EVIDENCE: str = _p.PIPELINE_EVIDENCE
EVENTS = _p.EVENTS
SITE: tuple[float, float] = _p.SITE
KOSTAL_AC_MEASUREMENT: str | None = _p.KOSTAL_AC_MEASUREMENT
PLANT_NAMES: dict[str, str] = _p.PLANT_NAMES
PV_PEAK_W: float | None = _p.PV_PEAK_W
STANDALONE_HA_URL: str = _p.STANDALONE_HA_URL
INFLUX_DEFAULT: tuple[str, str, str] = _p.INFLUX_DEFAULT
RSCP_HOST: str = _p.RSCP_HOST
