"""Live model: current entity states → canonical metrics, quality, derived values, flow edges
(plan §3.2, §3.3, §3.5; UI spec §2.2 bus model).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import MappingConfig, MetricMapping
from .models import (
    BalanceProfile,
    FlowEdge,
    MetricPoint,
    MetricState,
    Snapshot,
    SourceStatus,
    TimestampKind,
    Unit,
)
from .normalize import normalize
from .trust import ChargepointTrust

_STATE_RANK = {MetricState.OK: 0, MetricState.STALE: 1, MetricState.IMPLAUSIBLE: 2, MetricState.UNKNOWN: 3}
COMPARABLE_WINDOW = timedelta(seconds=15)
# Metrics computed by the hub (single source of truth, also used by /v1/capabilities)
DERIVED_METRICS: tuple[str, ...] = ("generation_total", "home_power")


def worst(*states: MetricState) -> MetricState:
    return max(states, key=lambda s: _STATE_RANK[s]) if states else MetricState.UNKNOWN


@dataclass
class EntityState:
    raw_state: str | None
    unit: str | None
    last_changed: datetime | None
    last_reported: datetime | None = None
    observed_at: datetime | None = None
    last_updated: datetime | None = None   # attribute clock (unit changes), separate from last_changed


@dataclass
class LiveModel:
    mapping: MappingConfig
    entities: dict[str, EntityState] = field(default_factory=dict)
    connected: bool = False
    disconnected_since: datetime | None = None
    revision: int = 0
    # last_reported is only sampled every freshness interval → allow that much extra age
    freshness_slack_s: float = 15.0
    # late/duplicate events dropped per source (diagnostics, plan §3.3 step 5)
    discarded_events: dict[str, int] = field(default_factory=dict)
    # openWB trust rules for the (single) logical charge point (plan §3.3 step 3)
    trust: ChargepointTrust = field(default_factory=ChargepointTrust)
    # called when an entity switches between known and unknown/unavailable (journal captures blips
    # shorter than its polling interval); argument: event time
    on_quality_edge: Callable[[datetime], None] | None = None
    # every accepted subscription change (not bootstrap) – persisted for the K2 reconciliation
    on_observation: Callable[[str, datetime, str | None, str | None], None] | None = None

    def subscribed_entities(self) -> list[str]:
        return self.mapping.entities()

    # --- input -----------------------------------------------------------------------------
    def apply_state(self, entity_id: str, raw_state: str | None, unit: str | None,
                    last_changed: datetime | None, observed_at: datetime, *, bootstrap: bool = False,
                    last_updated: datetime | None = None) -> bool:
        """Returns False if the event was ignored (unmapped entity, late or exact duplicate event).

        ``bootstrap`` marks values from ``get_states`` (start/reconnect): they set baselines but are
        never evidence for the trust rules (retained values look like normal values)."""
        metrics = self.mapping.metric_for_entity(entity_id)
        if not metrics:
            return False
        prev = self.entities.get(entity_id)
        if prev and prev.last_changed and last_changed:
            if last_changed < prev.last_changed:
                source = metrics[0].source
                self.discarded_events[source] = self.discarded_events.get(source, 0) + 1
                return False  # late event – never overwrite a newer value
            if last_changed == prev.last_changed and raw_state == prev.raw_state and unit == prev.unit:
                return False  # exact duplicate (replay, reconnect reconciliation) – changes nothing
        reported = max(filter(None, (last_changed, prev.last_reported if prev else None)), default=last_changed)
        updated = max(filter(None, (last_updated, last_changed)), default=None)
        self.entities[entity_id] = EntityState(raw_state, unit, last_changed, reported, observed_at, updated)
        self.revision += 1
        was_known = prev is not None and normalize(metrics[0], prev.raw_state, prev.unit).known
        self._feed_trust(metrics, raw_state, unit, last_changed or observed_at, baseline=bootstrap or not was_known)
        if prev is not None and was_known != normalize(metrics[0], raw_state, unit).known and self.on_quality_edge:
            self.on_quality_edge(observed_at)
        if not bootstrap and self.on_observation:
            self.on_observation(entity_id, last_changed or observed_at, raw_state, unit)
        return True

    def _feed_trust(self, metrics: list[MetricMapping], raw_state: str | None, unit: str | None,
                    at: datetime, *, baseline: bool, reset_counter: bool = False) -> None:
        for m in metrics:
            if m.metric_id not in ("wallbox_power", "wallbox_energy_total"):
                continue
            value = normalize(m, raw_state, unit).value
            num = float(value) if isinstance(value, float) else None
            if m.metric_id == "wallbox_power":
                self.trust.on_power(num, at, baseline=baseline)
            else:
                if reset_counter:
                    self.trust.counter_hwm, self.trust.counter_below_hwm = None, False   # new scale
                self.trust.on_counter(num, at, baseline=baseline)

    def _reseed_trust(self) -> None:
        """Re-establish trust baselines from the retained entity states (after a reset or remap)."""
        for mid in ("wallbox_power", "wallbox_energy_total"):
            m = self.mapping.metrics.get(mid)
            st = self.entities.get(m.entity) if m else None
            if m and st:
                self._feed_trust([m], st.raw_state, st.unit, st.last_changed or st.observed_at or datetime.now(UTC),
                                 baseline=True, reset_counter=True)

    def apply_unit(self, entity_id: str, unit: str | None, updated_at: datetime | None = None) -> bool:
        """Unit-only attribute change; ordered by the attribute clock. Returns False if not applied."""
        st = self.entities.get(entity_id)
        if st is None or st.unit == unit:
            return False
        ref = st.last_updated or st.last_changed
        if updated_at and ref and updated_at < ref:
            return False  # attribute update older than what we already have
        st.unit = unit
        st.last_updated = updated_at or st.last_updated
        self.revision += 1
        metrics = self.mapping.metric_for_entity(entity_id)
        # the normalized value changed scale: new baseline (an attribute update is no charging evidence)
        self._feed_trust(metrics, st.raw_state, unit, updated_at or st.last_changed or datetime.now(UTC),
                         baseline=True, reset_counter=True)
        return True

    def apply_reported(self, entity_id: str, last_reported: datetime | None) -> None:
        st = self.entities.get(entity_id)
        if st and last_reported and (st.last_reported is None or last_reported > st.last_reported):
            st.last_reported = last_reported
            self.revision += 1

    def set_mapping(self, mapping: MappingConfig) -> None:
        """Mapping changed (editor): keep states of still-mapped entities, restart trust if the charge
        point entities changed. The HA client re-subscribes with the new entity list."""
        wb_ids = ("wallbox_power", "wallbox_energy_total", "wallbox_plug", "wallbox_charging")
        reset = any(self.mapping.metrics.get(m) != mapping.metrics.get(m) for m in wb_ids)
        wanted = set(mapping.entities())
        self.entities = {e: st for e, st in self.entities.items() if e in wanted}
        self.mapping = mapping
        if reset:
            # fresh trust, but seeded from retained states: an unchanged value re-delivered on reconnect is
            # dropped as duplicate and would otherwise never reach the trust rules (Codex P2 finding 1)
            self.trust = ChargepointTrust()
            self._reseed_trust()
        self.revision += 1

    def set_connected(self, connected: bool, now: datetime) -> None:
        self.connected = connected
        self.disconnected_since = None if connected else (self.disconnected_since or now)
        self.revision += 1

    # --- metrics ---------------------------------------------------------------------------
    def _metric(self, m: MetricMapping, now: datetime) -> MetricPoint:
        st = self.entities.get(m.entity)
        src = self.mapping.sources[m.source]
        reasons: list[str] = []
        if st is None:
            return MetricPoint(metric_id=m.metric_id, value=None, unit=m.unit, source_id=m.source,
                               source_entity=m.entity, measurement_boundary=m.boundary, observed_at=now,
                               state=MetricState.UNKNOWN, reasons=["no_data"], mapping_revision=self.mapping.revision)
        norm = normalize(m, st.raw_state, st.unit)
        reasons.extend(norm.reasons)
        state = MetricState.OK if norm.known else MetricState.UNKNOWN
        if state is MetricState.OK and m.nonnegative and isinstance(norm.value, float) and norm.value < 0:
            state, reasons = MetricState.IMPLAUSIBLE, [*reasons, "negative_value"]
        if state is MetricState.OK and m.unit is Unit.PERCENT and isinstance(norm.value, float) and not 0 <= norm.value <= 100:
            state, reasons = MetricState.IMPLAUSIBLE, [*reasons, "percent_out_of_range"]
        if state is MetricState.OK:
            age_ref = st.last_reported or st.last_changed
            if m.freshness_from:
                # only a valid report of the reference keeps the value current (repeated "unavailable" must not)
                rm = self.mapping.metrics[m.freshness_from]
                ref = self.entities.get(rm.entity)
                age_ref = (ref.last_reported or ref.last_changed) if ref and normalize(rm, ref.raw_state, ref.unit).known else None
            if not self.connected:
                state, reasons = MetricState.STALE, [*reasons, "ha_disconnected"]
            elif age_ref is None or now - age_ref > timedelta(seconds=src.stale_after_s + self.freshness_slack_s):
                state, reasons = MetricState.STALE, [*reasons, "report_too_old"]
        return MetricPoint(
            metric_id=m.metric_id, value=norm.value, unit=m.unit, source_id=m.source, source_entity=m.entity,
            measurement_boundary=m.boundary, source_changed_at=st.last_changed, reported_at=st.last_reported,
            observed_at=st.observed_at or now, timestamp_kind=TimestampKind.HA_REPORTED, state=state,
            reasons=reasons, mapping_revision=self.mapping.revision,
        )

    @staticmethod
    def _derived(metric_id: str, value: float | None, unit: Unit, boundary: str, state: MetricState,
                 inputs: list[MetricPoint], now: datetime, reasons: list[str] | None = None) -> MetricPoint:
        return MetricPoint(metric_id=metric_id, value=value, unit=unit, source_id="hub", measurement_boundary=boundary,
                           observed_at=now, state=state, derived=True, reasons=reasons or [],
                           input_metric_ids=[i.metric_id for i in inputs],
                           reported_at=min((i.reported_at for i in inputs if i.reported_at), default=None))

    def snapshot(self, now: datetime | None = None, profile: BalanceProfile = BalanceProfile.E3DC_BALANCE) -> Snapshot:
        now = now or datetime.now(UTC)
        metrics = {mid: self._metric(m, now) for mid, m in self.mapping.metrics.items()}

        def num(mid: str) -> float | None:
            v = metrics[mid].value if mid in metrics else None
            return v if isinstance(v, float) else None

        external_id = "kostal_balance" if profile is BalanceProfile.E3DC_BALANCE else "kostal_ac"
        pv, ext = metrics["pv_internal"], metrics[external_id]
        gen_val = num("pv_internal") + num(external_id) if num("pv_internal") is not None and num(external_id) is not None else None
        metrics["generation_total"] = self._derived("generation_total", gen_val, Unit.W, "E3/DC-Bilanzsystem",
                                                    worst(pv.state, ext.state) if gen_val is not None else MetricState.UNKNOWN,
                                                    [pv, ext], now)

        self._apply_trust(metrics, now)
        cons, wb = metrics["consumption_total"], metrics["wallbox_power"]
        home_val: float | None = None
        home_state = MetricState.UNKNOWN
        home_reasons: list[str] = []
        c, w = num("consumption_total"), num("wallbox_power")
        if cons.state is MetricState.IMPLAUSIBLE:
            home_reasons = ["consumption_implausible"]  # do not blame the wallbox for a faulty consumption value
        elif c is None:
            home_reasons = ["input_unknown"]
        else:
            if (w is not None and cons.state is MetricState.OK and wb.state in (MetricState.OK, MetricState.STALE)
                    and w > c + max(100.0, 0.02 * c)):
                # only against a fresh consumption – a stale consumption must not make a real charge "implausible";
                # a held wallbox value above it is impossible now (consumption includes the wallbox): retained
                # replay after an HA restart (observed: a frozen value far above the consumption) → implausible
                wb = metrics["wallbox_power"] = wb.model_copy(update={"state": MetricState.IMPLAUSIBLE,
                                                                      "reasons": [*wb.reasons, "exceeds_consumption"]})
            if wb.state is MetricState.OK and w is not None:
                if c - w < 0:
                    home_reasons = ["negative_difference"]    # never shown as 0 – diagnostic only
                else:
                    home_val = c - w
                    home_state = worst(cons.state, wb.state)
            else:
                # user decision 2026-10-05 (replaces plan §3.2 "home unknown unless the wallbox is ok"): an untrusted
                # wallbox value counts as 0 W, home = total consumption, marked as an assumption – wrong only while
                # the car charges and HA reports nothing usable
                home_val, home_state = c, cons.state
                home_reasons = ["wallbox_assumed_zero", f"wallbox_{wb.state.value}"]
        assumed = "wallbox_assumed_zero" in home_reasons     # value and age come from the consumption alone
        metrics["home_power"] = self._derived("home_power", home_val, Unit.W, "E3/DC-Residuum minus Wallbox",
                                              home_state, [cons] if assumed else [cons, wb], now, home_reasons)

        edges = self._edges(metrics, external_id)
        residual, comparable = self._residual(metrics, external_id)
        return Snapshot(
            snapshot_id=f"{self.revision}-{uuid.uuid4().hex[:8]}", generated_at=now, balance_profile=profile,
            metrics=metrics, edges=edges, balance_residual_w=residual, balance_comparable=comparable,
            sources=self._sources(metrics),
        )

    def _apply_trust(self, metrics: dict[str, MetricPoint], now: datetime) -> None:
        wb = metrics.get("wallbox_power")
        if wb is None or wb.state is not MetricState.OK:
            return

        def fresh_flag(mid: str) -> bool | None:
            p = metrics.get(mid)
            return p.value if p is not None and p.state is MetricState.OK and isinstance(p.value, bool) else None

        override, reasons = self.trust.evaluate(now, fresh_flag("wallbox_plug"), fresh_flag("wallbox_charging"))
        if override is not None or reasons:
            metrics["wallbox_power"] = wb.model_copy(update={"state": override or wb.state,
                                                             "reasons": [*wb.reasons, *reasons]})

    @staticmethod
    def _edges(metrics: dict[str, MetricPoint], external_id: str) -> list[FlowEdge]:
        def edge(eid: str, a: str, b: str, mid: str) -> FlowEdge:
            p = metrics[mid]
            val = p.value if isinstance(p.value, float) else None
            return FlowEdge(edge_id=eid, from_node=a, to_node=b, power_w=val, state=p.state, derived=p.derived)

        edges = [
            edge("E1", "pv_internal", "generation", "pv_internal"),
            edge("E2", "kostal", "generation", external_id),
            edge("E3", "generation", "bus", "generation_total"),
            edge("E4", "bus", "battery", "battery_power"),     # + charging
            edge("E5", "grid", "bus", "grid_power"),           # + import
            edge("E6", "bus", "consumption", "consumption_total"),
            edge("E7", "consumption", "home", "home_power"),
            edge("E8", "consumption", "wallbox", "wallbox_power"),
        ]
        if "womo_shore_power" in metrics:                       # optional: custom mappings without the motorhome
            edges.append(edge("E9", "home", "womo", "womo_shore_power"))   # part of home (socket in the house)
        return edges

    @staticmethod
    def _residual(metrics: dict[str, MetricPoint], external_id: str) -> tuple[float | None, bool]:
        ids = ["pv_internal", external_id, "grid_power", "battery_power", "consumption_total"]
        pts = [metrics[i] for i in ids]
        if any(not isinstance(p.value, float) for p in pts):
            return None, False
        pv, ext, grid, bat, cons = (p.value for p in pts)  # type: ignore[misc]
        residual = pv + ext + grid - bat - cons
        stamps = [p.reported_at or p.source_changed_at for p in pts]
        comparable = all(stamps) and max(stamps) - min(stamps) <= COMPARABLE_WINDOW  # type: ignore[type-var,operator]
        return residual, bool(comparable)

    def _sources(self, metrics: dict[str, MetricPoint]) -> dict[str, SourceStatus]:
        out: dict[str, SourceStatus] = {}
        for sid in self.mapping.sources:
            pts = [p for p in metrics.values() if p.source_id == sid]
            if not pts:
                continue  # declared but no metric mapped (e.g. optional E3/DC Modbus strings)
            state = worst(*(p.state for p in pts))
            seen = max((p.reported_at for p in pts if p.reported_at), default=None)
            detail = "ha_disconnected" if not self.connected else None
            out[sid] = SourceStatus(source_id=sid, state=state, last_seen_at=seen, detail=detail)
        return out
