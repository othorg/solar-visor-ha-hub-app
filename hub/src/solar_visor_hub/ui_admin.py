"""Overview, history and "devices & diagnostics" of the web interface (P4 detail plan §3, step 5).

Everything here exists for the app administration (port B) or the command line already – the interface reuses it:
devices and pairing (``DeviceStore``), connection tests, the InfluxDB connection (address, database, retention policy –
add-on only, formerly its Home Assistant options) and token, the E3/DC credentials (write-only, ``rscp_admin.py``), the
generic history's archive (window, summary, recomputation).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import ConfigError, SecretFiles, store_secret
from .influx_config import InfluxConfig, InfluxConnection, TokenRequired
from .influx_config import validate as validate_influx
from .periods import zone
from .models import RscpConfigUpdate, RscpConfigView
from .rscp_admin import RscpAdmin, RscpUpdateError

log = logging.getLogger("solar_visor_hub.ui")
Probe = Callable[[], Awaitable[tuple[str, str]]]
OVERVIEW_METRICS = ("generation.power", "consumption.power", "home.power", "grid.power", "battery.power", "battery.soc")
MAX_RECOMPUTE_DAYS = 366


@dataclass
class AdminContext:
    mode: str
    snapshot: Callable[[], Any] | None = None          # PlantLive snapshot (None in setup)
    module_names: Callable[[], dict[str, str]] = dict  # module id → the name the user gave it
    ha_connected: Callable[[], bool] = lambda: False
    timezone: Callable[[], str | None] = lambda: None
    plant_history: Any = None                          # PlantHistory (mode plant only)
    devices: Any = None                                # DeviceStore
    test_ha: Probe | None = None
    test_influx: Probe | None = None
    secrets: SecretFiles | None = None
    rscp: RscpAdmin | None = None
    influx: InfluxConnection | None = None             # add-on: address and token of the InfluxDB history


class DeviceView(BaseModel):
    id: int
    name: str
    scope: str
    created_at: str
    last_used_at: str | None


class TestResult(BaseModel):
    status: str
    detail: str


class SecretIn(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


class InfluxIn(BaseModel):
    url: str = Field(max_length=200)
    database: str = Field(max_length=64)
    retention_policy: str | None = Field(default=None, max_length=64)
    token: str | None = Field(default=None, min_length=1, max_length=4096)   # write-only


class InfluxView(BaseModel):
    url: str
    database: str
    retention_policy: str | None
    token_set: bool


class RecomputeIn(BaseModel):
    from_: date = Field(alias="from")
    to: date


def register(app: FastAPI, ctx: AdminContext) -> None:
    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        """Connections, the main live values with their state and the balance residual of the home (a generator with
        full feed-in is outside it)."""
        snap = ctx.snapshot() if ctx.snapshot is not None else None
        live = []
        if snap is not None:
            # the totals first, then the power of every module (not its strings or detail powers)
            ids = [m for m in OVERVIEW_METRICS if m in snap.metrics] + sorted(
                m for m, v in snap.metrics.items() if m.count(".") == 1 and m.endswith(".power") and not v.derived
                and m not in OVERVIEW_METRICS)
            names = ctx.module_names()
            live = [{"metric_id": m, "name": names.get(m.split(".")[0]) if m not in OVERVIEW_METRICS else None,
                     "value": snap.metrics[m].value, "unit": snap.metrics[m].unit.value,
                     "state": snap.metrics[m].state.value, "reasons": snap.metrics[m].reasons} for m in ids]
        return {"mode": ctx.mode, "ha_connected": ctx.ha_connected(), "timezone": ctx.timezone(), "live": live,
                "balance_residual_w": snap.balance_residual_w if snap is not None else None,
                "not_ok": [m for m, v in snap.metrics.items() if v.state.value != "ok"] if snap is not None else []}

    @app.get("/api/history")
    def history() -> dict[str, Any]:
        h = ctx.plant_history
        if ctx.mode == "plant_legacy":
            return {"source": "existing_calculation", "window_start": None, "archive": None}
        if h is None:
            return {"source": None, "window_start": None, "archive": None}
        return {"source": "recorder", "window_start": h.window_start, "archive": h.archive.summary(),
                "timezone": ctx.timezone()}

    @app.post("/api/history/recompute")
    def recompute(body: RecomputeIn) -> dict[str, int]:
        """Archived days from … to (inclusive) are computed anew on their next request – with the plant of their time."""
        h = ctx.plant_history
        if h is None:
            raise HTTPException(status_code=409, detail={"code": "no_generic_history"})
        days = (body.to - body.from_).days + 1
        today = datetime.now(zone(ctx.timezone())).date()            # the plant's calendar, not UTC
        if days < 1 or days > MAX_RECOMPUTE_DAYS or body.to > today:
            raise HTTPException(status_code=422, detail={"code": "invalid_range"})
        marked = h.archive.mark_stale([body.from_ + timedelta(days=i) for i in range(days)])
        log.info("%d archived day(s) marked for recomputation from the interface", marked)
        return {"days": marked}

    @app.get("/api/devices", response_model=list[DeviceView])
    def devices() -> list[DeviceView]:
        if ctx.devices is None:
            raise HTTPException(status_code=503, detail="devices unavailable")
        return [DeviceView(id=d.device_id, name=d.name, scope=d.scope, created_at=d.created_at, last_used_at=d.last_used_at)
                for d in ctx.devices.list_devices() if not d.revoked]

    @app.delete("/api/devices/{device_id}", status_code=204)
    def revoke(device_id: int) -> None:
        if ctx.devices is None:
            raise HTTPException(status_code=503, detail="devices unavailable")
        if not ctx.devices.revoke(device_id):
            raise HTTPException(status_code=404, detail="unknown device")
        log.info("device %s revoked from the interface", device_id)

    async def run(probe: Probe | None) -> TestResult:
        if probe is None:
            raise HTTPException(status_code=503, detail="test unavailable")
        status, detail = await probe()
        return TestResult(status=status, detail=detail)

    @app.post("/api/test/ha", response_model=TestResult)
    async def test_ha() -> TestResult:
        return await run(ctx.test_ha)

    @app.post("/api/test/influx", response_model=TestResult)
    async def test_influx() -> TestResult:
        return await run(ctx.test_influx)

    @app.get("/api/secrets")
    def secrets() -> dict[str, bool]:
        """Only whether a secret exists – never its content."""
        sec = ctx.secrets
        path = sec.influx_token if sec is not None else None
        return {"influx_token": bool(path is not None and path.exists() and path.stat().st_size > 0)}

    @app.put("/api/secrets/influx", status_code=204)
    def set_influx(body: SecretIn) -> None:
        if ctx.secrets is None:
            raise HTTPException(status_code=503, detail="secrets unavailable")
        target = ctx.secrets.influx_token
        try:
            target.parent.mkdir(mode=0o700, exist_ok=True)
            store_secret(target, body.value)
        except ConfigError:
            raise HTTPException(status_code=422, detail={"code": "invalid_secret"}) from None
        except OSError:
            raise HTTPException(status_code=503, detail="secret not writable on the hub") from None
        log.info("InfluxDB token stored from the interface")

    def influx_view() -> InfluxView:
        if ctx.influx is None:
            raise HTTPException(status_code=503, detail="influx settings unavailable")
        cfg, _ = ctx.influx.config()
        token = ctx.secrets.influx_token if ctx.secrets is not None else None
        return InfluxView(url=cfg.url, database=cfg.database, retention_policy=cfg.retention_policy,
                          token_set=bool(token is not None and token.exists() and token.stat().st_size > 0))

    @app.get("/api/influx", response_model=InfluxView)
    def influx_get() -> InfluxView:
        return influx_view()

    @app.put("/api/influx", response_model=InfluxView)
    def influx_put(body: InfluxIn) -> InfluxView:
        if ctx.influx is None or ctx.secrets is None:
            raise HTTPException(status_code=503, detail="influx settings unavailable")
        try:
            cfg = validate_influx(InfluxConfig(url=body.url, database=body.database,
                                               retention_policy=body.retention_policy))
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_influx", "message": str(exc)}) from None
        try:
            # like the E3/DC credentials: a stored token never goes to another address unless it is entered again –
            # checked and written under the connection's lock (concurrent saves cannot mix the pairs)
            ctx.influx.update(cfg, body.token)
        except TokenRequired:
            raise HTTPException(status_code=422, detail={"code": "token_required",
                                                         "message": "re-enter the token for a new address"}) from None
        except ConfigError:
            raise HTTPException(status_code=422, detail={"code": "invalid_secret"}) from None
        except OSError:
            raise HTTPException(status_code=503, detail="influx settings not writable on the hub") from None
        log.info("InfluxDB connection stored from the interface%s", " with a new token" if body.token else "")
        return influx_view()

    @app.get("/api/rscp", response_model=RscpConfigView)
    def rscp_view() -> RscpConfigView:
        if ctx.rscp is None:
            raise HTTPException(status_code=503, detail="rscp unavailable")
        return ctx.rscp.view()

    @app.put("/api/rscp", response_model=RscpConfigView)
    def rscp_update(body: RscpConfigUpdate) -> RscpConfigView:
        if ctx.rscp is None:
            raise HTTPException(status_code=503, detail="rscp unavailable")
        try:
            return ctx.rscp.update(body)
        except RscpUpdateError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from None
