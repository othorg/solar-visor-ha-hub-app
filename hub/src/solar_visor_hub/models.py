"""Canonical data model and API contract (plan §3.1, §4.1).

Rules: timestamps are UTC (timezone-aware), power in W, energy in Wh,
unknown values are ``None`` (never 0, never NaN).
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, field_validator


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


# Timezone-aware timestamps only (naive values are rejected), normalised to UTC.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


class Unit(StrEnum):
    W = "W"
    WH = "Wh"
    V = "V"
    A = "A"
    PERCENT = "%"
    BOOL = "bool"
    TEXT = "text"


class MetricState(StrEnum):
    OK = "ok"
    STALE = "stale"
    UNKNOWN = "unknown"
    IMPLAUSIBLE = "implausible"


class Confidence(StrEnum):
    OBSERVED = "observed"
    RECONSTRUCTED = "reconstructed"
    ASSUMED = "assumed"


class TimestampKind(StrEnum):
    HA_REPORTED = "ha_reported"
    HISTORY_POINT = "history_point"
    DEVICE = "device"


class BalanceProfile(StrEnum):
    E3DC_BALANCE = "e3dc_balance"
    KOSTAL_AC_COMPARISON = "kostal_ac_comparison"


class EnergyMethod(StrEnum):
    COUNTER_DELTA = "counter_delta"
    SAMPLE_HOLD = "sample_hold"


class Allocation(StrEnum):
    OBSERVED = "observed"
    TIME_ALLOCATED = "time_allocated"
    UNRESOLVED = "unresolved"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class MetricPoint(_Strict):
    metric_id: str
    value: float | bool | str | None
    unit: Unit
    source_id: str
    source_entity: str | None = None
    measurement_boundary: str
    source_epoch_id: str | None = None
    source_changed_at: UtcDatetime | None = None
    reported_at: UtcDatetime | None = Field(
        default=None, description="Last report to Home Assistant – not a device measurement time"
    )
    observed_at: UtcDatetime
    timestamp_kind: TimestampKind = TimestampKind.HA_REPORTED
    valid_until: UtcDatetime | None = None
    state: MetricState
    derived: bool = False
    confidence: Confidence = Confidence.OBSERVED
    reasons: list[str] = Field(default_factory=list)
    input_metric_ids: list[str] = Field(default_factory=list)
    mapping_revision: int = 1

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float | bool | str | None) -> float | bool | str | None:
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("non-finite values must be represented as null")
        return v


class FlowEdge(_Strict):
    """Bus-model edge; ``power_w`` is signed relative to the edge's positive direction (v2: full feed-in ends at grid)."""

    edge_id: str
    from_node: str
    to_node: str
    power_w: float | None
    state: MetricState
    derived: bool = False


class SourceStatus(_Strict):
    source_id: str
    state: MetricState
    last_seen_at: UtcDatetime | None = None
    detail: str | None = None


class Snapshot(_Strict):
    snapshot_id: str
    generated_at: UtcDatetime
    balance_profile: BalanceProfile
    metrics: dict[str, MetricPoint]
    edges: list[FlowEdge]
    balance_residual_w: float | None = Field(description="Sources − sinks in W; never redistributed")
    balance_comparable: bool = Field(description="False if inputs are more than 15 s apart")
    sources: dict[str, SourceStatus]


class EnergyBucket(_Strict):
    start: UtcDatetime
    end: UtcDatetime
    local_label: str = Field(description="Local key of the bucket: day 'HH:MM', week 6-h block 'YYYY-MM-DDTHH:00', "
                             "month 'YYYY-MM-DD', year ISO week 'YYYY-Www' (edge weeks cut at the year)")
    utc_offset_minutes: int
    energy_wh: float | None
    method: EnergyMethod
    coverage_fraction: float = Field(ge=0.0, le=1.0)
    complete: bool
    provisional: bool = False
    unresolved_wh_max: float | None = Field(default=None, description="Upper bound of unresolved share")
    source_resolution_s: int | None = None
    allocation: Allocation = Allocation.OBSERVED
    balance_profile: BalanceProfile = BalanceProfile.E3DC_BALANCE
    calculation_version: int = 1
    quality_reasons: list[str] = Field(default_factory=list)


