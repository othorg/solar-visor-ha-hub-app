"""Read-only recorder access (P3 step 1): splitting by time, raw states with their start state, statistics rows,
one heavy request at a time over the live session, clean-up after a timeout."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
import websockets

from solar_visor_hub.ha_client import HaClient, HaReadError
from solar_visor_hub.ha_history import RAW_CHUNK, StatRow, raw_states, statistic_ids, statistics
from solar_visor_hub.ops_allowlist import HA_WS_ALLOWED
from solar_visor_hub.plant import parse
from solar_visor_hub.plant_live import PlantLive
from solar_visor_hub.plant_samples import MINIMAL
from solar_visor_hub.runtime import LiveFanout

DAY = datetime(2026, 11, 14, 23, 0, tzinfo=UTC)          # 2026-11-15 00:00 Europe/Berlin
GRID = "sensor.grid"


class Recorder:
    """Answers like Home Assistant, from a list of (time, state) per entity and statistics rows per id."""

    def __init__(self, states=None, stats=None):
        self.states, self.stats, self.calls = states or {}, stats or {}, []

    async def read(self, msg):
        self.calls.append(msg)
        if msg["type"] == "recorder/list_statistic_ids":
            return [{"statistic_id": "sensor.grid", "has_mean": True, "has_sum": False,
                     "statistics_unit_of_measurement": "kW", "unit_class": "power"}, {"no": "id"}]
        start = datetime.fromisoformat(msg["start_time"])
        end = datetime.fromisoformat(msg["end_time"])
        if msg["type"] == "history/history_during_period":
            out = {}
            for eid in msg["entity_ids"]:
                rows = [{"s": s, "lu": t.timestamp()} for t, s in self.states.get(eid, []) if start <= t < end]
                before = [s for t, s in self.states.get(eid, []) if t < start]
                if msg.get("include_start_time_state") and before:
                    rows.insert(0, {"s": before[-1], "lu": start.timestamp()})
                if rows:
                    out[eid] = rows
            return out
        if msg["type"] == "recorder/statistics_during_period":
            return {sid: [r for r in rows if start.timestamp() * 1000 <= r["start"] < end.timestamp() * 1000]
                    for sid, rows in self.stats.items() if sid in msg["statistic_ids"]}
        if msg["type"] == "recorder/list_statistic_ids":
            return [{"statistic_id": "sensor.grid", "has_mean": True, "has_sum": False,
                     "statistics_unit_of_measurement": "kW", "unit_class": "power"}, {"no": "id"}]
        raise AssertionError(msg["type"])


async def test_raw_states_come_in_slices_with_the_start_state_only_once():
    states = {GRID: [(DAY - timedelta(minutes=10), "-300"), (DAY + timedelta(hours=1), "250"),
                     (DAY + timedelta(hours=7), "unavailable"), (DAY + timedelta(hours=20), "40")]}
    rec = Recorder(states)
    rows = await raw_states(rec.read, GRID, DAY, DAY + timedelta(days=1))
    assert len(rec.calls) == 4 and all(c["entity_ids"] == [GRID] for c in rec.calls)      # 6-hour slices
    assert [c["include_start_time_state"] for c in rec.calls] == [True, False, False, False]
    assert all(c["minimal_response"] and c["no_attributes"] and not c["significant_changes_only"] for c in rec.calls)
    assert {c["type"] for c in rec.calls} <= HA_WS_ALLOWED
    assert [(r.at, r.state, r.initial) for r in rows] == [
        (DAY, "-300", True),                               # HA's state at the start: the time is the request start
        (DAY + timedelta(hours=1), "250", False), (DAY + timedelta(hours=7), "unavailable", False),
        (DAY + timedelta(hours=20), "40", False)]


async def test_an_entity_without_history_gives_nothing_and_odd_rows_are_skipped():
    rec = Recorder()
    assert await raw_states(rec.read, GRID, DAY, DAY + timedelta(hours=2)) == []

    async def odd(msg):
        return {GRID: [{"s": "1"}, "junk", {"s": None, "lu": DAY.timestamp() + 60}, {"s": "2", "lu": "2026-11-14T23:30:00Z"}]}

    rows = await raw_states(odd, GRID, DAY, DAY + timedelta(hours=1))
    assert [(r.state, r.at) for r in rows] == [(None, DAY + timedelta(minutes=1)), ("2", DAY + timedelta(minutes=30))]


async def test_statistics_come_in_slices_always_in_watt_and_watt_hours():
    ms = lambda t: int(t.timestamp() * 1000)
    hours = [{"start": ms(DAY - timedelta(days=60) + timedelta(hours=i)), "end": ms(DAY - timedelta(days=60) + timedelta(hours=i + 1)),
              "mean": 1000.0 + i, "sum": None, "change": None, "state": True} for i in range(60 * 24)]
    rec = Recorder(stats={GRID: hours, "sensor.other": []})
    out = await statistics(rec.read, [GRID, "sensor.other"], DAY - timedelta(days=60), DAY, "hour", ("mean",))
    assert len(rec.calls) == 2 and all(c["units"] == {"power": "W", "energy": "Wh"} for c in rec.calls)   # 31-day slices
    assert all(c["period"] == "hour" and c["types"] == ["mean"] for c in rec.calls)
    assert len(out[GRID]) == 60 * 24 and out["sensor.other"] == []
    first = out[GRID][0]
    assert first == StatRow(start=DAY - timedelta(days=60), end=DAY - timedelta(days=60) + timedelta(hours=1), mean=1000.0)
    assert first.state is None                                    # a boolean is no number
    five = Recorder(stats={GRID: []})
    await statistics(five.read, [GRID], DAY - timedelta(days=14), DAY, "5minute")
    assert len(five.calls) == 2                                   # 7-day slices
    assert await statistics(five.read, [], DAY, DAY + timedelta(days=1), "hour") == {}


async def test_statistic_ids_keep_only_valid_rows():
    meta = await statistic_ids(Recorder().read)
    assert list(meta) == ["sensor.grid"] and meta["sensor.grid"]["statistics_unit_of_measurement"] == "kW"


# --- over the hub's live session --------------------------------------------------------------------------------

class FakeHA:
    def __init__(self, delay=0.0, fail=False):
        self.delay, self.fail = delay, fail
        self.active = self.max_active = 0
        self.received = []
        self.history_ids = []

    async def handler(self, ws):
        await ws.send(json.dumps({"type": "auth_required"}))
        await ws.recv()
        await ws.send(json.dumps({"type": "auth_ok"}))

        async def answer(msg):
            t, mid = msg["type"], msg.get("id")
            if t == "history/history_during_period":
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(self.delay)
                self.active -= 1
                if self.fail:
                    await ws.send(json.dumps({"id": mid, "type": "result", "success": False,
                                              "error": {"code": "invalid_format", "message": "bad request"}}))
                    return
                result = {GRID: [{"s": "5", "lu": DAY.timestamp()}]}
            elif t == "render_template":
                await ws.send(json.dumps({"id": mid, "type": "result", "success": True, "result": None}))
                await ws.send(json.dumps({"id": mid, "type": "event", "event": {"result": ""}}))
                return
            else:
                result = [] if t == "get_states" else ({"energy_sources": [], "device_consumption": []}
                                                       if t == "energy/get_prefs" else None)
            await ws.send(json.dumps({"id": mid, "type": "result", "success": True, "result": result}))

        async for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg["type"])
            if msg["type"] == "history/history_during_period":
                self.history_ids.append(msg["id"])
            asyncio.create_task(answer(msg))


async def connected(fake):
    server = await websockets.serve(fake.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = HaClient(f"http://127.0.0.1:{port}", lambda: "t", LiveFanout(PlantLive(parse(MINIMAL))),
                      freshness_interval_s=0.05)
    task = asyncio.create_task(client.run_forever())
    await asyncio.wait_for(client.connected_once.wait(), 5)
    return server, client, task


async def test_reads_go_over_the_live_session_one_heavy_request_at_a_time():
    fake = FakeHA(delay=0.1)
    server, client, task = await connected(fake)
    try:
        results = await asyncio.gather(*(raw_states(client.read, GRID, DAY, DAY + timedelta(hours=1)) for _ in range(3)))
        assert all(r[0].state == "5" for r in results)
        assert fake.max_active == 1                               # the live updates keep flowing between them
        assert len(fake.history_ids) == 3 and not set(fake.history_ids) & set(client._pending)
    finally:
        task.cancel()
        server.close()
    assert set(fake.received) <= HA_WS_ALLOWED


async def test_a_refusal_and_a_timeout_leave_nothing_behind():
    fake = FakeHA(fail=True)
    server, client, task = await connected(fake)
    try:
        with pytest.raises(HaReadError) as info:
            await raw_states(client.read, GRID, DAY, DAY + timedelta(hours=1))
        assert info.value.code == "invalid_format"
        fake.fail, fake.delay = False, 1.0
        with pytest.raises(TimeoutError):
            await client.read({"type": "history/history_during_period", "start_time": DAY.isoformat(),
                               "end_time": (DAY + RAW_CHUNK).isoformat(), "entity_ids": [GRID]}, timeout=0.1)
        assert fake.history_ids[-1] not in client._pending        # the timed-out call did not leave its future
    finally:
        task.cancel()
        server.close()


async def test_without_a_session_reading_fails_at_once():
    client = HaClient("http://127.0.0.1:9", lambda: "t", LiveFanout(PlantLive(parse(MINIMAL))))
    with pytest.raises(ConnectionError):
        await client.read({"type": "recorder/list_statistic_ids"})
