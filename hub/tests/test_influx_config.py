"""InfluxDB connection in the web interface instead of the add-on options (user decision 2026-11-15)."""

import asyncio
import json

import httpx
import pytest

from solar_visor_hub.config import ConfigError, SecretFiles
from solar_visor_hub.influx import InfluxHistory
from solar_visor_hub.influx_config import (InfluxConfig, InfluxConfigStore, InfluxConnection, TokenRequired,
                                          migrate_from_options, validate)
from solar_visor_hub.settings import load_settings

DEFAULT = InfluxConfig("http://192.168.1.5:8086", "solar", "autogen")


def test_validation_accepts_plain_addresses_and_names():
    assert validate(InfluxConfig(" http://influx.lan:8086/ ", " ha ", " ")) == InfluxConfig("http://influx.lan:8086", "ha", None)
    assert validate(InfluxConfig("https://10.0.0.5", "home_assistant", "autogen")).retention_policy == "autogen"
    for bad in (InfluxConfig("influx.lan:8086", "db"), InfluxConfig("ftp://x", "db"),
                InfluxConfig("http://user:pw@x", "db"), InfluxConfig("http://x?db=y", "db"),
                InfluxConfig("http://x:99999", "db"), InfluxConfig("http://x", ""),
                InfluxConfig("http://x", "a b"), InfluxConfig("http://x", "db", 'x"; DROP')):
        with pytest.raises(ConfigError):
            validate(bad)


def test_store_round_trip_and_unreadable_file_is_ignored(tmp_path):
    store = InfluxConfigStore(tmp_path / "influx.json")
    assert store.current is None
    store.save(InfluxConfig("http://influx.lan:8086", "ha", None))
    assert store.current == InfluxConfig("http://influx.lan:8086", "ha", None)
    (tmp_path / "influx.json").write_text("{broken")
    assert store.current is None


def test_former_options_are_taken_over_once(tmp_path):
    store = InfluxConfigStore(tmp_path / "influx.json")
    assert not migrate_from_options(store, {}, DEFAULT)                          # nothing set: nothing written
    assert store.current is None
    assert migrate_from_options(store, {"influx_url": "http://influx.lan:8086", "influx_rp": "none"}, DEFAULT)
    assert store.current == InfluxConfig("http://influx.lan:8086", "solar", None)
    assert not migrate_from_options(store, {"influx_url": "http://other:8086"}, DEFAULT)   # never overwrites
    assert store.current.url == "http://influx.lan:8086"
    invalid = InfluxConfigStore(tmp_path / "other.json")
    assert not migrate_from_options(invalid, {"influx_url": "not a url"}, DEFAULT) and invalid.current is None


def test_addon_settings_prefer_the_web_interface(tmp_path):
    (tmp_path / "options.json").write_text(json.dumps({"influx_url": "http://old:8086", "influx_db": "old"}))
    env = {"SOLAR_VISOR_RUNTIME": "addon", "SOLAR_VISOR_DATA_DIR": str(tmp_path)}
    assert load_settings(env).influx_url == "http://old:8086"                   # until something was saved
    InfluxConfigStore(tmp_path / "influx.json").save(InfluxConfig("http://new:8086", "new", None))
    s = load_settings(env)
    assert (s.influx_url, s.influx_db, s.influx_rp) == ("http://new:8086", "new", None)


def connection(tmp_path, token="tok-a"):
    secrets = SecretFiles(ha_token=tmp_path / "ha", influx_token=tmp_path / "secrets" / "influx")
    (tmp_path / "secrets").mkdir(exist_ok=True)
    (tmp_path / "secrets" / "influx").write_text(token)
    return InfluxConnection(InfluxConfigStore(tmp_path / "influx.json"), DEFAULT, secrets.influx_token,
                            secrets.read_influx_token)


