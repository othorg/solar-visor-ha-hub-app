"""Availability journal (plan §3.3 step 7, P2 WP3).

Durable record of *when* data was trustworthy, so later history (P3) can tell "unchanged" from
"outage": every quality transition per metric, hub lifecycle events, and a heartbeat that bounds
the gap after an unclean shutdown (``restart_gap`` from the last heartbeat to the next start).

The evaluator (``QualityMonitor``) is the single place that turns the live model into quality
states for the journal; it runs on every model change (checked each second) and every 5 s for
time-based expiry (freshness, trust windows).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .live import LiveModel
from .models import MetricPoint

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
HEARTBEAT_S = 60


Interval = tuple[datetime, datetime]


def read_monitoring_gaps(db: sqlite3.Connection, start: datetime, end: datetime) -> list[Interval]:
    """Intervals in [start, end) in which the hub did not monitor: clean stop → next start, unclean shutdown:
    last heartbeat → next start (``restart_gap``). No journal row there does not mean "ok" (Codex P5 finding 5).
    Use ``start=EPOCH`` to get every gap that can end a state carried in from before a request."""
    gaps: list[Interval] = []
    stopped: datetime | None = None
    for at, kind, detail in db.execute("SELECT at_utc, kind, detail FROM hub_events WHERE kind IN "
                                       "('stop', 'start', 'restart_gap') ORDER BY id"):
        t = datetime.fromisoformat(at)
        if kind == "stop":
            stopped = t
        elif kind == "start":
            if stopped is not None:
                gaps.append((stopped, t))
            stopped = None
        else:
            try:
                d = json.loads(detail or "{}")
                a, b = datetime.fromisoformat(d["from"]), datetime.fromisoformat(d["to"])
            except (ValueError, KeyError, TypeError):
                continue
            gaps.append((a, b))
            stopped = None
    return [(max(a, start), min(b, end)) for a, b in gaps if min(b, end) > max(a, start)]


EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def end_at_gaps(iv: list[Interval], gaps: list[Interval]) -> list[Interval]:
    """A journaled state ends where monitoring stopped: it neither spans a gap nor resumes after it – after a
    restart only a new row is evidence (Codex P5 WP1b finding 4). Intervals carry their real start (row time)."""
    out: list[Interval] = []
    for a, b in iv:
        stop = min((g0 for g0, g1 in gaps if g1 > a and g0 < b), default=b)
        if stop > a:
            out.append((a, max(a, stop)))
    return out


def clip(iv: list[Interval], start: datetime, end: datetime) -> list[Interval]:
    return [(max(a, start), min(b, end)) for a, b in iv if min(b, end) > max(a, start)]


@dataclass(frozen=True)
class JournalEntry:
    at: datetime
    kind: str                 # "quality" or a hub event kind
    subject: str              # metric id or "hub"
    state: str | None
    detail: str | None


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metric_quality (
                    id INTEGER PRIMARY KEY, at_utc TEXT NOT NULL, metric_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    entity TEXT, state TEXT NOT NULL, reasons TEXT NOT NULL, mapping_revision INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS ix_quality_metric_time ON metric_quality (metric_id, at_utc);
                CREATE TABLE IF NOT EXISTS hub_events (
                    id INTEGER PRIMARY KEY, at_utc TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        self._last: dict[str, tuple[str, str]] = self._load_last_states()

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def _meta(self, db: sqlite3.Connection, key: str) -> str | None:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _load_last_states(self) -> dict[str, tuple[str, str]]:
        with closing(self._db()) as db:
            rows = db.execute("""SELECT metric_id, state, reasons FROM metric_quality q
                                 WHERE id = (SELECT MAX(id) FROM metric_quality WHERE metric_id = q.metric_id)""")
            return {m: (s, r) for m, s, r in rows}

    # --- lifecycle --------------------------------------------------------------------------
    def record_start(self, now: datetime, detail: str | None = None) -> None:
        with closing(self._db()) as db, db:
            if self._meta(db, "clean_stop") == "0":
                last_alive = self._meta(db, "last_alive")
                db.execute("INSERT INTO hub_events (at_utc, kind, detail) VALUES (?, 'restart_gap', ?)",
                           (now.isoformat(), json.dumps({"from": last_alive, "to": now.isoformat()})))
            db.execute("INSERT INTO hub_events (at_utc, kind, detail) VALUES (?, 'start', ?)", (now.isoformat(), detail))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('clean_stop', '0')")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('last_alive', ?)", (now.isoformat(),))
        # after a restart nothing is known yet – the next evaluation records the current states again
        self._last.clear()

    def heartbeat(self, now: datetime) -> None:
        with closing(self._db()) as db, db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('last_alive', ?)", (now.isoformat(),))

    def record_stop(self, now: datetime) -> None:
        with closing(self._db()) as db, db:
            db.execute("INSERT INTO hub_events (at_utc, kind, detail) VALUES (?, 'stop', NULL)", (now.isoformat(),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('clean_stop', '1')")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('last_alive', ?)", (now.isoformat(),))

    def record_event(self, kind: str, now: datetime, detail: str | None = None) -> None:
        with closing(self._db()) as db, db:
            db.execute("INSERT INTO hub_events (at_utc, kind, detail) VALUES (?, ?, ?)", (now.isoformat(), kind, detail))

    # --- quality ----------------------------------------------------------------------------
    def record_quality(self, points: dict[str, MetricPoint], now: datetime) -> int:
        """Writes one row per metric whose state or reasons changed; returns the number of rows.
        The in-memory 'last written' state advances only after the commit, so a failed write is retried
        on the next evaluation instead of being lost."""
        rows, pending = [], {}
        for mid, p in points.items():
            reasons = ",".join(r for r in p.reasons if r != "awaiting_confirmation")  # transient, not an outage
            current = (p.state.value, reasons)
            if self._last.get(mid) != current:
                pending[mid] = current
                rows.append((now.isoformat(), mid, p.source_id, p.source_entity, p.state.value, reasons,
                             p.mapping_revision))
        if rows:
            with closing(self._db()) as db, db:
                db.executemany("""INSERT INTO metric_quality (at_utc, metric_id, source_id, entity, state, reasons,
                                  mapping_revision) VALUES (?, ?, ?, ?, ?, ?, ?)""", rows)
            self._last.update(pending)
        return len(rows)

    def first_record(self) -> datetime | None:
        with closing(self._db()) as db:
            row = db.execute("SELECT MIN(at_utc) FROM metric_quality").fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def monitoring_gaps(self, start: datetime, end: datetime) -> list[Interval]:
        with closing(self._db()) as db:
            return read_monitoring_gaps(db, start, end)

    def intervals(self, metric_id: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime, str, str]]:
        """Intervals in [start, end) in which the metric was not ok while the hub was monitoring:
        (start, end, state, reasons). A state ends at the next monitoring gap and does not resume after it."""
        with closing(self._db()) as db:
            gaps = read_monitoring_gaps(db, EPOCH, end)
            prev = db.execute("SELECT at_utc, state, reasons FROM metric_quality WHERE metric_id=? AND at_utc < ? "
                              "ORDER BY id DESC LIMIT 1", (metric_id, start.isoformat())).fetchone()
            rows = db.execute("SELECT at_utc, state, reasons FROM metric_quality WHERE metric_id=? AND at_utc >= ? "
                              "AND at_utc < ? ORDER BY id", (metric_id, start.isoformat(), end.isoformat())).fetchall()
        out: list[tuple[datetime, datetime, str, str]] = []
        cur = (datetime.fromisoformat(prev[0]), prev[1], prev[2]) if prev else None   # real start of the carry-in
        for at, state, reasons in rows:
            t = datetime.fromisoformat(at)
            if cur and cur[1] != "ok" and t > cur[0]:
                out.append((cur[0], t, cur[1], cur[2]))
            cur = (t, state, reasons)
        if cur and cur[1] != "ok" and end > cur[0]:
            out.append((cur[0], end, cur[1], cur[2]))
        return [(a, b, st, rs) for a0, b0, st, rs in out for a, b in clip(end_at_gaps([(a0, b0)], gaps), start, end)]

    def recent(self, limit: int = 20, events: int | None = None) -> list[JournalEntry]:
        """Newest entries first. With ``events``, hub events get their own quota so a burst of quality
        transitions (reconnect, restart) cannot push start/stop/connectivity out of view."""
        with closing(self._db()) as db:
            q = db.execute("""SELECT at_utc, 'quality', metric_id, state, reasons FROM metric_quality
                              ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
            e = db.execute("SELECT at_utc, kind, 'hub', NULL, detail FROM hub_events ORDER BY id DESC LIMIT ?",
                           (events if events is not None else limit,)).fetchall()
        rows = sorted(q + e, key=lambda r: r[0], reverse=True)
        if events is None:
            rows = rows[:limit]
        return [JournalEntry(datetime.fromisoformat(a), k, s, st, d or None) for a, k, s, st, d in rows]