class EnergyTotals(_Strict):
    pv_internal_wh: float | None
    external_wh: float | None
    generation_wh: float | None
    consumption_wh: float | None
    wallbox_wh: float | None
    home_wh: float | None
    battery_charge_wh: float | None
    battery_discharge_wh: float | None
    grid_import_wh: float | None
    grid_export_wh: float | None
    grid_source: str = Field(description="e.g. 'iometer' or 'e3dc' (epoch-dependent)")
    # per category (pv_internal … grid_export, generation, home): covered / applicable seconds of the elapsed
    # period (None = no source); combinations use the guaranteed joint coverage
    coverage: dict[str, float | None] = Field(default_factory=dict)
    unresolved_wh_max: dict[str, float] = Field(default_factory=dict)


class RingState(StrEnum):
    OK = "ok"
    UNCERTAIN = "uncertain"            # |balance residual| > threshold
    UNRESOLVABLE = "unresolvable"      # unclamped direct share < −threshold: allocation impossible
    INCOMPLETE = "incomplete"          # guaranteed joint coverage of the ring inputs < full
    NONE = "none"                      # no data


class EnergyRing(_Strict):
    """Allocation of the period totals (plan §3.5): generation = direct + battery charge + export, or
    consumption = direct + battery discharge + import. The client renders, it does not re-derive rules."""
    state: RingState
    total_wh: float
    direct_wh: float                   # clamped ≥ 0 (display)
    direct_raw_wh: float               # unclamped, signed
    battery_wh: float
    grid_wh: float
    coverage: float = Field(ge=0.0, le=1.0, description="guaranteed joint coverage of all ring inputs")
    threshold_wh: float


class EnergyKpis(_Strict):
    autarky_pct: float | None
    self_consumption_pct: float | None
    direct_generation_side_wh: float | None
    direct_consumption_side_wh: float | None
    balance_residual_wh: float | None
    residual_warning: bool
    # share of the period in which all inputs of the KPI were fully covered (same valid period, plan §3.5 K1)
    autarky_coverage: float | None = None
    self_consumption_coverage: float | None = None
    e3dc_balance_residual_wh: float | None = None   # same balance with the E3/DC grid measurement
    generation_ring: EnergyRing | None = None
    consumption_ring: EnergyRing | None = None


class EnergyPeriod(_Strict):
    period: str
    local_date: str
    timezone: str = "Europe/Berlin"
    balance_profile: BalanceProfile
    buckets: dict[str, list[EnergyBucket]]
    totals: EnergyTotals
    kpis: EnergyKpis
    complete: bool
    # some days were served from an older cached result and are being recomputed: request again shortly
    refreshing: bool = False


# --- period comparison (comparison plan 2026-10-06) ---------------------------------------------------------------

class CompareBasis(StrEnum):
    ALIGNED = "aligned"          # only paired, elapsed sections of both periods (same calendar position / clock time)
    TOTALS = "totals"            # actual sums (a running period: so far)
    PER_DAY = "per_day"          # sum of the calendar-complete days ÷ their number


class CompareTier(StrEnum):
    RELIABLE = "reliable"
    UNCHANGED = "unchanged"      # complete, small uncertainty and |change| within the tolerance
    LIMITED = "limited"          # gaps (coverage ≥ 0.95) but the direction is certain within the uncertainty bounds
    NOT_RELIABLE = "not_reliable"


class CompareValue(_Strict):
    value: float | None = Field(description="Wh (per_day: Wh per day) or % for ratios; null = unknown, never 0")
    coverage: float | None = Field(default=None, ge=0.0, le=1.0,
                                   description="covered / expected seconds; solar measures: daylight seconds only")
    unresolved_wh: float | None = Field(default=None, description="upper bound of the unresolved energy (energy only)")
    complete: bool = False
    reconstructed: bool = False
    reason: str | None = Field(default=None, description="why the value is null: no_data | no_daylight_data | "
                               "no_source | zero_denominator | no_window")


class CompareDelta(_Strict):
    abs: float | None = Field(description="A − B in the unit of the values (ratios: percentage points); null when not "
                              "reliable")
    rel_pct: float | None = Field(default=None, description="100 · (A − B) / B, energy only, B > 0")
    tier: CompareTier
    reason: str | None = Field(default=None, description="missing | gaps | refreshing | reference_zero | "
                               "negative_value")


class CompareSeries(_Strict):
    """Per axis position (parallel arrays)."""
    a: list[float | None]
    b: list[float | None]
    coverage_a: list[float | None]
    coverage_b: list[float | None]
    tier: list[CompareTier]
    delta: list[float | None]


