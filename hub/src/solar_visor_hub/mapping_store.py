"""Effective mapping = packaged default + user overrides (P2 WP5, mapping editor in the app).

Validation happens against the entity's current Home Assistant state (exists, domain, unit compatible
with the metric's canonical unit). Changes bump ``mapping_revision``, are persisted atomically and
propagated to listeners (live model, HA client re-subscription, journal).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import MappingConfig, apply_overrides, read_overrides, write_overrides
from .live import DERIVED_METRICS
from .normalize import compatible_unit

ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


class MappingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MappingStore:
    def __init__(self, base: MappingConfig, overrides_path: Path | None) -> None:
        """``overrides_path=None``: the base is the whole truth (projection of a plant) – no file is read or written."""
        self.base = base
        self.path = overrides_path
        self._overrides = read_overrides(overrides_path) if overrides_path else {"schema": 1, "revision": 0, "metrics": {}}
        self.current = apply_overrides(base, self._overrides)
        self._listeners: list[Callable[[MappingConfig, str], None]] = []

    def subscribe(self, listener: Callable[[MappingConfig, str], None]) -> None:
        self._listeners.append(listener)

    def entries(self) -> list[dict[str, Any]]:
        out = []
        for mid, m in sorted(self.current.metrics.items()):
            d = self.base.metrics[mid]
            out.append({"metric_id": mid, "source_id": m.source, "unit": m.unit.value,
                        "measurement_boundary": m.boundary, "entity": m.entity, "sign": m.sign,
                        "default_entity": d.entity, "default_sign": d.sign,
                        "overridden": (m.entity, m.sign) != (d.entity, d.sign)})
        return out

    def set(self, metric_id: str, entity: str, sign: int, ha_state: dict[str, Any] | None) -> MappingConfig:
        if metric_id in DERIVED_METRICS:
            raise MappingError("derived", "Berechnete Größen sind nicht zuordenbar")
        base = self.base.metrics.get(metric_id)
        if base is None:
            raise MappingError("unknown_metric", "Unbekannte Messgröße")
        if sign not in (1, -1):
            raise MappingError("invalid_sign", "Vorzeichen muss +1 oder −1 sein")
        if not ENTITY_ID.match(entity):
            raise MappingError("invalid_entity", "Ungültige Entity-ID")
        if ha_state is None:
            raise MappingError("entity_not_found", "Entity in Home Assistant nicht gefunden")
        domain = entity.split(".", 1)[0]
        unit = (ha_state.get("attributes") or {}).get("unit_of_measurement")
        if not compatible_unit(unit, base.unit, domain):
            raise MappingError("unit_incompatible",
                               f"Einheit {unit or '—'} ({domain}) passt nicht zu {base.unit.value}")
        metrics = dict(self._overrides.get("metrics") or {})
        if (entity, sign) == (base.entity, base.sign):
            metrics.pop(metric_id, None)
        else:
            metrics[metric_id] = {"entity": entity, "sign": sign}
        return self._commit(metrics, f"{metric_id} → {entity} ({'+' if sign > 0 else '−'}1)")

    def clear(self, metric_id: str) -> MappingConfig:
        if metric_id not in self.base.metrics:
            raise MappingError("unknown_metric", "Unbekannte Messgröße")
        metrics = dict(self._overrides.get("metrics") or {})
        metrics.pop(metric_id, None)
        return self._commit(metrics, f"{metric_id} → Standard")

    def _commit(self, metrics: dict[str, Any], description: str) -> MappingConfig:
        if self.path is None:
            raise MappingError("read_only", "Die Zuordnung wird über die Anlage verwaltet")
        data = {"schema": self._overrides.get("schema", 1), "revision": int(self._overrides.get("revision", 0)) + 1,
                "metrics": metrics}
        write_overrides(self.path, data)
        self._overrides = data
        self.current = apply_overrides(self.base, data)
        for listener in self._listeners:
            listener(self.current, description)
        return self.current