def test_a_token_is_never_paired_with_another_address(tmp_path):
    conn = connection(tmp_path)
    cfg, gen = conn.config()
    assert cfg == DEFAULT and conn.token(gen) == "tok-a"
    conn.update(InfluxConfig("http://b:8086", "db"), "tok-b")
    with pytest.raises(ConfigError):
        conn.token(gen)                                        # a client built for A gets no token any more
    cfg2, gen2 = conn.config()
    assert cfg2.url == "http://b:8086" and conn.token(gen2) == "tok-b"


def test_a_failed_address_write_restores_the_previous_token(tmp_path, monkeypatch):
    conn = connection(tmp_path)
    def fail(self, cfg):
        raise OSError("disk full")
    monkeypatch.setattr(InfluxConfigStore, "save", fail)
    with pytest.raises(OSError):
        conn.update(InfluxConfig("http://b:8086", "db"), "tok-b")
    cfg, gen = conn.config()
    assert cfg == DEFAULT and conn.token(gen) == "tok-a"        # A keeps its own token


def test_the_history_follows_the_saved_connection_at_once(tmp_path):
    conn = connection(tmp_path)
    seen = []

    def handler(request):
        host = request.url.host
        seen.append((host, request.headers.get("Authorization"), request.url.params.get("db")))
        rows = [] if host == "192.168.1.5" else [["sensor.x"]]              # A holds none of the wanted series
        return httpx.Response(200, json={"results": [{"series": [{"values": rows}]}]})

    history = InfluxHistory(DEFAULT.url, DEFAULT.database, DEFAULT.retention_policy, lambda: "unused", {"sensor.x"},
                            transport=httpx.MockTransport(handler), connection=conn)

    async def run():
        first = await history.present()
        conn.update(InfluxConfig("http://b:8086", "other"), "tok-b")
        return first, await history.present()

    first, second = asyncio.run(run())
    assert first == set() and second == {"sensor.x"}                          # the empty cache of A does not stick
    assert seen == [("192.168.1.5", "Token tok-a", "solar"), ("b", "Token tok-b", "other")]


def test_concurrent_saves_cannot_mix_address_and_token(tmp_path):
    conn = connection(tmp_path)
    conn.update(InfluxConfig("http://b:8086", "db"), "tok-b")
    with pytest.raises(TokenRequired):                                         # checked inside the lock
        conn.update(InfluxConfig("http://a:8086", "db"), None)
    cfg, gen = conn.config()
    assert cfg.url == "http://b:8086" and conn.token(gen) == "tok-b"


def test_an_undo_that_fails_closes_the_connection(tmp_path, monkeypatch):
    conn = connection(tmp_path)
    _, old_generation = conn.config()
    def fail_save(self, cfg):
        raise OSError("disk full")
    calls = {"n": 0}
    import solar_visor_hub.influx_config as ic
    real = ic.store_secret
    def flaky_secret(target, value, allow_spaces=False):
        calls["n"] += 1
        if calls["n"] > 1:                                                    # the restore of A's token fails
            raise OSError("disk full")
        real(target, value, allow_spaces)
    monkeypatch.setattr(InfluxConfigStore, "save", fail_save)
    monkeypatch.setattr(ic, "store_secret", flaky_secret)
    with pytest.raises(OSError):
        conn.update(InfluxConfig("http://b:8086", "db"), "tok-b")
    cfg, gen = conn.config()
    assert cfg == DEFAULT and not (tmp_path / "secrets" / "influx").exists()   # removed: durable, also after a restart
    for g in (old_generation, gen):
        with pytest.raises((ConfigError, OSError)):
            conn.token(g)                                                     # B's token never reaches A
    monkeypatch.undo()
    conn.update(InfluxConfig("http://192.168.1.5:8086", "solar"), None)    # a tokenless save brings nothing back
    with pytest.raises((ConfigError, OSError)):
        conn.token(conn.config()[1])
    fresh = InfluxConnection(InfluxConfigStore(tmp_path / "influx.json"), DEFAULT, tmp_path / "secrets" / "influx",
                             lambda: (tmp_path / "secrets" / "influx").read_text().strip())
    with pytest.raises(OSError):
        fresh.token(fresh.config()[1])                                        # after a restart: still no token
