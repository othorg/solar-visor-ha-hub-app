"""Hub configuration: logical metric mapping (plan §3.2) and secret locations.

Secrets are read from files at runtime and never stored on config objects
that might be serialised (diagnostics, logs, API responses).
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path

from .models import Unit

# Packaged default (works for editable and wheel installs); override with SOLAR_VISOR_MAPPING=/path/to/file.toml
DEFAULT_MAPPING_PATH = Path(str(resources.files("solar_visor_hub") / "default_mapping.toml"))
MAPPING_ENV_VAR = "SOLAR_VISOR_MAPPING"


def mapping_path() -> Path:
    override = os.environ.get(MAPPING_ENV_VAR)
    return Path(override) if override else DEFAULT_MAPPING_PATH


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    kind: str
    stale_after_s: int
    profile: str | None = None


@dataclass(frozen=True)
class MetricMapping:
    metric_id: str
    source: str
    entity: str
    unit: Unit
    boundary: str
    sign: int = 1
    nonnegative: bool = False
    counter: bool = False
    # age taken from another metric of the same device (a value reported only on change, e.g. the motorhome SOC:
    # current as long as the battery power next to it reports)
    freshness_from: str | None = None


@dataclass(frozen=True)
class MappingConfig:
    revision: int
    sources: dict[str, SourceConfig]
    metrics: dict[str, MetricMapping]
    kostal_strings: dict[str, str] = field(default_factory=dict)
    e3dc_strings: dict[str, str] = field(default_factory=dict)      # string1 → "Dach Ost" (display names)

    def entities(self) -> list[str]:
        return sorted({m.entity for m in self.metrics.values()})

    def metric_for_entity(self, entity_id: str) -> list[MetricMapping]:
        return [m for m in self.metrics.values() if m.entity == entity_id]


class ConfigError(ValueError):
    pass


def load_mapping(path: Path | None = None) -> MappingConfig:
    raw = tomllib.loads((path or mapping_path()).read_text(encoding="utf-8"))
    sources = {
        sid: SourceConfig(
            source_id=sid,
            kind=s["kind"],
            stale_after_s=int(s["stale_after_s"]),
            profile=s.get("profile"),
        )
        for sid, s in raw.get("sources", {}).items()
    }
    metrics: dict[str, MetricMapping] = {}
    for mid, m in raw.get("metrics", {}).items():
        if m["source"] not in sources:
            raise ConfigError(f"metric {mid}: unknown source {m['source']!r}")
        if m["source"] and sources[m["source"]].kind != "home_assistant":
            raise ConfigError(f"metric {mid}: only home_assistant sources are implemented")
        sign = int(m.get("sign", 1))
        if sign not in (1, -1):
            raise ConfigError(f"metric {mid}: sign must be +1 or -1")
        metrics[mid] = MetricMapping(
            metric_id=mid,
            source=m["source"],
            entity=m["entity"],
            unit=Unit(m["unit"]),
            boundary=m["boundary"],
            sign=sign,
            nonnegative=bool(m.get("nonnegative", False)),
            counter=bool(m.get("counter", False)),
            freshness_from=m.get("freshness_from"),
        )
    for mid, m in metrics.items():
        if m.freshness_from is not None and m.freshness_from not in metrics:
            raise ConfigError(f"metric {mid}: freshness_from {m.freshness_from!r} is not a metric")
    return MappingConfig(
        revision=int(raw.get("revision", 1)),
        sources=sources,
        metrics=metrics,
        kostal_strings=dict(raw.get("kostal_strings", {}) or {}),
        e3dc_strings=dict(raw.get("e3dc_strings", {}) or {}),
    )


# --- user overrides of the mapping (P2 WP5: editor in the app) -----------------------------------
# Only entity and sign of existing logical metrics can be changed; units, boundaries, derived values
# and the balance logic stay defined by the packaged mapping.
OVERRIDES_SCHEMA = 1


def read_overrides(path: Path) -> dict:
    if not path.exists():
        return {"schema": OVERRIDES_SCHEMA, "revision": 0, "metrics": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != OVERRIDES_SCHEMA:
        raise ConfigError(f"unsupported mapping override schema {data.get('schema')!r}")
    return data


def write_overrides(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)  # atomic


def apply_overrides(base: MappingConfig, overrides: dict) -> MappingConfig:
    metrics = dict(base.metrics)
    for mid, o in (overrides.get("metrics") or {}).items():
        if mid not in metrics:
            continue  # metric removed from the packaged mapping – stale override is ignored
        m = metrics[mid]
        sign = int(o.get("sign", m.sign))
        if sign not in (1, -1):
            raise ConfigError(f"override {mid}: sign must be +1 or -1")
        metrics[mid] = replace(m, entity=str(o.get("entity", m.entity)), sign=sign)
    # unique revision per (packaged mapping, override state)
    return replace(base, metrics=metrics, revision=base.revision * 1000 + int(overrides.get("revision", 0)))


@dataclass(frozen=True)
class SecretFiles:
    """Paths only – contents are loaded on demand by the clients."""

    ha_token: Path
    influx_token: Path
    rscp_user: Path | None = None        # E3/DC RSCP (2026-10-05): portal user, password, RSCP key
    rscp_password: Path | None = None
    rscp_key: Path | None = None

    def read_ha_token(self) -> str:
        return _read_secret(self.ha_token)

    def read_influx_token(self) -> str:
        return _read_secret(self.influx_token)

    def __repr__(self) -> str:  # never leak paths' contents; paths themselves are harmless
        return f"SecretFiles(ha_token={self.ha_token.name}, influx_token={self.influx_token.name})"


def store_secret(target: Path, value: str, allow_spaces: bool = False) -> None:
    """Writes a secret file atomically with mode 0600 (CLI and admin API); the value is never logged."""
    import os
    bad = (lambda c: c in "\r\n\t") if allow_spaces else str.isspace
    if not value or len(value) > 4096 or any(bad(c) for c in value):
        raise ConfigError("empty or invalid secret")
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")   # unique, mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ConfigError(f"secret file {path.name} is empty")
    return value
