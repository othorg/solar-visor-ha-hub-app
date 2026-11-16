"""Runtime modes while ``/v1`` and ``/v2`` live side by side (P2 detail plan §5, step 5).

Exactly one source of truth decides at start:

- ``mapping``       no ``plant.json``: packaged mapping plus overrides, only ``/v1`` – everything as before;
- ``plant_legacy``  a plant with the legacy profile: the plant is the truth, ``/v1`` reads a projection of it,
                    ``/v2`` the plant itself; existing history calculation and InfluxDB stay;
- ``plant``         any other plant: only ``/v2``; the fixed v1 evaluation cannot describe it.
- ``setup``         add-on without a usable plant (fresh install, or the plant could not be started): no sensor is
                    subscribed, ``/v1`` and ``/v2`` answer 503 ``not_configured``; the web interface sets the plant up
                    (user decision 2026-11-15 – a fresh add-on must not watch the sensors of the author's plant).

One Home Assistant session feeds every evaluator through ``LiveFanout``; the journal has exactly one writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import profile
from .config import MappingConfig
from .plant import LEGACY_PROFILE, Plant
from .plant_legacy import legacy_mapping

MAPPING, PLANT_LEGACY, PLANT, SETUP = "mapping", "plant_legacy", "plant", "setup"
# the stored plant or the standalone mapping needs the existing-plant calculation of a specific installation
NO_PROFILE = ("Die gespeicherte Anlage nutzt die Bestandsrechnung einer bestimmten Installation; dieses Paket enthält "
              "kein solches Anlagenprofil. Die Dateien bleiben unverändert.")
EMPTY_MAPPING = MappingConfig(revision=0, sources={}, metrics={})


@dataclass(frozen=True)
class Mode:
    name: str
    plant: Plant | None = None
    projection: MappingConfig | None = None     # the old mapping as seen from the plant (plant_legacy only)
    error: str | None = None                    # setup: why the stored plant could not be started

    @property
    def serves_v1(self) -> bool:
        return self.name not in (PLANT, SETUP)

    @property
    def has_history(self) -> bool:
        """Existing history calculation, InfluxDB and reconciliation – tied to the fixed plant until P3."""
        return self.name not in (PLANT, SETUP)


def choose_mode(plant: Plant | None, template: MappingConfig | None = None, addon: bool = False,
                error: str | None = None) -> Mode:
    """``addon``: without a plant the add-on waits in ``setup``; standalone keeps the packaged mapping (the author's
    LXC hub until the migration)."""
    if error is not None:
        return Mode(SETUP, error=error)
    if plant is None:
        if addon:
            return Mode(SETUP)
        return Mode(MAPPING) if profile.AVAILABLE else Mode(SETUP, error=NO_PROFILE)
    if plant.profile == LEGACY_PROFILE and not profile.AVAILABLE:
        return Mode(SETUP, error=NO_PROFILE)                # fail closed: never a generic plant in its place
    projection = legacy_mapping(plant, template)
    return Mode(PLANT_LEGACY, plant, projection) if projection is not None else Mode(PLANT, plant)


class LiveFanout:
    """Hands every Home Assistant update to all evaluators (the fixed v1 model and/or ``PlantLive``). The session
    subscribes to the union of their entities; an update counts as applied if one of them took it."""

    def __init__(self, *evaluators: Any) -> None:
        self.evaluators = [e for e in evaluators if e is not None]
        if not self.evaluators:
            raise ValueError("at least one evaluator is needed")

    def subscribed_entities(self) -> list[str]:
        return sorted({e for ev in self.evaluators for e in ev.subscribed_entities()})

    @property
    def connected(self) -> bool:
        return bool(self.evaluators[0].connected)

    def apply_state(self, entity_id: str, raw_state: str | None, unit: str | None, last_changed: datetime | None,
                    observed_at: datetime, *, bootstrap: bool = False, last_updated: datetime | None = None) -> bool:
        taken = [ev.apply_state(entity_id, raw_state, unit, last_changed, observed_at, bootstrap=bootstrap,
                                last_updated=last_updated) for ev in self.evaluators]      # all of them, no short cut
        return any(taken)

    def apply_unit(self, entity_id: str, unit: str | None, updated_at: datetime | None = None) -> bool:
        return any([ev.apply_unit(entity_id, unit, updated_at) for ev in self.evaluators])

    def apply_reported(self, entity_id: str, last_reported: datetime | None) -> None:
        for ev in self.evaluators:
            ev.apply_reported(entity_id, last_reported)

    def set_connected(self, connected: bool, now: datetime) -> None:
        for ev in self.evaluators:
            ev.set_connected(connected, now)


class NoLive:
    """The evaluator of ``setup``: nothing subscribed, nothing evaluated – the session only serves the interface."""

    def __init__(self) -> None:
        self.connected = False

    def subscribed_entities(self) -> list[str]:
        return []

    def apply_state(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def apply_unit(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def apply_reported(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set_connected(self, connected: bool, now: datetime) -> None:
        self.connected = connected


class NotConfigured:
    """ASGI guard of ``setup``: the read API answers 503 ``not_configured`` (the apps say so); health checks pass."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith(("/v1/", "/v2/")):
            body = b'{"detail":"not_configured"}'
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class V1Unavailable:
    """ASGI guard for a plant the fixed v1 evaluation cannot describe: ``/v1`` answers 503 instead of presenting
    the values of another plant. Health checks and ``/v2`` pass."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith("/v1/"):
            body = b'{"detail":"v1_not_available"}'
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
