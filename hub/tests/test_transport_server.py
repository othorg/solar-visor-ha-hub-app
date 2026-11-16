import json
from datetime import datetime

import pytest

from solar_visor_hub.models import FlowEdge, MetricPoint
from solar_visor_hub.ops_allowlist import ForbiddenOperation
from solar_visor_hub.server import BindError, load_binds
from solar_visor_hub.transport import GuardedHaSocket, GuardedInflux


async def test_guarded_ha_socket_blocks_writes_before_sending():
    sent: list[str] = []

    async def sender(raw: str) -> None:
        sent.append(raw)

    ws = GuardedHaSocket(sender)
    assert await ws.send({"type": "get_states"}) == 1
    with pytest.raises(ForbiddenOperation):
        await ws.send({"type": "call_service", "domain": "select", "service": "select_option"})
    assert len(sent) == 1 and json.loads(sent[0])["type"] == "get_states"


async def test_guarded_influx_blocks_writes_before_sending():
    calls: list[tuple[str, dict]] = []

    async def http_get(path: str, query: dict) -> dict:
        calls.append((path, query))
        return {}

    db = GuardedInflux(http_get, "solar", "autogen")
    await db.query('SELECT last("value") FROM "x" WHERE "entity_id" = $eid', {"eid": "y"})
    with pytest.raises(ForbiddenOperation):
        await db.query("DROP MEASUREMENT x")
    assert len(calls) == 1 and json.loads(calls[0][1]["params"]) == {"eid": "y"}


def test_admin_bind_rules():
    pub, adm = load_binds({"SOLAR_VISOR_ADMIN_BIND": "192.168.1.13:8081"})
    assert (pub.port, adm.host, adm.port) == (8080, "192.168.1.13", 8081)
    for bad in ("0.0.0.0:8081", "[::]:8081", "8.8.8.8:8081", "hub.local:8081", "192.168.1.13:8080"):
        with pytest.raises(BindError):
            load_binds({"SOLAR_VISOR_ADMIN_BIND": bad})
    with pytest.raises(BindError):
        load_binds({})


def _metric(**overrides):
    base = dict(metric_id="m", value=1.0, unit="W", source_id="s", measurement_boundary="b",
                observed_at="2026-10-03T07:30:00+02:00", state="ok")
    base.update(overrides)
    return MetricPoint.model_validate(base)


def test_timestamps_must_be_aware_and_are_normalised_to_utc():
    m = _metric()
    assert m.observed_at.utcoffset().total_seconds() == 0 and m.observed_at.hour == 5
    with pytest.raises(ValueError):
        _metric(observed_at="2026-10-03T07:30:00")  # naive → rejected
    assert isinstance(m.observed_at, datetime)


def test_nan_rejected_in_all_numeric_fields():
    with pytest.raises(ValueError):
        FlowEdge(edge_id="E1", from_node="a", to_node="b", power_w=float("nan"), state="ok")
    with pytest.raises(ValueError):
        FlowEdge(edge_id="E1", from_node="a", to_node="b", power_w=float("inf"), state="ok")
