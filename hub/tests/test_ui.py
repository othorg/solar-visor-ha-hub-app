"""Web interface behind Home Assistant Ingress (P4 step 1): only the Ingress proxy, only HA administrators, writes only
with the CSRF header; the development listener only on loopback and never in the add-on."""

import json

import pytest
from fastapi.testclient import TestClient

from solar_visor_hub.ui import (
    INGRESS_PEER,
    DevGuard,
    IngressGuard,
    UiBindError,
    UiContext,
    UiSettings,
    UserList,
    create_ui_app,
    load_ui_bind,
)

ADMIN, USER = "a1", "u1"
USERS = [{"id": ADMIN, "name": "Ada", "is_active": True, "group_ids": ["system-admin"]},
         {"id": USER, "name": "Bob", "is_active": True, "group_ids": ["system-users"]},
         {"id": "gone", "is_active": False, "group_ids": ["system-admin"]},
         {"id": "own", "is_active": True, "is_owner": True, "group_ids": []},
         {"id": "sys", "is_active": True, "system_generated": True, "group_ids": ["system-admin"]}]


class Peer:
    """Sets the peer address the listener sees (the test client has none of its own)."""

    def __init__(self, app, host):
        self.app, self.host = app, host

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (self.host, 41000)}
        await self.app(scope, receive, send)


class Recorder:
    def __init__(self, users=USERS, fail=False):
        self.users, self.fail, self.calls = users, fail, 0

    async def read(self, message):
        assert message == {"type": "config/auth/list"}
        self.calls += 1
        if self.fail:
            raise ConnectionError("no session")
        return self.users


def ui(tmp_path, peer=INGRESS_PEER, ha=None, web_dir=None, dev=False):
    ctx = UiContext(mode="plant", running_revision=3, stored_revision=lambda: 4,
                    settings=UiSettings(tmp_path / "ui.json"), web_dir=web_dir or tmp_path / "noweb")
    app = create_ui_app(ctx)
    guarded = DevGuard(app) if dev else IngressGuard(app, UserList((ha or Recorder()).read))
    return TestClient(Peer(guarded, peer))


def h(user=ADMIN, write=False, **extra):
    out = {"X-Remote-User-Id": user, "X-Remote-User-Display-Name": "Ada Lovelace", **extra}
    if write:
        out |= {"X-Solar-Visor": "1", "Content-Type": "application/json"}
    return out


# --- listener addresses ---------------------------------------------------------------------------------------------

def test_the_interface_listens_in_the_add_on_and_only_for_development_on_loopback():
    b = load_ui_bind(True, {})
    assert (b.host, b.port, b.ingress) == ("0.0.0.0", 8099, True)
    with pytest.raises(UiBindError):
        load_ui_bind(True, {"SOLAR_VISOR_UI_DEV_BIND": "127.0.0.1:8099"})
    assert load_ui_bind(False, {}) is None
    dev = load_ui_bind(False, {"SOLAR_VISOR_UI_DEV_BIND": "127.0.0.1:8099"})
    assert (dev.host, dev.ingress) == ("127.0.0.1", False)
    for bad in ("0.0.0.0:8099", "192.168.1.13:8099", "localhost:8099", "127.0.0.1:x"):
        with pytest.raises(UiBindError):
            load_ui_bind(False, {"SOLAR_VISOR_UI_DEV_BIND": bad})


# --- who gets in ----------------------------------------------------------------------------------------------------

def test_only_the_ingress_proxy_and_only_administrators(tmp_path):
    assert ui(tmp_path, peer="172.30.32.5").get("/api/session", headers=h()).status_code == 403   # other add-on
    assert ui(tmp_path, peer="192.168.1.50").get("/api/session", headers=h()).status_code == 403  # home network
    c = ui(tmp_path)
    assert c.get("/api/session").status_code == 401                                             # no HA user
    assert c.get("/api/session", headers=h(USER)).status_code == 403                           # not an admin
    assert c.get("/api/session", headers=h("gone")).status_code == 403                         # deactivated
    assert c.get("/api/session", headers=h("own")).status_code == 200                          # owner
    assert c.get("/api/session", headers=h("sys")).status_code == 403                          # Supervisor, add-ons
    r = c.get("/api/session", headers=h())
    assert r.status_code == 200 and r.json() == {"user": "Ada Lovelace", "mode": "plant", "version": r.json()["version"],
                                                 "running_revision": 3, "stored_revision": 4, "startup_error": None}
    assert r.headers["cache-control"] == "no-store" and "frame-ancestors 'self'" in r.headers["content-security-policy"]


def test_without_the_administrator_list_nobody_gets_in(tmp_path):
    assert ui(tmp_path, ha=Recorder(fail=True)).get("/api/session", headers=h()).status_code == 503


