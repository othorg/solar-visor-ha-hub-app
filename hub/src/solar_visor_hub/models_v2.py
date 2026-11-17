"""API contract v2 (plan ha-addon 4.6, P2 detail plan §4.2): everything is described by the plant's modules instead
of the fixed fields of one plant. The classes have their own names so that the v1 schema stays untouched while both
contracts live side by side (v1 goes at the end of P5).

Rules as in v1: timestamps UTC, power in W, energy in Wh, unknown values are ``None`` – never 0.
"""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import Field, field_validator

from .models import (CompareAxisPoint, CompareBasis, CompareBasisOption, CompareExclusion, CompareMetric, CompareNote,
                     CompareSide, EnergyRing, FlowEdge, MetricState, PowerPoint, TimeInterval, Unit, UtcDatetime, WidgetLive,
                     WidgetSoc, WidgetSparkline, WidgetWindow, _Strict)

API_VERSION_2 = "v2"


class PointRole(StrEnum):
    AUTHORITATIVE = "authoritative"
    SUBSET = "subset"
    CROSS_CHECK = "cross_check"
    DETAIL = "detail"
    DERIVED = "derived"              # computed by the hub, never in plant.json


class PlantMetric(_Strict):
    metric_id: str = Field(description="<module>.<point>, e.g. 'pv_1.power' or 'pv_2.string_1.voltage'")
    module: str
    value: float | bool | str | None
    unit: Unit
    role: PointRole
    state: MetricState
    reasons: list[str] = Field(default_factory=list)
    derived: bool = False
    input_metric_ids: list[str] = Field(default_factory=list)
    source_changed_at: UtcDatetime | None = None
    reported_at: UtcDatetime | None = Field(
        default=None, description="Last report of the source (derived values: the oldest of their inputs)")
    observed_at: UtcDatetime

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float | bool | str | None) -> float | bool | str | None:
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("non-finite values must be represented as null")
        return v


class PlantModuleStatus(_Strict):
    state: MetricState = Field(description="Worst state of the module's balance-relevant points")
    last_seen_at: UtcDatetime | None = None


class PlantSnapshot(_Strict):
    snapshot_id: str
    generated_at: UtcDatetime
    plant_revision: int
    connected: bool = Field(description="Home Assistant connection of the hub")
    metrics: dict[str, PlantMetric]
    edges: list[FlowEdge] = Field(description="edge_id = node on the module side; nodes: module ids plus "
                                              "'generation', 'bus', 'consumption', 'home'. A PV edge ends at "
                                              "'generation', one with full feed-in at 'grid' (+ into the grid)")
    balance_residual_w: float | None = Field(
        description="Sources − sinks of the home in W (without full feed-in), never redistributed; null without a "
                    "measured total consumption")
    balance_comparable: bool = Field(description="False if the inputs are more than 15 s apart")
    modules: dict[str, PlantModuleStatus]


# --- capabilities ------------------------------------------------------------------------------------------------

class PlantPointInfo(_Strict):
    id: str = Field(description="Point within its module or string, e.g. 'power', 'soc'")
    unit: Unit
    role: PointRole


class PlantStringInfo(_Strict):
    id: str = Field(description="'string_1' … – metric ids are <module>.<string>.<point>")
    name: str
    points: list[PlantPointInfo]


class PlantModuleInfo(_Strict):
    """A module that is switched on. What is switched off does not appear at all."""

    id: str = Field(description="Stable slot id: grid, pv_1…pv_3, battery, wallbox, generic_1…generic_3, consumption")
    kind: str = Field(description="grid | pv | battery | wallbox | generic | consumption")
    name: str = Field(description="Name chosen by the user; changing it changes no id")
    balance: str | None = Field(default=None, description="generic: 'part_of_home' or 'beside_home'; pv: "
                                                          "'full_feed_in' (own meter straight into the grid, never "
                                                          "part of the home balance), null = feeds the home")
    icon: str | None = Field(default=None, description="Dashboard symbol: solar, house, battery, grid, wallbox, camper, plug (the chosen one or the default of the kind; none for the measured consumption). Apps fall back to the kind for an unknown value.")
    points: list[PlantPointInfo]
    strings: list[PlantStringInfo] = Field(default_factory=list)


class PlantFeatures(_Strict):
    rscp: bool = False              # GET /v2/rscp served and switched on (E3/DC detail values)
    history: bool = False           # energy and power history (P3)
    compare: bool = False
    consumers: bool = False
    widget: bool = False


