"""Live evaluation of a plant (P2 detail plan §4, step 3): entity states → metrics, quality, derived values, flow
edges – driven by the plant's modules instead of fixed metric ids.

Independent of ``live.py`` on purpose: that evaluation serves ``/v1`` unchanged as the reference until the apps have
moved and is then deleted without touching this one. For the imported existing plant both must give the same values
(``tests/test_plant_live.py`` compares them event by event).

Module rules:
- switched off = known absence: the term leaves every sum, there is no node, edge or point;
- switched on without a usable value = unknown: ``null`` in every value derived from it, never 0.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .models import FlowEdge, MetricState, Unit
from .models_v2 import PlantMetric, PlantModuleStatus, PlantSnapshot, PointRole
from .normalize import normalize
from .plant import Kind, Plant, Point, Role
from .trust import ChargepointTrust

_STATE_RANK = {MetricState.OK: 0, MetricState.STALE: 1, MetricState.IMPLAUSIBLE: 2, MetricState.UNKNOWN: 3}
COMPARABLE_WINDOW = timedelta(seconds=15)
# age limit when a point states none (unmeasured defaults, to be checked against real report intervals in P4):
# power changes all the time, the rest may rest; a status text has no age of its own
DEFAULT_STALE_S: dict[Unit, int | None] = {Unit.W: 120, Unit.WH: 900, Unit.V: 900, Unit.A: 900, Unit.PERCENT: 900,
                                           Unit.BOOL: None, Unit.TEXT: None}
NEGATIVE_TOLERANCE_W = 50.0      # sensors of one moment never agree exactly: small negative sums are noise
GENERATION, CONSUMPTION, HOME, BUS = "generation", "consumption", "home", "bus"
OK, STALE, IMPLAUSIBLE, UNKNOWN = MetricState.OK, MetricState.STALE, MetricState.IMPLAUSIBLE, MetricState.UNKNOWN


def worst(*states: MetricState) -> MetricState:
    return max(states, key=lambda s: _STATE_RANK[s]) if states else UNKNOWN


@dataclass
class EntityState:
    raw_state: str | None
    unit: str | None
    last_changed: datetime | None
    last_reported: datetime | None = None
    observed_at: datetime | None = None
    last_updated: datetime | None = None    # attribute clock (unit changes), separate from last_changed


@dataclass(frozen=True)
class _Ref:
    metric_id: str
    module: str
    point_id: str           # "power" – or "string_1.power"
    point: Point


@dataclass(frozen=True)
class QualityPoint:
    state: MetricState
    reasons: list[str]
    source_id: str
    source_entity: str | None
    mapping_revision: int


def _num(p: PlantMetric | None) -> float | None:
    return p.value if p is not None and isinstance(p.value, float) else None


@dataclass
class PlantLive:
    plant: Plant
    entities: dict[str, EntityState] = field(default_factory=dict)
    connected: bool = False
    disconnected_since: datetime | None = None
    revision: int = 0
    freshness_slack_s: float = 15.0         # last_reported is only sampled every freshness interval
    discarded_events: dict[str, int] = field(default_factory=dict)     # late events per module (diagnostics)
    trust: dict[str, ChargepointTrust] = field(default_factory=dict)   # per wallbox with the retained-value check
    on_quality_edge: Callable[[datetime], None] | None = None
    on_observation: Callable[[str, datetime, str | None, str | None], None] | None = None

    def __post_init__(self) -> None:
        self._refs: dict[str, _Ref] = {}
        self._by_entity: dict[str, list[_Ref]] = {}
        for metric_id, point in self.plant.points().items():
            module, point_id = metric_id.split(".", 1)
            ref = _Ref(metric_id, module, point_id, point)
            self._refs[metric_id] = ref
            self._by_entity.setdefault(point.entity, []).append(ref)
        for m in self.plant.active():
            if m.kind is Kind.WALLBOX and m.trust == "openwb_retained":
                self.trust[m.id] = ChargepointTrust()

    def subscribed_entities(self) -> list[str]:
        return sorted(self._by_entity)

    # --- input ---------------------------------------------------------------------------------------------------
    def apply_state(self, entity_id: str, raw_state: str | None, unit: str | None, last_changed: datetime | None,
                    observed_at: datetime, *, bootstrap: bool = False, last_updated: datetime | None = None) -> bool:
        """False if the event was ignored (entity not part of the plant, late or exact duplicate event).
        ``bootstrap`` marks values from ``get_states`` (start/reconnect): baselines, never evidence for the trust
        check – retained values look like normal values."""
        refs = self._by_entity.get(entity_id)
        if not refs:
            return False
        prev = self.entities.get(entity_id)
        if prev and prev.last_changed and last_changed:
            if last_changed < prev.last_changed:
                module = refs[0].module
                self.discarded_events[module] = self.discarded_events.get(module, 0) + 1
                return False        # late event – never overwrite a newer value
            if last_changed == prev.last_changed and raw_state == prev.raw_state and unit == prev.unit:
                return False        # exact duplicate (replay, reconnect) – changes nothing
        reported = max(filter(None, (last_changed, prev.last_reported if prev else None)), default=last_changed)
        updated = max(filter(None, (last_updated, last_changed)), default=None)
        self.entities[entity_id] = EntityState(raw_state, unit, last_changed, reported, observed_at, updated)
        self.revision += 1
        first = refs[0].point
        was_known = prev is not None and self._known(first, prev.raw_state, prev.unit)
        self._feed_trust(refs, raw_state, unit, last_changed or observed_at, baseline=bootstrap or not was_known)
        if prev is not None and was_known != self._known(first, raw_state, unit) and self.on_quality_edge:
            self.on_quality_edge(observed_at)
        if not bootstrap and self.on_observation:
            self.on_observation(entity_id, last_changed or observed_at, raw_state, unit)
        return True

    @staticmethod
    def _known(point: Point, raw_state: str | None, unit: str | None) -> bool:
        return normalize(point, raw_state, unit).known          # type: ignore[arg-type]  # needs .unit and .sign only

    def _feed_trust(self, refs: list[_Ref], raw_state: str | None, unit: str | None, at: datetime, *,
                    baseline: bool, reset_counter: bool = False) -> None:
        for ref in refs:
            trust = self.trust.get(ref.module)
            if trust is None or ref.point_id not in ("power", "energy_total"):
                continue
            value = normalize(ref.point, raw_state, unit).value   # type: ignore[arg-type]
            num = float(value) if isinstance(value, float) else None
            if ref.point_id == "power":
                trust.on_power(num, at, baseline=baseline)
            else:
                if reset_counter:
                    trust.counter_hwm, trust.counter_below_hwm = None, False    # new scale
                trust.on_counter(num, at, baseline=baseline)

    def apply_unit(self, entity_id: str, unit: str | None, updated_at: datetime | None = None) -> bool:
        """Unit-only attribute change, ordered by the attribute clock. False if not applied."""
        st = self.entities.get(entity_id)
        if st is None or st.unit == unit:
            return False
        ref = st.last_updated or st.last_changed
        if updated_at and ref and updated_at < ref:
            return False            # attribute update older than what we already have
        st.unit = unit
        st.last_updated = updated_at or st.last_updated
        self.revision += 1
        # the normalised value changed scale: new baseline (an attribute update is no charging evidence)
        self._feed_trust(self._by_entity.get(entity_id, []), st.raw_state, unit,
                         updated_at or st.last_changed or datetime.now(UTC), baseline=True, reset_counter=True)
        return True

    def apply_reported(self, entity_id: str, last_reported: datetime | None) -> None:
        st = self.entities.get(entity_id)
        if st and last_reported and (st.last_reported is None or last_reported > st.last_reported):
            st.last_reported = last_reported
            self.revision += 1

    def set_connected(self, connected: bool, now: datetime) -> None:
        self.connected = connected
        self.disconnected_since = None if connected else (self.disconnected_since or now)
        self.revision += 1

    # --- metrics -------------------------------------------------------------------------------------------------
    def _metric(self, ref: _Ref, now: datetime) -> PlantMetric:
        p = ref.point
        role = PointRole(p.role.value)
        st = self.entities.get(p.entity)
        if st is None:
            return PlantMetric(metric_id=ref.metric_id, module=ref.module, value=None, unit=p.unit, role=role,
                               state=UNKNOWN, reasons=["no_data"], observed_at=now)
        norm = normalize(p, st.raw_state, st.unit)              # type: ignore[arg-type]
        reasons = list(norm.reasons)
        state = OK if norm.known else UNKNOWN
        if state is OK and p.nonnegative and isinstance(norm.value, float) and norm.value < 0:
            state, reasons = IMPLAUSIBLE, [*reasons, "negative_value"]
        if state is OK and p.unit is Unit.PERCENT and isinstance(norm.value, float) and not 0 <= norm.value <= 100:
            state, reasons = IMPLAUSIBLE, [*reasons, "percent_out_of_range"]
        if state is OK:
            age_ref = st.last_reported or st.last_changed
            if p.freshness_from:
                # only a valid report of the reference keeps the value current (repeated "unavailable" must not)
                other = self._refs.get(f"{ref.metric_id.rsplit('.', 1)[0]}.{p.freshness_from}")
                o = self.entities.get(other.point.entity) if other else None
                age_ref = (o.last_reported or o.last_changed) if other and o and self._known(other.point, o.raw_state, o.unit) else None
            limit = p.stale_after_s if p.stale_after_s is not None else DEFAULT_STALE_S[p.unit]
            if not self.connected:
                state, reasons = STALE, [*reasons, "ha_disconnected"]
            elif limit is not None and (age_ref is None or now - age_ref > timedelta(seconds=limit + self.freshness_slack_s)):
                state, reasons = STALE, [*reasons, "report_too_old"]
        return PlantMetric(metric_id=ref.metric_id, module=ref.module, value=norm.value, unit=p.unit, role=role,
                           state=state, reasons=reasons, source_changed_at=st.last_changed,
                           reported_at=st.last_reported, observed_at=st.observed_at or now)

    @staticmethod
    def _derived(metric_id: str, value: float | None, state: MetricState, inputs: list[PlantMetric], now: datetime,
                 reasons: list[str] | None = None) -> PlantMetric:
        return PlantMetric(metric_id=metric_id, module=metric_id.split(".", 1)[0], value=value, unit=Unit.W,
                           role=PointRole.DERIVED, state=state, derived=True, reasons=reasons or [],
                           input_metric_ids=[i.metric_id for i in inputs], observed_at=now,
                           reported_at=min((i.reported_at for i in inputs if i.reported_at), default=None))

    def _sum(self, metric_id: str, terms: list[tuple[PlantMetric, int]], now: datetime) -> PlantMetric:
        """Signed sum of known terms; unknown as soon as one term is unknown."""
        inputs = [p for p, _ in terms]
        values = [_num(p) for p in inputs]
        if not terms or any(v is None for v in values):
            return self._derived(metric_id, None, UNKNOWN, inputs, now, ["input_unknown"] if terms else ["no_input"])
        total = float(sum(v * sign for v, (_, sign) in zip(values, terms)))     # type: ignore[operator]
        return self._derived(metric_id, total, worst(*(p.state for p in inputs)), inputs, now)

    def snapshot(self, now: datetime | None = None) -> PlantSnapshot:
        now = now or datetime.now(UTC)
        mods = self.plant.modules
        metrics = {mid: self._metric(ref, now) for mid, ref in self._refs.items()}
        pv_power = [metrics[f"{m.id}.power"] for m in self.plant.active() if m.kind is Kind.PV]

        # generation: all switched-on generators
        gen_val = float(sum(_num(p) for p in pv_power)) if all(_num(p) is not None for p in pv_power) else None  # type: ignore[misc]
        generation = metrics[f"{GENERATION}.power"] = self._derived(
            f"{GENERATION}.power", gen_val, worst(*(p.state for p in pv_power)) if gen_val is not None else UNKNOWN,
            pv_power, now)

        # grid: one signed sensor, or import and export as two non-negative ones
        if mods["grid"].variant == "split":
            grid_inputs = [metrics["grid.import_power"], metrics["grid.export_power"]]
            grid = metrics["grid.power"] = self._sum("grid.power", [(grid_inputs[0], 1), (grid_inputs[1], -1)], now)
        else:
            grid = metrics["grid.power"]
            grid_inputs = [grid]
        battery = metrics["battery.power"] if mods["battery"].enabled else None

        for wallbox_id in self.trust:
            self._apply_trust(metrics, wallbox_id, now)

        # total consumption: measured if the plant has the sensor, else what the bus delivers
        measured = "consumption.power" in self._refs
        if measured:
            cons = metrics[f"{CONSUMPTION}.power"]
        else:
            terms = [(generation, 1), (grid, 1)] + ([(battery, -1)] if battery is not None else [])
            cons = self._sum(f"{CONSUMPTION}.power", terms, now)
            if (_num(cons) or 0.0) < -NEGATIVE_TOLERANCE_W:
                # the plant cannot consume less than nothing: a sensor, a sign or a missing generator is wrong
                cons = cons.model_copy(update={"state": IMPLAUSIBLE, "reasons": [*cons.reasons, "negative_consumption"]})
            metrics[f"{CONSUMPTION}.power"] = cons

        metrics[f"{HOME}.power"] = self._home(metrics, cons, measured, now)
        edges = self._edges(metrics, pv_power)
        residual, comparable = self._residual(pv_power, grid_inputs, grid, battery, cons) if measured else (None, False)
        return PlantSnapshot(
            snapshot_id=f"{self.revision}-{uuid.uuid4().hex[:8]}", generated_at=now, plant_revision=self.plant.revision,
            connected=self.connected, metrics=metrics, edges=edges, balance_residual_w=residual,
            balance_comparable=comparable, modules=self._modules(metrics))

    def quality_points(self, now: datetime) -> dict[str, QualityPoint]:
        """The snapshot as the quality journal wants it: state and reasons per metric, with the entity it came from
        (the read contract carries no entity names, the journal needs them to mark history as unknown)."""
        out: dict[str, QualityPoint] = {}
        for mid, p in self.snapshot(now).metrics.items():
            ref = self._refs.get(mid)
            out[mid] = QualityPoint(state=p.state, reasons=p.reasons, source_id="hub" if p.derived else p.module,
                                    source_entity=ref.point.entity if ref else None,
                                    mapping_revision=self.plant.revision)
        return out

    def _apply_trust(self, metrics: dict[str, PlantMetric], wallbox_id: str, now: datetime) -> None:
        wb = metrics.get(f"{wallbox_id}.power")
        if wb is None or wb.state is not OK:
            return

        def fresh_flag(point_id: str) -> bool | None:
            p = metrics.get(f"{wallbox_id}.{point_id}")
            return p.value if p is not None and p.state is OK and isinstance(p.value, bool) else None

        override, reasons = self.trust[wallbox_id].evaluate(now, fresh_flag("plugged"), fresh_flag("charging"))
        if override is not None or reasons:
            metrics[f"{wallbox_id}.power"] = wb.model_copy(update={"state": override or wb.state,
                                                                   "reasons": [*wb.reasons, *reasons]})

    def _home(self, metrics: dict[str, PlantMetric], cons: PlantMetric, measured: bool, now: datetime) -> PlantMetric:
        """Home = total consumption minus the consumers shown beside it (wallbox, extra consumers "beside home")."""
        mods = self.plant.modules
        home_id = f"{HOME}.power"
        c = _num(cons)
        if cons.state is IMPLAUSIBLE:
            # do not blame the wallbox for a faulty consumption value
            return self._derived(home_id, None, UNKNOWN, self._home_inputs(metrics, cons), now, ["consumption_implausible"])
        if c is None:
            return self._derived(home_id, None, UNKNOWN, self._home_inputs(metrics, cons), now, ["input_unknown"])

        inputs, reasons, value, state = [cons], [], c, cons.state
        if mods["wallbox"].enabled:
            wb, w = metrics["wallbox.power"], _num(metrics["wallbox.power"])
            if (measured and w is not None and cons.state is OK and wb.state in (OK, STALE)
                    and w > c + max(100.0, 0.02 * c)):
                # only against a fresh, measured consumption (it includes the wallbox): a held wallbox value above
                # it is impossible now – retained replay after an HA restart (observed) → implausible
                wb = metrics["wallbox.power"] = wb.model_copy(update={"state": IMPLAUSIBLE,
                                                                      "reasons": [*wb.reasons, "exceeds_consumption"]})
            if wb.state is OK and w is not None:
                inputs.append(wb)
                value -= w
                state = worst(state, wb.state)
            else:
                # user decision 2026-10-05: an untrusted wallbox value counts as 0 W, marked as an assumption –
                # wrong only while the car charges and HA reports nothing usable. Value and age: consumption alone.
                reasons = ["wallbox_assumed_zero", f"wallbox_{wb.state.value}"]
        for m in self.plant.active():
            if m.kind is Kind.GENERIC and m.balance == "beside_home":
                extra = metrics[f"{m.id}.power"]
                inputs.append(extra)
                if extra.state is not OK or _num(extra) is None:
                    return self._derived(home_id, None, UNKNOWN, inputs, now, ["input_unknown"])
                value -= _num(extra)        # type: ignore[operator]
                state = worst(state, extra.state)
        if value < 0:
            return self._derived(home_id, None, UNKNOWN, inputs, now, ["negative_difference"])   # never shown as 0
        return self._derived(home_id, value, state, inputs, now, reasons)

    def _home_inputs(self, metrics: dict[str, PlantMetric], cons: PlantMetric) -> list[PlantMetric]:
        beside = [metrics["wallbox.power"]] if self.plant.modules["wallbox"].enabled else []
        beside += [metrics[f"{m.id}.power"] for m in self.plant.active()
                   if m.kind is Kind.GENERIC and m.balance == "beside_home"]
        return [cons, *beside]

    def _edges(self, metrics: dict[str, PlantMetric], pv_power: list[PlantMetric]) -> list[FlowEdge]:
        def edge(node: str, a: str, b: str, metric_id: str) -> FlowEdge:
            p = metrics[metric_id]
            return FlowEdge(edge_id=node, from_node=a, to_node=b, power_w=_num(p), state=p.state, derived=p.derived)

        mods = self.plant.modules
        edges = [edge(p.module, p.module, GENERATION, p.metric_id) for p in pv_power]
        edges.append(edge(GENERATION, GENERATION, BUS, f"{GENERATION}.power"))
        if mods["battery"].enabled:
            edges.append(edge("battery", BUS, "battery", "battery.power"))          # + charging
        edges.append(edge("grid", "grid", BUS, "grid.power"))                       # + import
        edges.append(edge(CONSUMPTION, BUS, CONSUMPTION, f"{CONSUMPTION}.power"))
        edges.append(edge(HOME, CONSUMPTION, HOME, f"{HOME}.power"))
        if mods["wallbox"].enabled:
            edges.append(edge("wallbox", CONSUMPTION, "wallbox", "wallbox.power"))
        for m in self.plant.active():
            if m.kind is Kind.GENERIC:
                source = HOME if m.balance == "part_of_home" else CONSUMPTION
                edges.append(edge(m.id, source, m.id, f"{m.id}.power"))
        return edges

    @staticmethod
    def _residual(pv_power: list[PlantMetric], grid_inputs: list[PlantMetric], grid: PlantMetric,
                  battery: PlantMetric | None, cons: PlantMetric) -> tuple[float | None, bool]:
        """Sources − sinks against the measured total consumption; never redistributed."""
        measured = [*pv_power, *grid_inputs, *([battery] if battery is not None else []), cons]
        if any(_num(p) is None for p in (*measured, grid)):
            return None, False
        residual = sum(_num(p) for p in pv_power) + _num(grid) - (_num(battery) if battery is not None else 0.0) - _num(cons)  # type: ignore[misc,operator]
        stamps = [p.reported_at or p.source_changed_at for p in measured]
        comparable = all(stamps) and max(stamps) - min(stamps) <= COMPARABLE_WINDOW   # type: ignore[type-var,operator]
        return float(residual), bool(comparable)

    def _modules(self, metrics: dict[str, PlantMetric]) -> dict[str, PlantModuleStatus]:
        out: dict[str, PlantModuleStatus] = {}
        for m in self.plant.active():
            own = [p for p in metrics.values() if p.module == m.id and not p.derived]
            counted = [p for p in own if p.role in (PointRole.AUTHORITATIVE, PointRole.SUBSET)]
            if not counted:
                continue            # consumption without its own sensor
            out[m.id] = PlantModuleStatus(state=worst(*(p.state for p in counted)),
                                          last_seen_at=max((p.reported_at for p in own if p.reported_at), default=None))
        return out


__all__ = ["PlantLive", "EntityState", "worst", "Role"]
