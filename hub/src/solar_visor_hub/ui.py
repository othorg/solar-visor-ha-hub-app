"""Web interface of the add-on (plan ha-addon 4.8, P4 detail plan): its own listener behind Home Assistant Ingress.

Access (P0 finding §4, P4 detail plan §2):
- only from the Supervisor's Ingress proxy (peer ``172.30.32.2``) – the identity headers are trusted from there only;
- only for a Home Assistant administrator: ``X-Remote-User-Id`` must be an active admin in ``config/auth/list``
  (cached briefly; unknown list → 503, never open);
- writes only with ``X-Solar-Visor: 1`` (a browser cannot send it cross-site without a preflight Home Assistant does not
  grant – no CSRF over the HA session) and JSON bodies.
Port A never carries any of this; port B keeps the app pairing and administration as before.

For development and the "fresh setup" acceptance a second guard serves the same app on a loopback address only, with a
fixed identity – standalone profile only, never in the add-on (``load_ui_bind``).

Cookies and identity headers are never logged; uvicorn's access log stays off.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .app import sanitize_validation_errors
from .ui_admin import AdminContext
from .ui_admin import register as register_admin
from .ui_ha import register as register_ha
from .ui_plant import PlantEditing
from .ui_plant import register as register_plant

log = logging.getLogger("solar_visor_hub.ui")

INGRESS_PEER = "172.30.32.2"
WRITE_HEADER = "x-solar-visor"
USER_KEY = "solar_visor.user"
WEB_DIR = Path(__file__).parent / "web"
ADMIN_CACHE_S = 60.0
ADMIN_STALE_MAX_S = 600.0       # a list that cannot be refreshed is used this long at most, then nobody gets in
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'")


# --- listener addresses ---------------------------------------------------------------------------------------------

class UiBindError(ValueError):
    pass


@dataclass(frozen=True)
class UiBind:
    host: str
    port: int
    ingress: bool          # True: Ingress guard (add-on); False: development guard (loopback, fixed identity)


def load_ui_bind(in_addon: bool, env: dict[str, str] | None = None) -> UiBind | None:
    """Add-on: always (``SOLAR_VISOR_UI_BIND``, default ``0.0.0.0:8099`` = ``ingress_port``; the port is not published,
    only the Supervisor's network reaches it). Standalone: only the development listener, and only on loopback."""
    env = dict(os.environ if env is None else env)
    if in_addon:
        if env.get("SOLAR_VISOR_UI_DEV_BIND"):
            raise UiBindError("the development listener is not available in the add-on")
        host, port = _split(env.get("SOLAR_VISOR_UI_BIND", "0.0.0.0:8099"))
        return UiBind(host, port, ingress=True)
    raw = env.get("SOLAR_VISOR_UI_DEV_BIND")
    if not raw:
        return None
    host, port = _split(raw)
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise UiBindError("the development listener binds to a loopback address only (127.0.0.1)")
    return UiBind(host, port, ingress=False)


def _split(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise UiBindError(f"invalid bind address {value!r}, expected host:port")
    return host.strip("[]"), int(port)


# --- who may use it ---------------------------------------------------------------------------------------------------

Read = Callable[[dict[str, Any]], Awaitable[Any]]


class AdminList:
    """Active administrators of Home Assistant (``config/auth/list``), refreshed at most every minute. Only user ids
    are kept – the list carries names and credential types, none of it is stored or logged."""

    def __init__(self, read: Read, now: Callable[[], float] = time.monotonic, max_age: float = ADMIN_CACHE_S,
                 stale_max: float = ADMIN_STALE_MAX_S) -> None:
        self._read, self._now, self._max_age, self._stale_max = read, now, max_age, stale_max
        self._admins: frozenset[str] | None = None
        self._at = 0.0

    async def is_admin(self, user_id: str) -> bool | None:
        """True/False, or None if the list cannot be read (the caller refuses with 503). While Home Assistant does not
        answer, the last list holds for a few minutes (a short reconnect must not lock the administrator out) – a
        demoted administrator keeps access at most that long (Codex P4 review N1)."""
        if self._admins is None or self._now() - self._at > self._max_age:
            try:
                users = await self._read({"type": "config/auth/list"})
            except Exception as exc:   # noqa: BLE001 – no list, no access; the reason without content
                log.warning("administrators of Home Assistant not readable: %s", type(exc).__name__)
                if self._admins is None or self._now() - self._at > self._stale_max:
                    return None
                return user_id in self._admins
            self._admins = frozenset(
                u["id"] for u in users or [] if isinstance(u, dict) and isinstance(u.get("id"), str)
                and u.get("is_active", True) and (u.get("is_owner") or "system-admin" in (u.get("group_ids") or [])))
            self._at = self._now()
        return user_id in self._admins


def _reply(status: int, detail: str) -> tuple[dict[str, Any], dict[str, Any]]:
    body = json.dumps({"detail": detail}).encode()
    return ({"type": "http.response.start", "status": status,
             "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]},
            {"type": "http.response.body", "body": body})


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


class _Guard:
    """Common part: writes need the custom header (and JSON when they carry a body)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def identity(self, scope: dict[str, Any]) -> tuple[dict[str, Any] | None, tuple[int, str] | None]:
        raise NotImplementedError

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        user, refusal = await self.identity(scope)
        if refusal is None and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            if _header(scope, WRITE_HEADER.encode()) != "1":
                refusal = (403, "write header missing")
            elif scope["method"] in ("POST", "PUT", "PATCH") and not (
                    _header(scope, b"content-type") or "").split(";")[0].strip().lower() == "application/json":
                refusal = (415, "JSON expected")
        if refusal is not None:
            start, body = _reply(*refusal)
            await send(start)
            await send(body)
            return
        scope[USER_KEY] = user
        await self.app(scope, receive, send)


class IngressGuard(_Guard):
    def __init__(self, app: Any, admins: AdminList, peer: str = INGRESS_PEER) -> None:
        super().__init__(app)
        self.admins, self.peer = admins, peer

    async def identity(self, scope: dict[str, Any]) -> tuple[dict[str, Any] | None, tuple[int, str] | None]:
        client = scope.get("client")
        if not client or str(client[0]) != self.peer:
            return None, (403, "only through Home Assistant")
        user_id = _header(scope, b"x-remote-user-id")
        if not user_id:
            return None, (401, "no Home Assistant user")
        admin = await self.admins.is_admin(user_id)
        if admin is None:
            return None, (503, "administrators of Home Assistant not readable")
        if not admin:
            return None, (403, "administrators only")
        name = _header(scope, b"x-remote-user-display-name") or _header(scope, b"x-remote-user-name")
        return {"id": user_id, "name": name}, None


class DevGuard(_Guard):
    """Development listener: loopback peers only, a fixed identity – and only under a loopback host name, so a web
    page cannot reach it by rebinding its own name to 127.0.0.1 (Codex P4 review N7)."""

    LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")

    async def identity(self, scope: dict[str, Any]) -> tuple[dict[str, Any] | None, tuple[int, str] | None]:
        client = scope.get("client")
        try:
            loopback = bool(client) and ipaddress.ip_address(str(client[0])).is_loopback
        except ValueError:
            loopback = False
        host = (_header(scope, b"host") or "").lower()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if not loopback or name not in self.LOOPBACK_HOSTS:
            return None, (403, "loopback only")
        return {"id": "dev", "name": "Entwicklung"}, None


# --- settings of the interface (/data/ui.json) ------------------------------------------------------------------------

class Addresses(BaseModel):
    read_url: str | None = Field(default=None, max_length=300)
    admin_url: str | None = Field(default=None, max_length=300)

    @field_validator("read_url", "admin_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")) or any(c.isspace() for c in v):
            raise ValueError("http(s) address expected")
        return v


class UiSettings:
    """Addresses for the apps and other settings of the interface – no plant data, no secrets."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def addresses(self) -> Addresses:
        try:
            raw = json.loads(self.path.read_text())
            return Addresses.model_validate(raw.get("addresses") or {})
        except FileNotFoundError:
            return Addresses()
        except (ValueError, OSError) as exc:
            log.warning("ui settings unreadable (%s) – using defaults", type(exc).__name__)
            return Addresses()

    def set_addresses(self, a: Addresses) -> Addresses:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text())
            except (FileNotFoundError, ValueError):
                raw = {}
            raw["addresses"] = a.model_dump()
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw, indent=2))
            os.replace(tmp, self.path)
        return a