class PlantCapabilities(_Strict):
    api_version: str
    hub_version: str
    plant_revision: int
    timezone: str | None = Field(description="Time zone of the plant from Home Assistant (IANA name); null until the "
                                             "hub has read it")
    modules: list[PlantModuleInfo]
    metrics: list[str] = Field(description="Every metric id the snapshot carries, including the calculated ones")
    features: PlantFeatures
    pv_peak_w: float | None = Field(None, description="Installed power of the PV generators of the home in W, "
                                                      "without full feed-in (scale of gauges); null when the user "
                                                      "has not entered it")
    rscp_pv: str | None = Field(None, description="PV module whose strings are the E3/DC trackers of /v2/rscp")


# --- diagnostics (no secrets, no entity names) -------------------------------------------------------------------

class PlantModuleDiagnostics(_Strict):
    module_id: str
    state: MetricState
    last_seen_at: UtcDatetime | None = None
    newest_report_age_s: float | None = None
    oldest_report_age_s: float | None = None
    discarded_events: int = 0


class PlantTrust(_Strict):
    module_id: str
    last_evidence_at: UtcDatetime | None = None
    counter_anomalies: int = 0


class PlantProblem(_Strict):
    metric_id: str
    state: MetricState
    reasons: list[str] = Field(default_factory=list)


class PlantJournalItem(_Strict):
    at: UtcDatetime
    kind: str
    subject: str
    state: str | None = None
    detail: str | None = None


class PlantDiagnostics(_Strict):
    hub_version: str
    generated_at: UtcDatetime
    ha_connected: bool
    ha_disconnected_since: UtcDatetime | None = None
    plant_revision: int
    timezone: str | None = None
    modules: list[PlantModuleDiagnostics]
    problems: list[PlantProblem]
    trust: list[PlantTrust] = Field(default_factory=list)
    journal: list[PlantJournalItem] = Field(default_factory=list)


# --- energy history (P3 step 5) ----------------------------------------------------------------------------------

class PlantEnergyMethod(StrEnum):
    COUNTER_DELTA = "counter_delta"
    SAMPLE_HOLD = "sample_hold"
    STATISTIC_MEAN = "statistic_mean"     # time-weighted mean of Home Assistant's statistics
    DERIVED = "derived"                   # formed from other categories of the plant


class PlantEnergyBucket(_Strict):
    start: UtcDatetime
    end: UtcDatetime
    local_label: str = Field(description="day 'HH:MM', week 6-h block 'YYYY-MM-DDTHH:00', month 'YYYY-MM-DD', "
                                         "year ISO week 'YYYY-Www'")
    utc_offset_minutes: int
    energy_wh: float | None = Field(description="Known energy of the bucket; null = unknown or no source, never 0")
    method: PlantEnergyMethod
    coverage_fraction: float = Field(ge=0.0, le=1.0)
    complete: bool
    provisional: bool = False
    unresolved_wh_max: float | None = Field(default=None, description="Upper bound of the unresolved share")
    source_resolution_s: int | None = Field(
        default=None, description="Resolution of the data the bucket was computed from (300 / 3600 s); null for raw "
                                  "states and for the existing calculation")
    allocation: str = Field(description="'observed' or 'time_allocated' (an hour spread over its quarter hours)")
    calculation_version: int
    quality_reasons: list[str] = Field(default_factory=list)


class PlantEnergySource(_Strict):
    method: str = Field(description="statistic_mean | counter_delta | sample_hold | derived | existing_calculation")
    metric_id: str | None = None
    resolution_s: int | None = None
    mixed: bool = Field(default=False, description="different sources within the period")
    note: str | None = Field(default=None, description="e.g. the grid meter of the existing calculation")


class PlantEnergyTotals(_Strict):
    energy_wh: dict[str, float | None]
    coverage: dict[str, float | None] = Field(default_factory=dict)
    unresolved_wh_max: dict[str, float] = Field(default_factory=dict)
    sources: dict[str, PlantEnergySource] = Field(default_factory=dict)


class PlantEnergyKpis(_Strict):
    autarky_pct: float | None
    self_consumption_pct: float | None
    autarky_coverage: float | None = None
    self_consumption_coverage: float | None = None
    direct_generation_side_wh: float | None = None
    direct_consumption_side_wh: float | None = None
    balance_residual_wh: float | None = Field(
        default=None, description="Sources − sinks of measured sums; null when the consumption is calculated")
    residual_warning: bool = False
    cross_check_residual_wh: float | None = Field(default=None, description="same balance with a second grid meter")
    generation_ring: EnergyRing | None = None
    consumption_ring: EnergyRing | None = None