class CompareMetric(_Strict):
    metric_id: str
    kind: Literal["energy", "ratio"]
    unit: Unit
    supplementary: bool = Field(default=False, description="only for the data basis sheet (E3/DC grid values when "
                                "the grid meters of the periods differ)")
    a: CompareValue
    b: CompareValue
    delta: CompareDelta
    comparability: Literal["comparable", "limited"]
    comparability_reasons: list[str] = Field(default_factory=list,
                                             description="plant event ids, 'reconstructed'")
    totals_a: CompareValue = Field(description="actual sum of period A (running: so far)")
    totals_b: CompareValue
    per_day_a: CompareValue | None = None
    per_day_b: CompareValue | None = None
    series: CompareSeries


class CompareSide(_Strict):
    first_day: date
    end_day: date = Field(description="exclusive")
    start: UtcDatetime
    end: UtcDatetime
    days: int
    complete_days: int
    running: bool
    grid_source: str


class CompareAxisPoint(_Strict):
    key: str = Field(description="day 'HH:MM' ('HH:MM#2' second occurrence on the 25-h day), week ISO weekday '1'–'7', "
                     "month day '1'–'31', year month '1'–'12', range day index '1'… or block 'B1'…")
    a_start: UtcDatetime | None = None
    a_end: UtcDatetime | None = None
    b_start: UtcDatetime | None = None
    b_end: UtcDatetime | None = None
    a_days: int = 0
    b_days: int = 0
    provisional_a: bool = False
    provisional_b: bool = False


class CompareExclusion(_Strict):
    side: Literal["a", "b"]
    start: UtcDatetime
    end: UtcDatetime
    reason: str = Field(description="no_partner | beyond_common_length | dst_missing_hour | dst_repeated_hour | after_cut")


class CompareBasisOption(_Strict):
    basis: CompareBasis
    available: bool
    reason: str | None = None


class CompareNote(_Strict):
    code: str
    metrics: list[str] = Field(default_factory=list)
    at: UtcDatetime | None = None
    until: UtcDatetime | None = None
    side: Literal["a", "b"] | None = None
    detail: str | None = None


class EnergyComparison(_Strict):
    kind: Literal["day", "week", "month", "year", "range"]
    basis: CompareBasis
    default_basis: CompareBasis
    available_bases: list[CompareBasisOption]
    timezone: str = "Europe/Berlin"
    balance_profile: BalanceProfile
    calculation_version: int
    generated_at: UtcDatetime
    cut: UtcDatetime | None = Field(description="common cut of running periods (exclusive)")
    aligned_end_a: UtcDatetime | None = Field(default=None, description="running: end of the last paired section of A "
                                              "(what 'same point' means; a day without partner is not included)")
    aligned_end_b: UtcDatetime | None = None
    no_window: bool = Field(description="a running comparison without any elapsed paired section yet")
    refreshing: bool = False
    a: CompareSide
    b: CompareSide
    axis: list[CompareAxisPoint]
    metrics: list[CompareMetric]
    excluded: list[CompareExclusion]
    notes: list[CompareNote]


# --- home detail: consumers of the HA energy dashboard (2026-10-06) -----------------------------------------------

class HomeConsumer(_Strict):
    consumer_id: str = Field(description="HA energy statistic of the consumer (stable identity)")
    name: str
    kind: Literal["device", "wallbox", "womo"]
    power_w: float | None = Field(description="now; null = unknown, never 0")
    power_state: Literal["ok", "stale", "unknown", "assumed"] = Field(
        description="assumed: untrusted wallbox shown as 0 W like the dashboard (user decision 2026-10-05)")
    power_reported_at: UtcDatetime | None = None
    energy_today_wh: float | None = Field(description="today up to stats_until; null = unknown")
    energy_reason: str | None = Field(default=None, description="no_statistics | counter_reset | incomplete")


class ConsumersRest(_Strict):
    value: float | None = Field(description="consumption − Σ measured consumers (HA's untracked consumption)")
    reason: str | None = Field(default=None, description="total_unknown | total_incomplete | sum_exceeds_total | "
                               "within_tolerance | no_statistics")
    unmeasured: int = Field(default=0, description="consumers without a value – their consumption is in the rest")


class HomeConsumers(_Strict):
    available: bool
    reason: str | None = Field(default=None, description="ha_disconnected | no_catalog | not_configured")
    generated_at: UtcDatetime
    local_date: date
    stats_from: UtcDatetime
    stats_until: UtcDatetime | None = Field(description="common end of the hourly HA statistics of today")
    consumption_w: float | None
    consumption_today_wh: float | None = Field(description="energy engine, same period as the statistics")
    consumers: list[HomeConsumer]
    untracked_power: ConsumersRest
    untracked_energy: ConsumersRest


