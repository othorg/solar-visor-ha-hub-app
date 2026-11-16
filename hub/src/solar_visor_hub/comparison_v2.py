"""Comparison of two periods for /v2 (plan ha-addon P3 step 8). The imported plant: the unchanged v1 comparison,
renamed to v2 ids. Other plants: not yet (capability ``compare`` is false) – the comparison is built on the existing
calculation's days and measures."""

from __future__ import annotations

from .history_v2 import LEGACY_CATEGORIES
from .models import EnergyComparison
from .models_v2 import PlantComparison

METRICS = {**LEGACY_CATEGORIES, "autarky": "autarky", "self_consumption": "self_consumption",
           "grid_import_e3dc": "grid.import.cross_check", "grid_export_e3dc": "grid.export.cross_check"}


def from_legacy(c: EnergyComparison) -> PlantComparison:
    data = c.model_dump()
    data.pop("balance_profile")
    for m in data["metrics"]:
        m["metric_id"] = METRICS.get(m["metric_id"], m["metric_id"])
    for n in data["notes"]:
        n["metrics"] = [METRICS.get(x, x) for x in n["metrics"]]
    return PlantComparison.model_validate(data)
