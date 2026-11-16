"""Consumers of the HA energy dashboard for /v2 (plan ha-addon P3 step 9). The imported plant: the unchanged v1
service renamed – the wallbox and the motorhome become modules of the plant. Other plants: not yet (the capability
``consumers`` is false), the existing service is tied to the fixed plant's metrics."""

from __future__ import annotations

from .models import HomeConsumers
from .models_v2 import PlantConsumer, PlantConsumers, PlantConsumersRest

KIND_MODULE = {"wallbox": "wallbox", "womo": "generic_1"}


def from_legacy(v: HomeConsumers) -> PlantConsumers:
    return PlantConsumers(
        available=v.available, reason=v.reason, generated_at=v.generated_at, local_date=v.local_date.isoformat(),
        stats_from=v.stats_from, stats_until=v.stats_until, consumption_w=v.consumption_w,
        consumption_today_wh=v.consumption_today_wh,
        consumers=[PlantConsumer(consumer_id=c.consumer_id, name=c.name,
                                 kind="module" if c.kind in KIND_MODULE else "device", module=KIND_MODULE.get(c.kind),
                                 power_w=c.power_w, power_state=c.power_state, power_reported_at=c.power_reported_at,
                                 energy_today_wh=c.energy_today_wh, energy_reason=c.energy_reason) for c in v.consumers],
        untracked_power=PlantConsumersRest(**v.untracked_power.model_dump()),
        untracked_energy=PlantConsumersRest(**v.untracked_energy.model_dump()))