# --- the app ----------------------------------------------------------------------------------------------------------

@dataclass
class UiContext:
    mode: str
    running_revision: int | None
    stored_revision: Callable[[], int | None]
    settings: UiSettings
    web_dir: Path = WEB_DIR
    editing: PlantEditing | None = None
    startup_error: str | None = None
    read: Read | None = None                      # Home Assistant (small reads) for sensor search and checks
    devices: Any = None                           # DeviceStore: pairing codes for the apps
    admin: AdminContext | None = None             # overview, history, devices and diagnostics


class SessionView(BaseModel):
    user: str | None
    mode: str
    version: str
    running_revision: int | None
    stored_revision: int | None
    startup_error: str | None = None


def build_label(stamp: Path = Path("/BUILD")) -> str:
    """Version with the build stamp of the add-on image (``0.1.0.155 (129c5ea)``), else the package version."""
    try:
        return stamp.read_text().strip() or __version__
    except OSError:
        return __version__


class PairingIn(BaseModel):
    scope: Literal["read", "admin"] = "admin"


class PairingView(BaseModel):
    code: str
    scope: str
    valid_minutes: int
    read_url: str | None
    admin_url: str | None
    payload: str | None          # versioned pairing text for scanning/pasting in the apps (P5); None without addresses
    qr_svg: str | None           # the payload as QR code (SVG), for the iPhone camera