class CompareCapabilities(_Strict):
    kinds: list[str]
    bases: list[CompareBasis]
    max_days: int
    data_since: date


class Capabilities(_Strict):
    api_version: str
    hub_version: str
    components: list[str]
    metrics: list[str]
    balance_profiles: list[BalanceProfile]
    kostal_strings: dict[str, str] = Field(default_factory=dict)  # mppt1 → "Garage" (display names, mapping)
    e3dc_strings: dict[str, str] = Field(default_factory=dict)    # string1 → "Dach Ost"
    rscp: bool = False                                            # GET /v1/rscp served (E3/DC detail values)
    max_series_per_request: int = 16
    max_points_per_series: int = 2000
    max_power_range_days: int = 31
    max_energy_range: str = "1 calendar year"
    compare: CompareCapabilities | None = None                    # GET /v1/compare served
    consumers: bool = False                                       # GET /v1/consumers served


# --- diagnostics / config (plan §4.1; no secrets, no URLs) ----------------------------------------

class SourceDiagnostics(_Strict):
    source_id: str
    state: MetricState
    stale_after_s: int
    last_seen_at: UtcDatetime | None = None
    newest_report_age_s: float | None = None
    oldest_report_age_s: float | None = None
    discarded_events: int = 0


class MetricProblem(_Strict):
    metric_id: str
    state: MetricState
    reasons: list[str] = Field(default_factory=list)


class JournalItem(_Strict):
    at: UtcDatetime
    kind: str
    subject: str
    state: str | None = None
    detail: str | None = None


class WallboxTrust(_Strict):
    last_evidence_at: UtcDatetime | None = None
    counter_anomalies: int = 0


class Diagnostics(_Strict):
    hub_version: str
    generated_at: UtcDatetime
    ha_connected: bool
    ha_disconnected_since: UtcDatetime | None = None
    mapping_revision: int
    sources: list[SourceDiagnostics]
    problems: list[MetricProblem]
    wallbox_trust: WallboxTrust
    journal: list[JournalItem] = Field(default_factory=list)


class MetricConfig(_Strict):
    metric_id: str
    source_id: str
    entity: str
    unit: Unit
    sign: int
    measurement_boundary: str
    nonnegative: bool = False
    counter: bool = False


class SourceConfigView(_Strict):
    source_id: str
    kind: str
    stale_after_s: int
    profile: str | None = None


class ConfigView(_Strict):
    mapping_revision: int
    sources: list[SourceConfigView]
    metrics: list[MetricConfig]
    derived_metrics: list[str]
    kostal_strings: dict[str, str] = Field(default_factory=dict)
    e3dc_strings: dict[str, str] = Field(default_factory=dict)


# --- power history / jobs (P3) ---------------------------------------------------------------------

class PowerPoint(_Strict):
    """Statistics of one column [t, t + resolution) – never a single measurement."""
    t: UtcDatetime                    # start of the column
    mean: float | None                # time-weighted over the covered part of the column
    min: float | None                 # extremes of the validated values inside the column
    max: float | None
    coverage: float = Field(ge=0.0, le=1.0, description="covered / applicable seconds")
    estimated: float = Field(0.0, ge=0.0, le=1.0, description="share of the covered time that is estimated")
    applicable: float = Field(1.0, ge=0.0, le=1.0, description="share of the column with a source (0 = future)")


class TimeInterval(_Strict):
    start: UtcDatetime
    end: UtcDatetime


class PowerSeries(_Strict):
    series_id: str
    unit: Unit
    derived: bool = False             # computed from other series (generation, home)
    resolution_s: int
    points: list[PowerPoint]
    outages: list[TimeInterval] = Field(default_factory=list, description="source unavailable per journal")


class PowerResponse(_Strict):
    start: UtcDatetime
    end: UtcDatetime
    timezone: str = "Europe/Berlin"
    calculation_version: int
    series: list[PowerSeries]
    monitoring_gaps: list[TimeInterval] = Field(default_factory=list, description="hub not running")
    journal_since: UtcDatetime | None = None


class JobStatus(_Strict):
    job_id: str
    state: str                        # running | done | failed
    progress: float = Field(ge=0.0, le=1.0)
    detail: str | None = None


class AvailabilityInterval(_Strict):
    start: UtcDatetime
    end: UtcDatetime
    state: MetricState
    reasons: list[str] = Field(default_factory=list)


