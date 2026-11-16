"""Backup and restore of the hub's state (plan §8, P8).

- consistent: SQLite online-backup API (safe while the hub runs), each copy checked with ``PRAGMA integrity_check``
- contents: devices (token hashes), journal, K2 observations, mapping overrides, manifest; the energy cache is
  rebuildable and excluded unless asked for; secrets are never included (backed up separately)
- archives are 0600, rotated (``keep``); restore verifies the archive before replacing anything and replaces each
  file atomically; it refuses to run while the hub writes (lock check via the journal database)
"""

from __future__ import annotations

import json
import os
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .epochs import CALCULATION_VERSION

DATABASES = ("devices.sqlite3", "journal.sqlite3", "observations.sqlite3", "archive.sqlite3")   # archive: data, not cache
CACHE = "energy.sqlite3"
FILES = ("mapping_overrides.json", "plant.json")
PREFIX = "solar-visor-"


class BackupError(RuntimeError):
    pass


def _copy_db(src: Path, dst: Path) -> None:
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    with sqlite3.connect(dst) as d:
        if d.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError(f"integrity check failed for {src.name}")


def create(data_dir: Path, target_dir: Path, keep: int = 14, with_cache: bool = False, now: datetime | None = None) -> Path:
    now = now or datetime.now(UTC)
    target_dir.mkdir(parents=True, exist_ok=True)
    names = [*DATABASES, *([CACHE] if with_cache else [])]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        included = []
        for name in names:
            if (data_dir / name).exists():
                _copy_db(data_dir / name, work / name)
                included.append(name)
        for name in FILES:
            if (data_dir / name).exists():
                (work / name).write_bytes((data_dir / name).read_bytes())
                included.append(name)
        manifest = {"created_at": now.isoformat(), "hub_version": __version__,
                    "calculation_version": CALCULATION_VERSION, "files": included}
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))
        archive = target_dir / f"{PREFIX}{now.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            for name in ["manifest.json", *included]:
                tar.add(work / name, arcname=name)
    for old in sorted(target_dir.glob(f"{PREFIX}*.tar.gz"))[:-keep]:
        old.unlink()
    return archive


def _members(tar: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    out = {}
    for m in tar.getmembers():
        if not m.isfile() or "/" in m.name or m.name.startswith("."):
            raise BackupError(f"unexpected archive member: {m.name}")
        out[m.name] = m
    return out


def verify(archive: Path) -> dict:
    """Manifest of a valid archive (every listed file present, every database passes its integrity check)."""
    with tarfile.open(archive, "r:gz") as tar:
        members = _members(tar)
        if "manifest.json" not in members:
            raise BackupError("manifest missing")
        manifest = json.loads(tar.extractfile(members["manifest.json"]).read())   # type: ignore[union-attr]
        with tempfile.TemporaryDirectory() as tmp:
            for name in manifest["files"]:
                if name not in members:
                    raise BackupError(f"{name} missing")
                if name.endswith(".sqlite3"):
                    path = Path(tmp) / name
                    path.write_bytes(tar.extractfile(members[name]).read())   # type: ignore[union-attr]
                    with sqlite3.connect(path) as db:
                        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise BackupError(f"{name} damaged")
    return manifest


def _hub_running(data_dir: Path) -> bool:
    journal = data_dir / "journal.sqlite3"
    if not journal.exists():
        return False
    try:
        with sqlite3.connect(journal, timeout=0.2) as db:
            row = db.execute("SELECT value FROM meta WHERE key='clean_stop'").fetchone()
        return bool(row) and row[0] == "0"          # started and not cleanly stopped
    except sqlite3.Error:
        return True


def restore(archive: Path, data_dir: Path, force: bool = False) -> list[str]:
    manifest = verify(archive)
    if _hub_running(data_dir) and not force:
        raise BackupError("hub appears to be running (stop the service first, or --force after a crash)")
    data_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = _members(tar)
        for name in manifest["files"]:
            data = tar.extractfile(members[name]).read()   # type: ignore[union-attr]
            tmp = data_dir / f".{name}.restore"
            tmp.write_bytes(data)
            for suffix in ("-wal", "-shm"):               # stale WAL of the replaced database must not be replayed
                (data_dir / f"{name}{suffix}").unlink(missing_ok=True)
            os.replace(tmp, data_dir / name)
    # a configuration file that the backup does not carry did not exist then: restoring means going back to that
    # state (e.g. before a plant was set up) – it is set aside, not deleted
    for name in FILES:
        current = data_dir / name
        if name not in manifest["files"] and current.exists():
            os.replace(current, data_dir / f"{name}.before-restore")
    return list(manifest["files"])


def latest(target_dir: Path) -> Path | None:
    archives = sorted(target_dir.glob(f"{PREFIX}*.tar.gz"))
    return archives[-1] if archives else None
