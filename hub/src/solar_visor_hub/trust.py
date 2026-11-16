"""openWB trust rules (plan §3.3 step 3, P2 WP1).

A retained or frozen MQTT value looks perfectly normal in Home Assistant (a charging power can stay frozen for
almost a day after an HA restart). Trust is therefore *expiring evidence*:

- Evidence = an observed change of the charging power or progress of the energy counter above its
  high-water mark – only from the running subscription. Values from ``get_states`` (bootstrap /
  reconnect) and transitions out of ``unknown``/``unavailable`` only set baselines.
- A positive power stays trustworthy for 120 s after the latest evidence (or baseline), a zero for
  5 min unless the plug or charging status confirms it (status "off").
- Power > 50 W while the charging status says "off" contradicts itself → implausible.

Measured 2026-10-04 (11 days of HA history): while charging, power and counter change every ~10 s
(median = p90); "charging on" never occurred with 0 W and "charging off" never with power > 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import MetricState

POSITIVE_W = 50.0                      # below: treated as "zero" (no charging)
CHANGE_EPS_W = 0.5                     # any real change counts (live values jitter by a few W)
POSITIVE_WINDOW = timedelta(seconds=120)
ZERO_WINDOW = timedelta(minutes=5)


@dataclass
class ChargepointTrust:
    power: float | None = None          # last power value seen (W)
    baseline_at: datetime | None = None
    last_evidence: datetime | None = None
    counter_hwm: float | None = None    # highest trusted counter value (Wh)
    counter_below_hwm: bool = False
    counter_anomalies: int = 0          # rollbacks below the high-water mark (diagnostics)

    def on_power(self, value: float | None, at: datetime, *, baseline: bool) -> None:
        if value is None:
            self.power = None           # unknown/unavailable: the next value is a baseline
            return
        if baseline or self.power is None:
            self.power, self.baseline_at = value, at
            return
        if abs(value - self.power) > CHANGE_EPS_W:
            self.last_evidence = at
        self.power = value

    def on_counter(self, value: float | None, at: datetime, *, baseline: bool) -> None:
        if value is None:
            return
        if self.counter_hwm is None:
            self.counter_hwm = value
            return
        if value < self.counter_hwm:
            if not self.counter_below_hwm:
                self.counter_anomalies += 1
            self.counter_below_hwm = True   # e.g. 1562.64 → 1556.23 kWh: no progress until above the old maximum
            return
        self.counter_below_hwm = False
        if value > self.counter_hwm:
            self.counter_hwm = value
            if not baseline:
                self.last_evidence = at

    def evaluate(self, now: datetime, plug: bool | None, charging: bool | None) -> tuple[MetricState | None, list[str]]:
        """State override for the power metric (None = keep the base state) plus reasons."""
        if self.power is None:
            return None, []   # no wallbox value at all (metric itself is unknown)
        refs = [t for t in (self.last_evidence, self.baseline_at) if t is not None]
        ref = max(refs) if refs else None
        awaiting = ["awaiting_confirmation"] if self.last_evidence is None or (
            self.baseline_at is not None and self.last_evidence < self.baseline_at) else []
        if self.power > POSITIVE_W:
            if charging is False:
                return MetricState.IMPLAUSIBLE, ["status_contradiction"]
            if ref is None or now - ref > POSITIVE_WINDOW:
                return MetricState.STALE, ["unconfirmed_hold"]
            return None, awaiting
        if plug is False or charging is False:
            return None, []             # zero confirmed by a fresh status
        if ref is None or now - ref > ZERO_WINDOW:
            return MetricState.STALE, ["unconfirmed_zero"]
        return None, awaiting
