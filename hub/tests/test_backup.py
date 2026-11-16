"""P8: consistent backup and verified restore of the hub state."""

import sqlite3
import tarfile
from datetime import UTC, datetime

import pytest

from solar_visor_hub import backup
from solar_visor_hub.devices import DeviceStore
from solar_visor_hub.journal import Journal


def _state(data):
    store = DeviceStore(data / "devices.sqlite3")
    token, _ = store.enroll(store.create_pairing_code(), "iPhone")
    j = Journal(data / "journal.sqlite3")
    j.record_start(datetime(2026, 10, 5, tzinfo=UTC))
    (data / "mapping_overrides.json").write_text('{"x": 1}')
    with sqlite3.connect(data / "energy.sqlite3") as db:
        db.execute("CREATE TABLE t (x)")
    return token, j


def test_backup_restore_roundtrip(tmp_path):
    data, out = tmp_path / "data", tmp_path / "bak"
    data.mkdir()
    token, j = _state(data)
    archive = backup.create(data, out)
    assert archive.stat().st_mode & 0o777 == 0o600
    m = backup.verify(archive)
    assert "energy.sqlite3" not in m["files"] and "devices.sqlite3" in m["files"]   # cache rebuildable, excluded
    with tarfile.open(archive) as tar:
        assert not any("secret" in n for n in tar.getnames())                    # secrets never included
    fresh = tmp_path / "empty"
    restored = backup.restore(archive, fresh)
    assert set(restored) == {"devices.sqlite3", "journal.sqlite3", "mapping_overrides.json"}
    assert DeviceStore(fresh / "devices.sqlite3").authenticate(token) is not None   # pairing survives a restore
    # running hub (journal not cleanly stopped) → refused without --force
    with pytest.raises(backup.BackupError):
        backup.restore(archive, data)
    j.record_stop(datetime(2026, 10, 5, 1, tzinfo=UTC))
    assert backup.restore(archive, data)


def test_rotation_and_damaged_archive(tmp_path):
    data, out = tmp_path / "data", tmp_path / "bak"
    data.mkdir()
    _state(data)
    for h in range(5):
        backup.create(data, out, keep=3, now=datetime(2026, 10, 5, h, tzinfo=UTC))
    assert len(list(out.glob("solar-visor-*.tar.gz"))) == 3
    assert backup.latest(out).name == "solar-visor-20261005-040000.tar.gz"
    bad = out / "solar-visor-bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        p = tmp_path / "devices.sqlite3"
        p.write_bytes(b"not a database")
        tar.add(p, arcname="devices.sqlite3")
    with pytest.raises(backup.BackupError):
        backup.verify(bad)
