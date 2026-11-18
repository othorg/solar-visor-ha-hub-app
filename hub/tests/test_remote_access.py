"""Remote access over Home Assistant (plan ha-remote-access P1): invitations, separate failure budgets, the bridge
marker and the address suggestion from Home Assistant."""

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from solar_visor_hub.app import _FailureLimiter, create_admin_app, create_public_app
from solar_visor_hub.bridge import HEADER, SEEN, VERSIONS_HEADER, ha_url
from solar_visor_hub.devices import DeviceStore, PairingError
from solar_visor_hub.ha_client import HaReadError
from solar_visor_hub.ui import Addresses, UiContext, UiSettings, create_ui_app, pairing_payload

W = {"X-Solar-Visor": "1"}


@pytest.fixture
def store(tmp_path):
    return DeviceStore(tmp_path / "devices.sqlite3")


@pytest.fixture(autouse=True)
def fresh_bridge():
    SEEN.reset()
    yield
    SEEN.reset()


# --- invitations ------------------------------------------------------------------------------------------------------

def test_an_invitation_pairs_and_uses_up_its_code(store, tmp_path):
    code, invite = store.create_pairing("admin")
    assert len(invite) == 32 and invite != code
    token, scope = store.enroll(None, "iPhone", invite=invite)
    assert scope == "admin" and store.authenticate(token, "admin")
    with pytest.raises(PairingError):
        store.enroll(code, "again")                                    # the same pairing, used up
    with pytest.raises(PairingError):
        store.enroll(None, "again", invite=invite)
    assert invite.encode() not in (tmp_path / "devices.sqlite3").read_bytes()


def test_a_code_uses_up_its_invitation_and_both_expire(store):
    code, invite = store.create_pairing()
    store.enroll(code, "Mac")
    with pytest.raises(PairingError):
        store.enroll(None, "late", invite=invite)
    old_code, old_invite = store.create_pairing(now=datetime.now(UTC) - timedelta(minutes=11))
    with pytest.raises(PairingError):
        store.enroll(None, "late", invite=old_invite)
    with pytest.raises(PairingError):
        store.enroll(old_code, "late")


def test_exactly_one_secret_is_required(store):
    code, invite = store.create_pairing()
    with pytest.raises(PairingError):
        store.enroll(None, "x")
    with pytest.raises(PairingError):
        store.enroll(code, "x", invite=invite)


def test_an_older_database_gains_the_invitation_column(tmp_path):
    path = tmp_path / "devices.sqlite3"
    with sqlite3.connect(path) as db:                      # the table as the hub created it before invitations
        db.execute("CREATE TABLE pairing_codes (code_hash TEXT PRIMARY KEY, scope TEXT NOT NULL, "
                   "expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO pairing_codes VALUES ('x', 'read', '2000-01-01T00:00:00+00:00', 1)")
    store = DeviceStore(path)
    code, invite = store.create_pairing()
    assert store.enroll(None, "n", invite=invite)[1] == "read"
    assert store.create_pairing_code() != code


# --- admin listener ---------------------------------------------------------------------------------------------------

def admin(store):
    return TestClient(create_admin_app(devices=store))


def test_enroll_takes_a_code_or_an_invitation(store):
    c = admin(store)
    code, invite = store.create_pairing()
    assert c.post("/admin/v1/devices/enroll", json={"invite": invite, "device_name": "A"}).status_code == 200
    code2, invite2 = store.create_pairing()
    assert c.post("/admin/v1/devices/enroll", json={"code": code2, "invite": invite2, "device_name": "B"}).status_code == 422
    assert c.post("/admin/v1/devices/enroll", json={"device_name": "B"}).status_code == 422
    assert c.post("/admin/v1/devices/enroll", json={"invite": "short", "device_name": "B"}).status_code == 422
    r = c.post("/admin/v1/devices/enroll", json={"invite": "x" * 32, "device_name": "B"})
    assert r.status_code == 403 and "x" * 32 not in r.text


def test_a_flood_of_wrong_codes_does_not_block_invitations_or_the_watch(store):
    c = admin(store)
    for _ in range(10):
        assert c.post("/admin/v1/devices/enroll", json={"code": "00000000", "device_name": "x"}).status_code == 403
    code, invite = store.create_pairing("read")
    assert c.post("/admin/v1/devices/enroll", json={"code": code, "device_name": "x"}).status_code == 429
    r = c.post("/admin/v1/devices/enroll", json={"invite": invite, "device_name": "phone"})
    assert r.status_code == 200
    phone = r.json()["token"]
    watch = c.post("/admin/v1/devices/delegate", json={"device_name": "Watch"}, headers={"Authorization": f"Bearer {phone}"})
    assert watch.status_code == 200


def test_failed_delegations_do_not_block_pairing(store):
    c = admin(store)
    for _ in range(10):
        r = c.post("/admin/v1/devices/delegate", json={"device_name": "W"}, headers={"Authorization": "Bearer nope"})
        assert r.status_code == 403
    assert c.post("/admin/v1/devices/delegate", json={"device_name": "W"}, headers={"Authorization": "Bearer nope"}).status_code == 429
    code = store.create_pairing_code()
    assert c.post("/admin/v1/devices/enroll", json={"code": code, "device_name": "x"}).status_code == 200


