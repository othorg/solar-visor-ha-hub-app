"""Read routes of the dashboard card for Home Assistant (plan lovelace-card, P1): the card's renderer, the plant with the
Home Assistant sensors behind each node and the live snapshot – over Ingress only, for every active Home Assistant user
(``ui.IngressGuard``), GET only. Never on port A or B: the entity ids stay inside Home Assistant, where every user sees
them anyway.

The sensors of a node are the ones its history graph shows (as in the apps' node detail, ``PlantNodeDetail.history``):
powers and charge levels, no counters or limits. Home is calculated and has none: its curve comes from the hub instead
(``history/home``: today's calculated power next to the total consumption, as in the apps – user decision 2026-11-17).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .api_v2 import NOT_CONFIGURED, build_capabilities, snapshot_reply
from .models_v2 import PlantCapabilities, PlantPowerResponse, PlantSnapshot
from .periods import period_bounds, zone
from .plant import Kind, Module, Plant

CARD_PREFIX = "/card/v1/"
RENDERER = "card.js"
# nodes without Home Assistant sensors whose day curve the hub calculates (the apps' PlantNodeDetail.history)
NODE_HISTORY: dict[str, tuple[str, ...]] = {"home": ("consumption.power", "home.power")}
HISTORY_POINTS = 288                         # 5-minute columns over a day

# points per module kind, in the order the graph lists them (string powers come first for PV)
SENSOR_POINTS: dict[Kind, tuple[str, ...]] = {
    Kind.PV: ("power",),
    Kind.BATTERY: ("soc", "power"),
    Kind.GENERIC: ("storage_soc", "power", "storage_power"),
    Kind.GRID: ("power", "import_power", "export_power"),
    Kind.WALLBOX: ("power",),
    Kind.CONSUMPTION: ("power",),
}


class CardSensor(BaseModel):
    entity_id: str
    metric_id: str


class CardPlant(BaseModel):
    build: str                                   # the card reloads its renderer when the hub's build changes
    capabilities: PlantCapabilities
    sensors: dict[str, list[CardSensor]]         # node id → Home Assistant sensors of its history graph


def _module_sensors(m: Module) -> list[CardSensor]:
    out: list[CardSensor] = []
    if m.kind is Kind.PV:
        for s in m.strings:
            if (p := s.points.get("power")) is not None:
                out.append(CardSensor(entity_id=p.entity, metric_id=f"{m.id}.{s.id}.power"))
    for pid in SENSOR_POINTS.get(m.kind, ()):
        if (p := m.points.get(pid)) is not None:
            out.append(CardSensor(entity_id=p.entity, metric_id=f"{m.id}.{pid}"))
    return out


def card_sensors(plant: Plant) -> dict[str, list[CardSensor]]:
    """Node id → its sensors (each entity once per node); nodes without one are left out."""
    nodes: dict[str, list[CardSensor]] = {}
    generation: list[CardSensor] = []
    for m in plant.active():
        sensors = _module_sensors(m)
        if m.kind is Kind.PV and (p := m.points.get("power")) is not None:
            generation.append(CardSensor(entity_id=p.entity, metric_id=f"{m.id}.power"))
        if sensors:
            nodes[m.id] = sensors
    if generation:
        nodes["generation"] = generation
    for node, sensors in nodes.items():
        seen: set[str] = set()
        nodes[node] = [s for s in sensors if not (s.entity_id in seen or seen.add(s.entity_id))]
    return nodes


Power = Callable[[Plant, Sequence[str], datetime, datetime, int], Awaitable[PlantPowerResponse]]


@dataclass
class CardContext:
    plant: Callable[[], Plant | None]                          # the running plant (None: not configured)
    snapshot: Callable[[], PlantSnapshot] | None               # PlantLive snapshot (None: no live data)
    timezone: Callable[[], str | None]
    build: Callable[[], str]
    power: Power | None = None                                 # plant_power.power_reply with the plant's history
    # the zone the history computes its days in (the existing calculation keeps its own); default: Home Assistant's
    history_timezone: Callable[[], str | None] | None = None
    history_errors: tuple[type[BaseException], ...] = ()       # a failing history source: 503
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


def register(app: FastAPI, ctx: CardContext, web_dir: Path) -> None:
    @app.get(CARD_PREFIX + RENDERER, response_model=None)
    def renderer() -> FileResponse:
        path = web_dir / "card" / RENDERER
        if not path.is_file():
            raise HTTPException(status_code=404, detail="card not built")
        # the loader asks for it with ?v=<build>: no-cache keeps a stale copy from outliving an update
        return FileResponse(path, media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get(CARD_PREFIX + "plant", response_model=CardPlant)
    def plant() -> CardPlant:
        p = ctx.plant()
        if p is None or ctx.snapshot is None:
            raise HTTPException(status_code=503, detail=NOT_CONFIGURED)
        return CardPlant(build=ctx.build(), capabilities=build_capabilities(p, ctx.timezone()), sensors=card_sensors(p))

    # async: runs on the event loop thread that also applies HA updates → no concurrent mutation (as /v2/snapshot)
    @app.get(CARD_PREFIX + "snapshot", response_model=PlantSnapshot)
    async def snapshot(response: Response, if_none_match: str | None = Header(default=None)) -> Any:
        if ctx.snapshot is None:
            raise HTTPException(status_code=503, detail=NOT_CONFIGURED)
        return snapshot_reply(ctx.snapshot(), response, if_none_match)


    @app.get(CARD_PREFIX + "history/{node}", response_model=PlantPowerResponse)
    async def history(node: str) -> PlantPowerResponse:
        """Today's calculated power of a node without sensors, from midnight (the plant's time zone) until now."""
        from .plant_power import NoHistory, PowerSeriesError
        p = ctx.plant()
        if p is None or ctx.snapshot is None:
            raise HTTPException(status_code=503, detail=NOT_CONFIGURED)
        ids = [m for m in NODE_HISTORY.get(node, ()) if m in build_capabilities(p, ctx.timezone()).metrics]
        if not ids:
            raise HTTPException(status_code=404, detail="no calculated history for this node")
        if ctx.power is None:
            raise HTTPException(status_code=503, detail="history unavailable")
        now = ctx.now()
        tz = zone((ctx.history_timezone or ctx.timezone)())
        start, _ = period_bounds("day", now.astimezone(tz).date(), tz)
        try:
            return await ctx.power(p, ids, start, max(now, start + timedelta(minutes=1)), HISTORY_POINTS)
        except PowerSeriesError:
            raise HTTPException(status_code=404, detail="no calculated history for this node") from None
        except (NoHistory, *ctx.history_errors) as exc:
            raise HTTPException(status_code=503, detail="history unavailable") from exc


def is_card_path(path: str) -> bool:
    return path.startswith(CARD_PREFIX)