def qr_svg(payload: str | None) -> str | None:
    if payload is None:
        return None
    import io

    import segno
    out = io.BytesIO()
    segno.make(payload, error="m").save(out, kind="svg", scale=4, border=2, xmldecl=False, dark="#000", light="#fff")
    return out.getvalue().decode()


def pairing_payload(a: Addresses, code: str) -> str | None:
    """``solarvisor://pair?v=1&read=…&admin=…&code=…`` – the contract of the QR code (fixture in contracts/)."""
    if not a.read_url or not a.admin_url:
        return None
    return "solarvisor://pair?" + urlencode({"v": 1, "read": a.read_url, "admin": a.admin_url, "code": code})


class AddressesView(Addresses):
    suggested_read_url: str | None = None
    suggested_admin_url: str | None = None


def _suggest(request: Request) -> tuple[str | None, str | None]:
    """From the host the browser used for Home Assistant (forwarded by Ingress) – the apps reach the add-on's ports on
    the same host in the home network."""
    host = request.headers.get("x-forwarded-host") or ""
    host = host.split(",")[0].strip()
    if host.startswith("["):
        name = host.split("]")[0] + "]"
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not name:
        return None, None
    return f"http://{name}:8080", f"http://{name}:8081"


def create_ui_app(ctx: UiContext) -> FastAPI:
    app = FastAPI(title="Solar Visor – interface", docs_url=None, redoc_url=None, openapi_url=None)
    sanitize_validation_errors(app)                 # a 422 never echoes what was entered (keys, tokens)

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/session", response_model=SessionView)
    def session(request: Request) -> SessionView:
        user = request.scope.get(USER_KEY) or {}
        return SessionView(user=user.get("name"), mode=ctx.mode, version=build_label(),
                           running_revision=ctx.running_revision, stored_revision=ctx.stored_revision(),
                           startup_error=ctx.startup_error)

    @app.get("/api/addresses", response_model=AddressesView)
    def get_addresses(request: Request) -> AddressesView:
        read, admin = _suggest(request)
        return AddressesView(**ctx.settings.addresses().model_dump(), suggested_read_url=read, suggested_admin_url=admin)

    @app.put("/api/addresses", response_model=AddressesView)
    def put_addresses(body: Addresses, request: Request) -> AddressesView:
        saved = ctx.settings.set_addresses(body)
        read, admin = _suggest(request)
        return AddressesView(**saved.model_dump(), suggested_read_url=read, suggested_admin_url=admin)

    if ctx.editing is not None:
        register_plant(app, ctx.editing)
    register_ha(app, ctx.read)
    if ctx.admin is not None:
        register_admin(app, ctx.admin)

    @app.post("/api/pairing", response_model=PairingView)
    def pairing(body: PairingIn) -> PairingView:
        """A one-time code for the apps (10 minutes) and the pairing text built from the addresses. The text carries
        the code, never a device token; the apps redeem it over port B as before (P5 adds scanning)."""
        if ctx.devices is None:
            raise HTTPException(status_code=503, detail="devices unavailable")
        code = ctx.devices.create_pairing_code(body.scope)
        a = ctx.settings.addresses()
        payload = pairing_payload(a, code)
        return PairingView(code=code, scope=body.scope, valid_minutes=10, read_url=a.read_url, admin_url=a.admin_url,
                           payload=payload, qr_svg=qr_svg(payload))

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def unknown_api(rest: str) -> JSONResponse:
        raise HTTPException(status_code=404, detail="unknown")

    index = ctx.web_dir / "index.html"
    if (ctx.web_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=ctx.web_dir / "assets"), name="assets")

    @app.get("/", response_model=None)
    @app.get("/index.html", response_model=None)
    def page() -> FileResponse | HTMLResponse:
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Solar Visor</title>"
                            "<p>Die Oberfläche ist in diesem Paket nicht gebaut (web/ → npm run build).</p>")

    return app
