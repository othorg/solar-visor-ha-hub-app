"""Live/history precedence K2 (plan §3.4): persistent hub observations reconciled against InfluxDB.

InfluxDB only stores changes, so a lost change (e.g. 6 kW → 0 during an InfluxDB outage) would make
sample-and-hold invent energy. The hub therefore records every numeric state change it observes on the
subscription (not bootstrap values) and reconciles closed intervals:

- event present in InfluxDB (same value, ±1 s because of ``precision: s``) → confirmed, row deleted;
- event missing in InfluxDB → InfluxDB gap: the row is kept permanently with origin ``hub``; the energy
  engine uses InfluxDB points ∪ these rows (only events InfluxDB lacks → the gap interval is replaced,
  nothing is counted twice);
- query failure → rows stay pending (never adopted on error, never deleted only because of age).
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import math
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

RECONCILE_AFTER = timedelta(minutes=10)
BATCH_PER_ENTITY = 2000         # bounded work per round (backlogs after long InfluxDB outages)
MATCH_WINDOW_S = 1.0
BINARY = {"on": 1.0, "off": 0.0}

# fetch(measurement, start, end) -> [(t, value)] – InfluxDB points in [start, end]
Fetch = Callable[[str, datetime, datetime], Awaitable[list[tuple[datetime, float]]]]


def numeric(entity: str, raw_state: str | None) -> float | None:
    """The value HA writes to InfluxDB's ``value`` field (binary sensors as 1/0), else None."""
    if raw_state is None:
        return None
    s = raw_state.strip().lower()
    if entity.startswith("binary_sensor."):
        return BINARY.get(s)
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


class ObservationStore:
    def __init__(self, path: Path, entities: set[str] | None = None) -> None:
        self.path = path
        # only engine inputs: a live-only entity (not in InfluxDB, e.g. the motorhome Shelly) would otherwise be
        # adopted as a permanent "gap" row on every change
        self.entities = entities
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, entity TEXT NOT NULL, t_utc TEXT NOT NULL, value REAL NOT NULL,
                    unit TEXT, origin TEXT NOT NULL DEFAULT 'pending' CHECK (origin IN ('pending', 'hub')));
                CREATE INDEX IF NOT EXISTS ix_obs_entity_time ON observations (entity, t_utc);
                CREATE INDEX IF NOT EXISTS ix_obs_origin ON observations (origin, t_utc);
            """)
        self._buffer: list[tuple[str, str, float, str | None]] = []

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def record(self, entity: str, t: datetime, raw_state: str | None, unit: str | None) -> None:
        if self.entities is not None and entity not in self.entities:
            return
        v = numeric(entity, raw_state)
        if v is not None:
            self._buffer.append((entity, t.astimezone(UTC).isoformat(), v, unit))

    def flush(self) -> int:
        if not self._buffer:
            return 0
        rows, self._buffer = self._buffer, []
        try:
            with closing(self._db()) as db, db:
                db.executemany("INSERT INTO observations (entity, t_utc, value, unit) VALUES (?, ?, ?, ?)", rows)
        except sqlite3.Error:
            self._buffer = rows + self._buffer      # keep for the next flush
            raise
        return len(rows)

    def pending(self, before: datetime, limit: int = BATCH_PER_ENTITY) -> dict[str, list[tuple[int, datetime, float]]]:
        with closing(self._db()) as db:
            entities = [r[0] for r in db.execute("SELECT DISTINCT entity FROM observations WHERE origin='pending' "
                                                 "AND t_utc < ?", (before.isoformat(),))]
            out: dict[str, list[tuple[int, datetime, float]]] = {}
            for e in entities:
                rows = db.execute("SELECT id, t_utc, value FROM observations WHERE origin='pending' AND entity=? "
                                  "AND t_utc < ? ORDER BY t_utc LIMIT ?", (e, before.isoformat(), limit)).fetchall()
                out[e] = [(i, datetime.fromisoformat(t), v) for i, t, v in rows]
        return out

    def resolve(self, confirmed: list[int], adopted: list[int]) -> None:
        with closing(self._db()) as db, db:
            db.executemany("DELETE FROM observations WHERE id=?", [(i,) for i in confirmed])
            db.executemany("UPDATE observations SET origin='hub' WHERE id=?", [(i,) for i in adopted])

    def hub_points(self, entity: str, start: datetime, end: datetime) -> list[tuple[datetime, float, str | None]]:
        with closing(self._db()) as db:
            rows = db.execute("SELECT t_utc, value, unit FROM observations WHERE origin='hub' AND entity=? "
                              "AND t_utc >= ? AND t_utc < ? ORDER BY t_utc",
                              (entity, start.isoformat(), end.isoformat())).fetchall()
        return [(datetime.fromisoformat(t), v, u) for t, v, u in rows]

    def counts(self) -> dict[str, int]:
        with closing(self._db()) as db:
            return dict(db.execute("SELECT origin, count(*) FROM observations GROUP BY origin").fetchall())


def _matched(points: list[tuple[datetime, float]], times: list[datetime], t: datetime, v: float) -> bool:
    i = bisect.bisect_left(times, t - timedelta(seconds=MATCH_WINDOW_S))
    while i < len(points) and points[i][0] <= t + timedelta(seconds=MATCH_WINDOW_S):
        if abs(points[i][1] - v) <= 1e-6 * max(1.0, abs(v)):
            return True
        i += 1
    return False


Adopted = list[tuple[str, datetime]]


async def reconcile(store: ObservationStore, fetch: Fetch, now: datetime,
                    on_adopted: Callable[[Adopted], None] | None = None) -> tuple[int, int, int]:
    """Returns (confirmed, adopted, failed entities). Never adopts on query errors. ``on_adopted`` receives
    (entity, timestamp) of adopted observations (cached energy results covering them must be invalidated)."""
    confirmed: list[int] = []
    adopted: list[tuple[int, datetime, str]] = []
    failed = 0
    for entity, rows in store.pending(now - RECONCILE_AFTER).items():
        start, end = rows[0][1] - timedelta(seconds=2), rows[-1][1] + timedelta(seconds=2)
        try:
            points = sorted(await fetch(entity, start, end))
        except Exception:   # noqa: BLE001 – any query failure: keep pending, retry later (logged, no secrets)
            log.warning("reconcile: InfluxDB query failed for %s – retry later", entity)
            failed += 1
            continue
        times = [p[0] for p in points]
        for oid, t, v in rows:
            if _matched(points, times, t, v):
                confirmed.append(oid)
            else:
                adopted.append((oid, t, entity))
        await asyncio.sleep(0)            # yield to live processing between entities
    if confirmed or adopted:
        store.resolve(confirmed, [i for i, _, _ in adopted])
    if adopted:
        log.warning("reconcile: %d observed changes missing in InfluxDB kept as origin 'hub'", len(adopted))
        if on_adopted:
            on_adopted([(e, t) for _, t, e in adopted])
    return len(confirmed), len(adopted), failed


async def run_reconciler(store: ObservationStore, fetch: Fetch, interval_s: float = 300,
                         on_adopted: Callable[[Adopted], None] | None = None) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await reconcile(store, fetch, datetime.now(UTC), on_adopted)
        except sqlite3.Error:
            log.exception("reconcile: local store error – continuing")