class AvailabilitySeries(_Strict):
    metric_id: str
    intervals: list[AvailabilityInterval]


class AvailabilityResponse(_Strict):
    start: UtcDatetime
    end: UtcDatetime
    journal_since: UtcDatetime | None = None      # before this, no availability record exists
    series: list[AvailabilitySeries]
    monitoring_gaps: list[TimeInterval] = Field(default_factory=list, description="hub not running: state unknown")



# --- widget / complication snapshot (P6, plan §4.1) ---------------------------------------------------------------

class WidgetLive(_Strict):
    value: float | None
    state: MetricState
    observed_at: UtcDatetime | None = Field(None, description="measurement/report time of the value")


class WidgetWindowValue(_Strict):
    mean_w: float | None
    coverage: float = Field(ge=0.0, le=1.0)
    measured_until: UtcDatetime | None = Field(None, description="end of the last valid measurement inside the window")


class WidgetWindow(_Strict):
    """Last completed, aligned quarter hour. Signs like the live snapshot: battery + = charging, grid + = import."""
    start: UtcDatetime
    end: UtcDatetime
    values: dict[str, WidgetWindowValue]


class WidgetSoc(_Strict):
    value: float
    observed_at: UtcDatetime


class WidgetToday(_Strict):
    local_date: str
    energy_wh: dict[str, float | None]
    coverage: dict[str, float | None]
    autarky_pct: float | None
    self_consumption_pct: float | None
    autarky_coverage: float | None = None
    self_consumption_coverage: float | None = None
    complete: bool
    # motorhome shore power today (part of home – shown as „davon WoMo“), known part over the covered time
    womo_wh: float | None = None
    womo_coverage: float | None = None


class WidgetSparkline(_Strict):
    start: UtcDatetime
    resolution_s: int
    generation_w: list[float | None]
    consumption_w: list[float | None]


class WidgetSnapshot(_Strict):
    generated_at: UtcDatetime
    timezone: str = "Europe/Berlin"
    calculation_version: int
    live: dict[str, WidgetLive]
    window: WidgetWindow | None = None
    soc: WidgetSoc | None = None
    today: WidgetToday | None = None
    sparkline: WidgetSparkline | None = None


# --- E3/DC RSCP detail values (rscp-plan.md) ---------------------------------------------------------------------

RscpState = Literal["disabled", "not_configured", "idle", "connecting", "connected", "degraded", "error", "auth_failed"]


class RscpValue(_Strict):
    id: str
    label: str
    value: bool | int | float | str | None
    unit: str | None = None          # W, V, A, VA, var, %, °C, Wh, Ah, count, code, bool, text


class RscpItem(_Strict):
    """A battery, battery module, string, phase or meter; ``details`` = cell voltages/temperatures (expandable)."""
    id: str
    title: str
    values: list[RscpValue]
    details: list[RscpValue] = Field(default_factory=list)


class RscpGroup(_Strict):
    id: str
    title: str
    observed_at: UtcDatetime
    stale: bool                      # older than 3 × its interval
    error: str | None = None         # not_available / unreadable: the last known values stay, marked
    values: list[RscpValue]
    items: list[RscpItem] = Field(default_factory=list)


class RscpDevice(_Strict):
    model: str | None = None
    firmware: str | None = None
    serial_masked: str | None = None  # first 4 and last 4 characters only


class RscpView(_Strict):
    schema_version: int = 1
    state: RscpState
    enabled: bool
    power_interval_s: int
    system_interval_s: int
    last_attempt: UtcDatetime | None = None
    last_success: UtcDatetime | None = None
    error: str | None = None         # sanitized code, never device or credential text
    device: RscpDevice | None = None
    groups: list[RscpGroup] = Field(default_factory=list)


class RscpConfigView(_Strict):
    enabled: bool
    host: str
    port: int
    power_interval_s: int
    system_interval_s: int
    user_set: bool                   # credentials are write-only – only whether they exist
    password_set: bool
    key_set: bool
    state: RscpState
    device: RscpDevice | None = None


class RscpConfigUpdate(_Strict):
    enabled: bool | None = None
    host: str | None = Field(default=None, max_length=64)
    port: int | None = None
    power_interval_s: int | None = None
    system_interval_s: int | None = None
    user: str | None = Field(default=None, min_length=1, max_length=256)
    password: str | None = Field(default=None, min_length=1, max_length=256)
    key: str | None = Field(default=None, min_length=1, max_length=32)