async def test_the_administrator_list_is_read_once_a_minute():
    now = [0.0]
    rec = Recorder()
    admins = UserList(rec.read, now=lambda: now[0])
    assert await admins.is_admin(ADMIN) and not await admins.is_admin(USER)
    assert rec.calls == 1
    now[0] = 61.0
    rec.fail = True
    assert await admins.is_admin(ADMIN) is True and rec.calls == 2        # a failed refresh keeps the last list …
    now[0] = 700.0
    assert await admins.is_admin(ADMIN) is None                            # … for a few minutes only


def test_writes_need_the_csrf_header_and_json(tmp_path):
    c = ui(tmp_path)
    body = json.dumps({"read_url": "https://solar.example.org/", "admin_url": None})
    assert c.put("/api/addresses", content=body, headers=h(**{"Content-Type": "application/json"})).status_code == 403
    assert c.put("/api/addresses", content=body,
                 headers=h(**{"X-Solar-Visor": "1", "Content-Type": "text/plain"})).status_code == 415
    r = c.put("/api/addresses", content=body, headers=h(write=True, **{"X-Forwarded-Host": "homeassistant.local:8123"}))
    assert r.status_code == 200
    assert r.json() == {"read_url": "https://solar.example.org", "admin_url": None,
                        "suggested_read_url": "http://homeassistant.local:8080",
                        "suggested_admin_url": "http://homeassistant.local:8081"}
    assert json.loads((tmp_path / "ui.json").read_text())["addresses"]["read_url"] == "https://solar.example.org"
    assert c.get("/api/addresses", headers=h()).json()["read_url"] == "https://solar.example.org"
    bad = c.put("/api/addresses", content=json.dumps({"read_url": "javascript:alert(1)"}), headers=h(write=True))
    assert bad.status_code == 422


def test_the_development_listener_answers_loopback_only(tmp_path):
    local = {"Host": "127.0.0.1:8099"}
    assert ui(tmp_path, peer="127.0.0.1", dev=True).get("/api/session", headers=local).json()["user"] == "Entwicklung"
    assert ui(tmp_path, peer="127.0.0.1", dev=True).get("/api/session", headers={"Host": "localhost:8099"}).status_code == 200
    assert ui(tmp_path, peer="192.168.1.50", dev=True).get("/api/session", headers=local).status_code == 403
    assert ui(tmp_path, peer="127.0.0.1", dev=True).put("/api/addresses", json={}, headers=local).status_code == 403
    # DNS rebinding: a foreign page whose name now points at 127.0.0.1 – the Host header gives it away
    assert ui(tmp_path, peer="127.0.0.1", dev=True).get("/api/session", headers={"Host": "evil.example:8099"}).status_code == 403


def test_a_422_never_echoes_what_was_entered(tmp_path):
    """Codex P4 review H1: FastAPI's default answer repeats the input – a key or token would come back."""
    from solar_visor_hub.config import SecretFiles
    from solar_visor_hub.ui_admin import AdminContext
    secret = "x" * 5000
    ctx = UiContext(mode="plant", running_revision=1, stored_revision=lambda: 1, settings=UiSettings(tmp_path / "ui.json"),
                    web_dir=tmp_path / "noweb",
                    admin=AdminContext(mode="plant", secrets=SecretFiles(tmp_path / "ha", tmp_path / "influx")))
    c = TestClient(Peer(DevGuard(create_ui_app(ctx)), "127.0.0.1"))
    r = c.put("/api/secrets/influx", json={"value": secret}, headers={"Host": "127.0.0.1:1", "X-Solar-Visor": "1"})
    assert r.status_code == 422 and secret not in r.text and r.json()["fields"] == ["body.value"]


# --- what it serves -------------------------------------------------------------------------------------------------

def test_the_built_page_and_its_assets_are_served_relative_to_the_ingress_prefix(tmp_path):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text('<script type="module" src="./assets/app.js"></script>')
    (web / "assets" / "app.js").write_text("console.log(1)")
    c = ui(tmp_path, web_dir=web)
    page = c.get("/", headers=h())
    assert page.status_code == 200 and "./assets/app.js" in page.text
    assert c.get("/assets/app.js", headers=h()).text == "console.log(1)"
    assert c.get("/assets/app.js").status_code == 401                                        # assets need the user too
    assert c.get("/api/nothing", headers=h()).status_code == 404
    assert "nicht gebaut" in ui(tmp_path).get("/", headers=h()).text                           # without a build


def test_no_interface_route_on_port_a_or_b():
    from solar_visor_hub.app import create_admin_app, create_public_app
    for app in (create_public_app(), create_admin_app()):
        c = TestClient(app)
        assert c.get("/api/session").status_code == 404 and c.get("/api/addresses").status_code == 404