class PlantEnergyPeriod(_Strict):
    period: str
    local_date: str
    timezone: str
    plant_revision: int
    calculation_version: int
    buckets: dict[str, list[PlantEnergyBucket]] = Field(
        description="Categories under v2 ids: '<module>' (pv_1, wallbox, generic_1), 'generation' (generation in "
                    "the home balance), 'consumption', 'home', '<module>.<direction>' (grid.import, battery.charge), "
                    "'<module>.net' (balance of a signed power), 'supply.<source>' (origin of the consumption; no "
                    "generator with full feed-in), 'full_feed_in' (sum of the generators with full feed-in, only "
                    "when the plant of the day has one), '<module>.string_<n>'")
    totals: PlantEnergyTotals
    kpis: PlantEnergyKpis
    complete: bool
    refreshing: bool = False


# --- power history (P3 step 6) -----------------------------------------------------------------------------------

class PlantPowerSeries(_Strict):
    series_id: str = Field(description="Id of the live metric with the same meaning and sign, e.g. 'battery.power' "
                                        "(+ charging), 'grid.power' (+ import), 'battery.soc'")
    unit: Unit
    derived: bool = False
    resolution_s: int
    source: str = Field(description="statistic_mean | existing_calculation")
    points: list[PowerPoint]
    outages: list[TimeInterval] = Field(default_factory=list)


class PlantPowerResponse(_Strict):
    start: UtcDatetime
    end: UtcDatetime
    timezone: str
    calculation_version: int
    series: list[PlantPowerSeries]
    monitoring_gaps: list[TimeInterval] = Field(default_factory=list)
    journal_since: UtcDatetime | None = None


# --- widget snapshot (P3 step 7) ---------------------------------------------------------------------------------

class PlantWidgetToday(_Strict):
    local_date: str
    energy_wh: dict[str, float | None] = Field(description="Today's energy under the v2 category ids")
    coverage: dict[str, float | None]
    autarky_pct: float | None
    self_consumption_pct: float | None
    autarky_coverage: float | None = None
    self_consumption_coverage: float | None = None
    complete: bool


class PlantWidgetSnapshot(_Strict):
    generated_at: UtcDatetime
    timezone: str
    plant_revision: int
    calculation_version: int
    live: dict[str, WidgetLive] = Field(description="Live metric ids (generation.power, grid.power, battery.soc …)")
    window: WidgetWindow | None = Field(default=None, description="Last completed quarter hour; values under the live "
                                                                  "metric ids with the live sign")
    soc: WidgetSoc | None = None
    today: PlantWidgetToday | None = None
    sparkline: WidgetSparkline | None = None


# --- consumers of the HA energy dashboard (P3 step 9) ------------------------------------------------------------

class PlantConsumer(_Strict):
    consumer_id: str = Field(description="HA energy statistic of the consumer (stable identity)")
    name: str
    kind: str = Field(description="'device' (a consumer of the HA energy dashboard) or 'module' (a module of the plant)")
    module: str | None = Field(default=None, description="module id for kind 'module' (wallbox, generic_1 …)")
    power_w: float | None
    power_state: str = Field(description="ok | stale | unknown | assumed")
    power_reported_at: UtcDatetime | None = None
    energy_today_wh: float | None
    energy_reason: str | None = None


class PlantConsumersRest(_Strict):
    value: float | None
    reason: str | None = None
    unmeasured: int = 0


class PlantConsumers(_Strict):
    available: bool
    reason: str | None = None
    generated_at: UtcDatetime
    local_date: str
    stats_from: UtcDatetime
    stats_until: UtcDatetime | None
    consumption_w: float | None
    consumption_today_wh: float | None
    consumers: list[PlantConsumer]
    untracked_power: PlantConsumersRest
    untracked_energy: PlantConsumersRest


# --- comparison of two periods (P3 step 8) -----------------------------------------------------------------------

class PlantComparison(_Strict):
    """Like the v1 comparison without the balance profile; metric ids are v2 category ids (pv_1, grid.import …) or
    the KPIs (autarky, self_consumption)."""
    kind: str
    basis: CompareBasis
    default_basis: CompareBasis
    available_bases: list[CompareBasisOption]
    timezone: str
    calculation_version: int
    generated_at: UtcDatetime
    cut: UtcDatetime | None
    aligned_end_a: UtcDatetime | None = None
    aligned_end_b: UtcDatetime | None = None
    no_window: bool
    refreshing: bool = False
    a: CompareSide
    b: CompareSide
    axis: list[CompareAxisPoint]
    metrics: list[CompareMetric]
    excluded: list[CompareExclusion]
    notes: list[CompareNote]
