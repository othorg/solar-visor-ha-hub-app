"""Device tokens and one-time pairing codes (plan §4.1).

- tokens: 256-bit random, only SHA-256 hashes stored, scope ``read`` or ``admin``, revocable
- pairing codes: short-lived (10 min), single use, hashed; created via CLI on the hub host
- delegated tokens (P6): a paired device derives one read token per name for a companion (the watch); revoking
  the parent revokes its children, a child cannot delegate further
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCOPES = ("read", "admin")
PAIRING_TTL = timedelta(minutes=10)
MAX_CHILDREN = 3                     # companions per paired device (watch + spare); bounded rows, no growth


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Device:
    device_id: int
    name: str
    scope: str
    created_at: str
    last_used_at: str | None
    revoked: bool


class PairingError(PermissionError):
    pass


class DeviceStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, scope TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                    last_used_at TEXT, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    code_hash TEXT PRIMARY KEY, scope TEXT NOT NULL, expires_at TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0);
                """
            )
            columns = {r[1] for r in db.execute("PRAGMA table_info(devices)")}
            if "parent_id" not in columns:                       # migration P6
                db.execute("ALTER TABLE devices ADD COLUMN parent_id INTEGER REFERENCES devices(id)")
            # one row per (parent, companion name): early P6 builds inserted a new row per delegation – keep the newest
            db.execute("""DELETE FROM devices WHERE parent_id IS NOT NULL AND id NOT IN
                          (SELECT MAX(id) FROM devices WHERE parent_id IS NOT NULL GROUP BY parent_id, name)""")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_devices_child ON devices (parent_id, name) "
                       "WHERE parent_id IS NOT NULL")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, timeout=5, isolation_level="IMMEDIATE")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def create_pairing_code(self, scope: str = "read", now: datetime | None = None) -> str:
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        now = now or datetime.now(UTC)
        code = f"{secrets.randbelow(10**8):08d}"
        with closing(self._db()) as db, db:
            db.execute("INSERT INTO pairing_codes VALUES (?,?,?,0)", (_hash(code), scope, (now + PAIRING_TTL).isoformat()))
        return code

    def enroll(self, code: str, name: str, now: datetime | None = None) -> tuple[str, str]:
        """Consume a pairing code and return ``(token, scope)``. The token is shown exactly once."""
        now = now or datetime.now(UTC)
        name = (name or "Gerät").strip()[:64]
        with closing(self._db()) as db, db:
            row = db.execute("SELECT scope, expires_at, used FROM pairing_codes WHERE code_hash=?", (_hash(code.strip()),)).fetchone()
            if not row or row[2] or datetime.fromisoformat(row[1]) < now:
                raise PairingError("invalid or expired pairing code")
            scope = row[0]
            consumed = db.execute("UPDATE pairing_codes SET used=1 WHERE code_hash=? AND used=0", (_hash(code.strip()),))
            if consumed.rowcount != 1:  # concurrent enroll already consumed this code
                raise PairingError("invalid or expired pairing code")
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO devices (name, scope, token_hash, created_at) VALUES (?,?,?,?)",
                       (name, scope, _hash(token), now.isoformat()))
        return token, scope

    def authenticate(self, token: str | None, required_scope: str = "read", now: datetime | None = None) -> Device | None:
        if not token:
            return None
        now = now or datetime.now(UTC)
        with closing(self._db()) as db, db:
            # a delegated token is valid only while its parent is
            row = db.execute("""SELECT d.id, d.name, d.scope, d.created_at, d.last_used_at,
                                       d.revoked OR COALESCE(p.revoked, 0)
                                FROM devices d LEFT JOIN devices p ON p.id = d.parent_id WHERE d.token_hash=?""",
                             (_hash(token),)).fetchone()
            if not row or row[5]:
                return None
            if required_scope == "admin" and row[2] != "admin":
                return None
            last = row[4]
            if last is None or now - datetime.fromisoformat(last) > timedelta(minutes=5):  # throttle writes
                db.execute("UPDATE devices SET last_used_at=? WHERE id=?", (now.isoformat(), row[0]))
            return Device(row[0], row[1], row[2], row[3], last, bool(row[5]))

    def list_devices(self) -> list[Device]:
        with closing(self._db()) as db:
            rows = db.execute("SELECT id, name, scope, created_at, last_used_at, revoked FROM devices ORDER BY id").fetchall()
        return [Device(r[0], r[1], r[2], r[3], r[4], bool(r[5])) for r in rows]

    def revoke(self, device_id: int) -> bool:
        """Revokes the device and every token delegated from it."""
        with closing(self._db()) as db, db:
            hit = db.execute("UPDATE devices SET revoked=1 WHERE id=?", (device_id,)).rowcount == 1
            db.execute("UPDATE devices SET revoked=1 WHERE parent_id=?", (device_id,))
            return hit

    def delegate(self, parent_token: str | None, name: str, now: datetime | None = None) -> str:
        """A paired (non-delegated) device derives a read token for a companion such as its watch. Repeating the
        call for the same name replaces the previous child token. Returns the new token (shown once)."""
        now = now or datetime.now(UTC)
        parent = self.authenticate(parent_token, now=now)
        if parent is None:
            raise PairingError("invalid token")
        name = (name or "Begleitgerät").strip()[:64]
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")        # write lock before the checks: concurrent delegations serialize
            if db.execute("SELECT parent_id FROM devices WHERE id=?", (parent.device_id,)).fetchone()[0] is not None:
                raise PairingError("delegated tokens cannot delegate")
            token = secrets.token_urlsafe(32)
            # same name: the row is re-keyed in place (old token invalid, no row growth)
            existing = db.execute("SELECT id FROM devices WHERE parent_id=? AND name=? ORDER BY id DESC LIMIT 1",
                                  (parent.device_id, name)).fetchone()
            if existing:
                db.execute("UPDATE devices SET token_hash=?, created_at=?, last_used_at=NULL, revoked=0 WHERE id=?",
                           (_hash(token), now.isoformat(), existing[0]))
                return token
            if db.execute("SELECT COUNT(*) FROM devices WHERE parent_id=?", (parent.device_id,)).fetchone()[0] >= MAX_CHILDREN:
                raise PairingError("too many companions")
            db.execute("INSERT INTO devices (name, scope, token_hash, created_at, parent_id) VALUES (?,?,?,?,?)",
                       (name, "read", _hash(token), now.isoformat(), parent.device_id))
        return token