class QualityMonitor:
    """Evaluates the live model on change (≤ 1 s) and every 5 s; journals transitions and HA connectivity."""

    def __init__(self, live: Any, journal: Journal, now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 points: Callable[[datetime], dict[str, Any]] | None = None):
        """``live``: the evaluator that owns the journal – exactly one per hub (the fixed model, or ``PlantLive``
        with ``points=plant_live.quality_points`` for a plant without the legacy profile)."""
        self.live, self.journal, self._now = live, journal, now
        self._points = points or (lambda at: live.snapshot(at).metrics)
        self._rev = -1
        self._connected: bool | None = None
        self._last_eval: datetime | None = None
        self._last_beat: datetime | None = None
        # entity switched known ↔ unknown: evaluate at once, so blips shorter than one tick are journaled
        live.on_quality_edge = self._on_edge

    def _on_edge(self, at: datetime) -> None:
        try:
            self._evaluate(at)
        except (sqlite3.Error, OSError):
            log.exception("journal write failed – continuing")   # never break HA event processing

    def _evaluate(self, now: datetime) -> None:
        self._rev, self._last_eval = self.live.revision, now
        self.journal.record_quality(self._points(now), now)

    def tick(self) -> None:
        now = self._now()
        if self.live.connected != self._connected:
            first = self._connected is None
            if not (first and not self.live.connected):   # "not yet connected" right after start is no outage
                self.journal.record_event("ha_connected" if self.live.connected else "ha_disconnected", now)
            self._connected = self.live.connected          # only after the event was written
        due = self._last_eval is None or (now - self._last_eval).total_seconds() >= 5
        if self.live.revision != self._rev or due:
            self._evaluate(now)
        if self._last_beat is None or (now - self._last_beat).total_seconds() >= HEARTBEAT_S:
            self.journal.heartbeat(now)
            self._last_beat = now

    async def run_forever(self) -> None:
        while True:
            try:
                self.tick()
            except (sqlite3.Error, OSError):
                log.exception("journal write failed – continuing")   # never take the live API down
            await asyncio.sleep(1)

