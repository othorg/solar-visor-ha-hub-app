"""Coordinated shutdown (Codex P2 open point): SIGTERM → both listeners stop, journal records a clean stop."""

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sigterm_stops_cleanly_and_journals_it(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in ("ha_token", "influx_token"):
        (secrets / name).write_text("dummy\n")
    pub, adm = _free_port(), _free_port()
    env = {**os.environ, "SOLAR_VISOR_PUBLIC_BIND": f"127.0.0.1:{pub}", "SOLAR_VISOR_ADMIN_BIND": f"127.0.0.1:{adm}",
           "SOLAR_VISOR_DATA_DIR": str(tmp_path / "data"), "SOLAR_VISOR_SECRETS_DIR": str(secrets),
           "SOLAR_VISOR_HA_URL": "http://127.0.0.1:9"}
    proc = subprocess.Popen([sys.executable, "-m", "solar_visor_hub.server"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{pub}/health/live", timeout=1)
                break
            except OSError:
                assert time.monotonic() < deadline, "server did not start"
                time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    db = sqlite3.connect(tmp_path / "data" / "journal.sqlite3")
    assert [k for (k,) in db.execute("SELECT kind FROM hub_events ORDER BY id")] == ["start", "stop"]
    assert db.execute("SELECT value FROM meta WHERE key='clean_stop'").fetchone() == ("1",)
