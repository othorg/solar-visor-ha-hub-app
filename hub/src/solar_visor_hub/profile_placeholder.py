"""Neutral plant profile of the public package (plan hub-publish P4): no installation's data.

Only keeps the modules of the existing-plant calculation importable – that calculation never runs without a real
profile (``profile.AVAILABLE`` is False: no ``plant_legacy`` mode, no ``mapping`` mode, no legacy import). Generic
plants are described in the web interface and calculated from Home Assistant's recorder and the hub's archive.
"""

from __future__ import annotations

from datetime import date, datetime

from .energy import HoldRule, E3DC_HOLD
from .profile_types import EARLIEST, PlantEvent, SeriesSpec

AVAILABLE = False

# epochs: a data start far in the past, every boundary at it – nothing is plant-specific
T_MIN: datetime = EARLIEST
LEGACY_END: datetime = EARLIEST
DATA_SINCE: date = EARLIEST.date()
SPLIT_SENSORS: datetime = EARLIEST
IOMETER_FROM: datetime = EARLIEST
IOMETER_BASE_FROM: datetime = EARLIEST
DC3_FROM: datetime = EARLIEST
CONSUMPTION_COMPLETE_FROM: datetime = EARLIEST
SOC_STEP_WH = 0.0
OPENWB_COUNTER_EPOCHS: list[datetime] = []
P_MAX: dict[str, float] = {}
SERIES: list[SeriesSpec] = []
SHELLY_WOMO_FROM: datetime = EARLIEST
WOMO_HOLD: HoldRule = E3DC_HOLD
WOMO_SERIES: list[SeriesSpec] = []
DETAIL_SERIES: list[SeriesSpec] = []
EXTRA_SERIES: list[SeriesSpec] = []
PIPELINE_EVIDENCE = ""
EVENTS: tuple[PlantEvent, ...] = ()

# site, names, defaults: none – the location comes from Home Assistant, the rest is set in the web interface
SITE = (0.0, 0.0)
KOSTAL_AC_MEASUREMENT: str | None = None
PLANT_NAMES: dict[str, str] = {}
PV_PEAK_W: float | None = None
STANDALONE_HA_URL = ""
INFLUX_DEFAULT: tuple[str, str, str] = ("", "", "")
RSCP_HOST = ""
