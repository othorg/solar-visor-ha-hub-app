"""Archive of computed days for a plant without the existing history (plan ha-addon P3 step 4, detail plan §5).

The recorder keeps raw states and 5-minute statistics for a limited time (14 days measured); the archive keeps the
15-minute days the hub computed from them for good. It is data, not a cache – part of the backup.

Lifecycle of a day (user decision 2026-11-15, 3):
- not final (the running day, and the previous one until 6 h after its end): recomputed when older than a few minutes;
- final and computed from the keep window (``durable``): frozen – a later change of the plant never recomputes it
  automatically; only an explicit request (``mark_stale``) does, at the cost of resolution;
- final and computed from hourly statistics: recomputed when the plant's sources for that day or the calculation
  version changed (``source_key``).

The plant's revisions are logged with the time they were first seen: a day is computed with the modules that were
active at its start, so a module switched off later stays visible in old periods (user decision 2026-11-15, 4).

A sensor changed "retroactively from day X" (user decision 2026-11-15, P4) is a **source override**: metric, the new
point, valid from X, created at C. ``plant_at(t)`` applies it to a revision seen before C that has the point, if X ≤ t –
a later change "from now on" is seen after C and stays untouched. Overrides apply in the order they were created; the
days from X on are marked for recomputation (also frozen ones: they would read the old sensor otherwise). Overrides
belong to the plant revision that brought them: restoring the previous plant withdraws them again. An override that
would make an old revision invalid is refused at activation and skipped (logged) should one exist anyway – the history
must never fail on it (Codex P4 review M2, M6). A computation that began before overrides changed writes its day as
stale (``override_gen``), so it cannot undo the invalidation (N4).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import zlib
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import Any
from collections.abc import Callable

from .daycalc import SlotEnergy
from .plant import Plant, PlantError, dump, parse
from .plant_daycalc import TIER_RAW, CategorySource, PlantDay, requirements

CALCULATION_VERSION = 1
SCHEMA = 1
log = logging.getLogger("solar_visor_hub.plant_archive")


@dataclass(frozen=True)
class ArchivedDay:
    """An archived day; its slots are decoded on first use (deciding whether it is usable needs only the head)."""
    payload: bytes = field(repr=False)
    tz: str
    tier: str
    computed_at: datetime
    final: bool
    durable: bool
    calc_version: int
    plant_revision: int
    source_key: str

    @cached_property
    def day(self) -> PlantDay:
        return _decode(self.payload)


def source_key(plant: Plant, tier: str) -> str:
    """What a day's numbers depend on: the points read, with entity, sign, unit and validity – not the names."""
    pts = plant.points()
    parts = sorted((mid, sorted(kinds), pts[mid].entity, pts[mid].sign, pts[mid].unit.value,
                    pts[mid].valid_from.isoformat() if pts[mid].valid_from else None)
                   for mid, kinds in requirements(plant, tier).items())
    balances = sorted((m.id, m.balance or "", m.variant or "") for m in plant.active())
    raw = json.dumps([tier, CALCULATION_VERSION, parts, balances], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _encode(day: PlantDay) -> bytes:
    cats = {}
    for name, slots in day.categories.items():
        cats[name] = {
            "wh": [e.wh for e in slots], "cov": [round(e.coverage, 6) for e in slots],
            "unres": [round(e.unresolved_wh, 3) for e in slots], "app": [e.applicable_s for e in slots],
            "method": [e.method for e in slots], "alloc": [e.time_allocated for e in slots],
            "conf": [e.confidence for e in slots], "reasons": [sorted(e.reasons) for e in slots],
        }
    payload = {"tier": day.tier, "resolution_s": day.resolution_s, "categories": cats,
               "sources": {k: [v.method, v.metric_id, v.resolution_s] for k, v in day.sources.items()},
               "kpi_parts": day.kpi_parts, "counter_anomalies": day.counter_anomalies}
    return zlib.compress(json.dumps(payload, separators=(",", ":")).encode(), 6)


def _decode(blob: bytes) -> PlantDay:
    p = json.loads(zlib.decompress(blob))
    cats: dict[str, list[SlotEnergy]] = {}
    for name, c in p["categories"].items():
        cats[name] = [SlotEnergy(wh, cov, unres, method, alloc, conf, set(reasons), app) for wh, cov, unres, method, alloc,
                      conf, reasons, app in zip(c["wh"], c["cov"], c["unres"], c["method"], c["alloc"], c["conf"],
                                                c["reasons"], c["app"], strict=True)]
    return PlantDay(tier=p["tier"], resolution_s=p["resolution_s"], categories=cats,
                    sources={k: CategorySource(*v) for k, v in p["sources"].items()},
                    kpi_parts={k: [tuple(r) for r in rows] for k, rows in p["kpi_parts"].items()},   # type: ignore[misc]
                    counter_anomalies=p["counter_anomalies"])


def _apply(raw: dict[str, Any], metric_id: str, point: dict[str, Any]) -> dict[str, Any]:
    mid, _, pid = metric_id.partition(".")
    points = (raw.get("modules", {}).get(mid) or {}).get("points") or {}
    if pid in points:
        points[pid] = point
    return raw


def _apply_all(raw: dict[str, Any], overrides: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    for metric_id, point in overrides:
        _apply(raw, metric_id, point)
    return raw


def _with_overrides(raw: dict[str, Any], overrides: list[tuple[str, dict[str, Any]]]) -> Plant:
    """Applies the overrides one after the other; one that would make the revision invalid is skipped (logged)."""
    plant = parse(raw)
    for metric_id, point in overrides:
        trial = _apply(json.loads(json.dumps(raw)), metric_id, point)
        try:
            plant, raw = parse(trial), trial
        except PlantError as exc:
            log.warning("source override of %s skipped for revision %s: %s", metric_id, raw.get("revision"),
                        ",".join(sorted(exc.codes)))
    return plant


class PlantArchive:
    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(self._db()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS days (local_date TEXT PRIMARY KEY, tz TEXT NOT NULL, tier TEXT NOT NULL,
                    final INTEGER NOT NULL, durable INTEGER NOT NULL, stale INTEGER NOT NULL DEFAULT 0,
                    calc_version INTEGER NOT NULL, plant_revision INTEGER NOT NULL, source_key TEXT NOT NULL,
                    computed_at TEXT NOT NULL, payload BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS plant_revisions (revision INTEGER PRIMARY KEY, seen_at TEXT NOT NULL,
                    plant TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS point_overrides (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_id TEXT NOT NULL, point TEXT NOT NULL, effective_from TEXT NOT NULL, created_at TEXT NOT NULL,
                    revision INTEGER, active INTEGER NOT NULL DEFAULT 1);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(point_overrides)")}
            if "revision" not in columns:                 # archives of the first P4 build
                db.execute("ALTER TABLE point_overrides ADD COLUMN revision INTEGER")
            if "active" not in columns:
                db.execute("ALTER TABLE point_overrides ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('override_gen', '0')")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    # --- plant revisions ------------------------------------------------------------------------------------------
    def record_plant(self, plant: Plant, seen_at: datetime) -> None:
        with closing(self._db()) as db, db:
            db.execute("INSERT OR IGNORE INTO plant_revisions VALUES (?, ?, ?)",
                       (plant.revision, seen_at.isoformat(), json.dumps(dump(plant))))

    def plant_at(self, t: datetime, current: Plant) -> Plant:
        """The plant that was active at ``t``: the newest revision seen at or before ``t``, else the oldest known – with
        the source overrides that reach back to ``t``."""
        with closing(self._db()) as db:
            row = db.execute("SELECT plant, seen_at FROM plant_revisions WHERE seen_at <= ? ORDER BY seen_at DESC, "
                             "revision DESC LIMIT 1", (t.isoformat(),)).fetchone() or db.execute(
                "SELECT plant, seen_at FROM plant_revisions ORDER BY seen_at, revision LIMIT 1").fetchone()
            overrides = db.execute("SELECT metric_id, point, created_at FROM point_overrides WHERE active = 1 AND "
                                   "effective_from <= ? ORDER BY created_at, id", (t.isoformat(),)).fetchall()
        if not row:
            return current
        seen = datetime.fromisoformat(row[1])
        return _with_overrides(json.loads(row[0]), [(m, json.loads(p)) for m, p, c in overrides
                                                    if datetime.fromisoformat(c) > seen])

    def check_overrides(self, candidates: list[tuple[str, dict[str, Any]]]) -> list[str]:
        """Metric ids whose new point would make a known revision invalid (e.g. a sensor counted twice there)."""
        with closing(self._db()) as db:
            revisions = [json.loads(r[0]) for r in db.execute("SELECT plant FROM plant_revisions")]
            existing = [(m, json.loads(p)) for m, p in db.execute(
                "SELECT metric_id, point FROM point_overrides WHERE active = 1 ORDER BY created_at, id")]
        bad: list[str] = []
        for raw in revisions:
            for metric_id, point in candidates:
                trial = _apply(_apply_all(json.loads(json.dumps(raw)), existing), metric_id, point)
                try:
                    parse(trial)
                except PlantError:
                    if metric_id not in bad:
                        bad.append(metric_id)
        return bad

    def add_overrides(self, overrides: list[tuple[str, dict[str, Any], datetime]], revision: int, created_at: datetime,
                      stale_from: date) -> int:
        """Source overrides of one activation and the recomputation of the days they reach – one transaction."""
        with closing(self._db()) as db, db:
            db.executemany("INSERT INTO point_overrides (metric_id, point, effective_from, created_at, revision, active) "
                           "VALUES (?, ?, ?, ?, ?, 1)", [(m, json.dumps(p), eff.isoformat(), created_at.isoformat(), revision)
                                                         for m, p, eff in overrides])
            self._bump(db)
            return db.execute("UPDATE days SET stale = 1 WHERE local_date >= ?", (stale_from.isoformat(),)).rowcount

    def add_override(self, metric_id: str, point: dict[str, Any], effective_from: datetime, created_at: datetime) -> None:
        with closing(self._db()) as db, db:
            db.execute("INSERT INTO point_overrides (metric_id, point, effective_from, created_at) VALUES (?, ?, ?, ?)",
                       (metric_id, json.dumps(point), effective_from.isoformat(), created_at.isoformat()))
            self._bump(db)

    def withdraw_overrides(self, revision: int, first_day: Callable[[datetime], date]) -> int:
        """The overrides a revision brought (restore of the plant before it); the days they reached are computed anew.
        ``first_day``: local day of an override's start (the archive keeps local dates)."""
        with closing(self._db()) as db, db:
            rows = db.execute("SELECT effective_from FROM point_overrides WHERE revision = ? AND active = 1",
                              (revision,)).fetchall()
            if not rows:
                return 0
            db.execute("UPDATE point_overrides SET active = 0 WHERE revision = ?", (revision,))
            self._bump(db)
            first = min(first_day(datetime.fromisoformat(r[0])) for r in rows)
            return db.execute("UPDATE days SET stale = 1 WHERE local_date >= ?", (first.isoformat(),)).rowcount

    def max_revision(self) -> int:
        with closing(self._db()) as db:
            row = db.execute("SELECT MAX(revision) FROM plant_revisions").fetchone()
        return int(row[0] or 0)

    def override_gen(self) -> int:
        with closing(self._db()) as db:
            return int(db.execute("SELECT value FROM meta WHERE key = 'override_gen'").fetchone()[0])

    @staticmethod
    def _bump(db: sqlite3.Connection) -> None:
        db.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'override_gen'")

    def mark_stale_from(self, first: date) -> int:
        """Every archived day from ``first`` on is computed anew on its next request (retroactive sensor change)."""
        with closing(self._db()) as db, db:
            return db.execute("UPDATE days SET stale = 1 WHERE local_date >= ?", (first.isoformat(),)).rowcount

    # --- days -----------------------------------------------------------------------------------------------------
    def get(self, d: date) -> ArchivedDay | None:
        with closing(self._db()) as db:
            row = db.execute("SELECT tier, final, durable, stale, calc_version, plant_revision, source_key, computed_at, "
                             "payload, tz FROM days WHERE local_date = ?", (d.isoformat(),)).fetchone()
        if row is None or row[3]:
            return None
        return ArchivedDay(payload=row[8], tz=row[9], tier=row[0], computed_at=datetime.fromisoformat(row[7]),
                           final=bool(row[1]), durable=bool(row[2]), calc_version=row[4], plant_revision=row[5],
                           source_key=row[6])

    def put(self, d: date, tz: str, day: PlantDay, computed_at: datetime, final: bool, plant: Plant,
            gen: int | None = None) -> ArchivedDay:
        """``gen``: the override generation the computation began with – if overrides changed meanwhile, the day is
        stored but stays due for recomputation."""
        durable = final and day.tier == TIER_RAW
        key = source_key(plant, day.tier)
        payload = _encode(day)
        with closing(self._db()) as db, db:
            stale = int(gen is not None and gen != int(db.execute(
                "SELECT value FROM meta WHERE key = 'override_gen'").fetchone()[0]))
            db.execute("INSERT OR REPLACE INTO days VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (d.isoformat(), tz, day.tier, int(final), int(durable), stale, CALCULATION_VERSION, plant.revision,
                        key, computed_at.isoformat(), payload))
        return ArchivedDay(payload, tz, day.tier, computed_at, final, durable, CALCULATION_VERSION, plant.revision, key)

    def mark_stale(self, days: list[date]) -> int:
        """Explicit recomputation (user decision 2026-11-15, 3): the next request computes these days anew – from the
        keep window if they still lie in it, else from hourly statistics."""
        with closing(self._db()) as db, db:
            cur = db.executemany("UPDATE days SET stale = 1 WHERE local_date = ?", [(d.isoformat(),) for d in days])
            return cur.rowcount

    def summary(self) -> dict[str, Any]:
        with closing(self._db()) as db:
            rows = db.execute("SELECT tier, durable, COUNT(*), MIN(local_date), MAX(local_date) FROM days "
                              "WHERE stale = 0 GROUP BY tier, durable").fetchall()
        return {f"{tier}{'_durable' if durable else ''}": {"days": n, "first": a, "last": b}
                for tier, durable, n, a, b in rows}