def test_successful_attempts_do_not_count_and_concurrent_ones_stay_bounded():
    limiter = _FailureLimiter(limit=3, window_s=600)
    for _ in range(5):                                      # successes are released again
        slot = limiter.reserve()
        assert slot is not None
        limiter.release(slot)
    taken, lock = [], threading.Lock()

    def attempt():
        slot = limiter.reserve()
        with lock:
            taken.append(slot)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(s is not None for s in taken) == 3           # concurrent failures never exceed the limit


# --- bridge marker --------------------------------------------------------------------------------------------------

def test_health_names_the_bridge_versions_and_forwarded_requests_are_noticed(store, tmp_path):
    public = TestClient(create_public_app(devices=store))   # no mapping: public repo too
    r = public.get("/health/ready")
    assert r.headers[VERSIONS_HEADER] == "1" and not SEEN.recently()
    public.get("/health/ready", headers={HEADER: "1"})
    assert SEEN.recently()
    SEEN.reset()
    admin(store).get("/admin/health", headers={HEADER: "1"})
    assert SEEN.recently()


def test_the_bridge_marker_grants_nothing(store):
    public = TestClient(create_public_app(devices=store))   # no mapping: public repo too
    assert public.get("/v2/capabilities", headers={HEADER: "1"}).status_code == 401


# --- address over Home Assistant ------------------------------------------------------------------------------------

def test_ha_url_prefers_the_cloud_then_the_external_address_never_the_internal_one():
    assert ha_url({"internal": "https://ha.lan:8123", "external": "https://ha.example.org:8717/", "cloud": None}) \
        == "https://ha.example.org:8717/api/solar_visor"
    assert ha_url({"external": "https://ha.example.org", "cloud": "https://abc.ui.nabu.casa"}) \
        == "https://abc.ui.nabu.casa/api/solar_visor"
    assert ha_url({"internal": "https://ha.lan:8123", "external": None, "cloud": None}) is None
    assert ha_url({"external_url": "https://h.example.org"}) == "https://h.example.org/api/solar_visor"   # get_config
    assert ha_url({"external": "ftp://x"}) is None and ha_url(None) is None


class FakeHa:
    def __init__(self, network=None, config=None, fail_network=False):
        self.network, self.config, self.fail_network, self.calls = network, config, fail_network, []

    async def read(self, message):
        self.calls.append(message["type"])
        if message["type"] == "network/url":
            if self.fail_network:
                raise HaReadError("unknown_command", "no")
            return self.network
        if message["type"] == "get_config":
            return self.config
        raise AssertionError(message)


def ui(tmp_path, ha=None, devices=None):
    ctx = UiContext(mode="plant", running_revision=1, stored_revision=lambda: 1, settings=UiSettings(tmp_path / "ui.json"),
                    web_dir=tmp_path / "noweb", read=ha.read if ha else None, devices=devices)
    return TestClient(create_ui_app(ctx))


def test_the_address_commands_pass_the_read_only_guard():
    from solar_visor_hub.ops_allowlist import check_ha_ws
    check_ha_ws("network/url")                     # Codex review P1: blocked by the guard before
    check_ha_ws("get_config")


def test_a_command_blocked_by_the_guard_falls_back_to_get_config(tmp_path):
    from solar_visor_hub.ops_allowlist import ForbiddenOperation

    class Guarded(FakeHa):
        async def read(self, message):
            if message["type"] == "network/url":
                self.calls.append(message["type"])
                raise ForbiddenOperation("not allowed")          # a PermissionError, i.e. an OSError
            return await super().read(message)

    ha = Guarded(config={"external_url": "https://h.example.org"})
    assert ui(tmp_path, ha).get("/api/addresses").json()["suggested_ha_url"] == "https://h.example.org/api/solar_visor"


def test_the_interface_suggests_the_address_over_home_assistant(tmp_path):
    ha = FakeHa(network={"internal": "https://ha.lan:8123", "external": "https://ha.example.org:8717", "cloud": None})
    v = ui(tmp_path, ha).get("/api/addresses").json()
    assert v["suggested_ha_url"] == "https://ha.example.org:8717/api/solar_visor" and v["bridge_seen"] is False
    fallback = FakeHa(config={"external_url": "https://h.example.org"}, fail_network=True)
    assert ui(tmp_path, fallback).get("/api/addresses").json()["suggested_ha_url"] == "https://h.example.org/api/solar_visor"
    assert fallback.calls == ["network/url", "get_config"]
    assert ui(tmp_path).get("/api/addresses").json()["suggested_ha_url"] is None     # no Home Assistant session
    SEEN.mark()
    assert ui(tmp_path, ha).get("/api/addresses").json()["bridge_seen"] is True


def test_the_pairing_text_carries_the_invitation_and_it_pairs(tmp_path, store):
    c = ui(tmp_path, devices=store)
    base = "https://ha.example.org/api/solar_visor"
    assert c.put("/api/addresses", json={"read_url": base, "admin_url": base}, headers=W).status_code == 200
    p = c.post("/api/pairing", json={"scope": "admin"}, headers=W).json()
    q = parse_qs(urlsplit(p["payload"]).query)
    assert q["read"] == [base] and q["admin"] == [base] and q["code"] == [p["code"]] and len(q["invite"][0]) == 32
    token, scope = store.enroll(None, "Pixel", invite=q["invite"][0])
    assert scope == "admin" and token


def test_pairing_payload_without_invitation_stays_as_before():
    a = Addresses(read_url="https://r.example.org", admin_url="http://192.168.1.2:8081")
    assert "invite" not in pairing_payload(a, "12345678")
    assert pairing_payload(a, "12345678", "I" * 32).endswith("&invite=" + "I" * 32)
