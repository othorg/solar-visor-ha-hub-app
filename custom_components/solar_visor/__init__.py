"""Solar Visor: the hub (Home Assistant add-on) on Home Assistant's own address, ``/api/solar_visor/…``
(plan ha-remote-access §3.2) – the apps reach it wherever Home Assistant is reachable (Home Assistant Cloud, a proxy,
a VPN) without forwarding the add-on's ports.

The integration only forwards (rules.py): no entities, no services, nothing is written to Home Assistant. The hub's
device tokens stay the authentication, so the view itself needs no Home Assistant login. Home Assistant cannot remove a
registered view: it is registered once and forwards to the target of the loaded entry (503 without one).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from . import rules
from .const import CONF_ADMIN_PORT, CONF_HOST, CONF_READ_PORT, DOMAIN

_LOG = logging.getLogger(__name__)
ISSUE_INCOMPATIBLE = "hub_incompatible"


@dataclass
class Target:
    host: str
    read_port: int
    admin_port: int
    session: ClientSession
    slots: asyncio.Semaphore

    def base(self, kind: str) -> str:
        return f"http://{self.host}:{self.read_port if kind == rules.READ else self.admin_port}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("view"):
        hass.http.register_view(SolarVisorView(hass))
        data["view"] = True
    # one session for Home Assistant's lifetime (closed by Home Assistant on stop; reloads reuse it). The hub's gzip
    # passes through unchanged.
    if (session := data.get("session")) is None:
        session = data["session"] = async_create_clientsession(hass, auto_decompress=False)
    target = Target(entry.data[CONF_HOST], entry.data[CONF_READ_PORT], entry.data[CONF_ADMIN_PORT], session,
                    asyncio.Semaphore(rules.MAX_CONCURRENT))
    data["target"] = target
    await _check_hub(hass, target)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop("target", None)     # the view answers 503 until the entry is loaded again
    return True


async def _check_hub(hass: HomeAssistant, target: Target) -> None:
    """A repair hint when the hub does not speak this bridge version; a hub that is not running yet is no error –
    requests answer 502 until it is."""
    try:
        async with target.session.get(f"{target.base(rules.READ)}/health/ready", allow_redirects=False,
                                      timeout=ClientTimeout(total=5)) as resp:
            versions = resp.headers.get(rules.VERSIONS_HEADER)
    except (ClientError, TimeoutError) as err:
        _LOG.info("Solar Visor hub not reachable yet at %s: %s", target.host, type(err).__name__)
        return
    if rules.compatible(versions):
        ir.async_delete_issue(hass, DOMAIN, ISSUE_INCOMPATIBLE)
    else:
        ir.async_create_issue(hass, DOMAIN, ISSUE_INCOMPATIBLE, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                              translation_key=ISSUE_INCOMPATIBLE)


def _error(status: int, code: str, message: str) -> web.Response:
    return web.Response(status=status, body=rules.error_body(code, message), content_type="application/json")


async def _read_limited(stream: object, limit: int) -> bytes | None:
    """The whole stream, or ``None`` beyond ``limit`` bytes."""
    chunks, size = [], 0
    async for chunk in stream.iter_chunked(64 * 1024):  # type: ignore[attr-defined]
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class SolarVisorView(HomeAssistantView):
    """``/api/solar_visor/{path}`` → the hub's read or admin listener (rules.route)."""

    url = "/api/solar_visor/{path:.+}"
    name = "api:solar_visor"
    requires_auth = False             # the hub's device tokens authenticate; nothing here acts on Home Assistant

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, path: str) -> web.Response:
        return await self._forward(request, path)

    async def post(self, request: web.Request, path: str) -> web.Response:
        return await self._forward(request, path)

    async def put(self, request: web.Request, path: str) -> web.Response:
        return await self._forward(request, path)

    async def delete(self, request: web.Request, path: str) -> web.Response:
        return await self._forward(request, path)

    async def _forward(self, request: web.Request, path: str) -> web.Response:
        found = rules.route(request.method, path)
        if found is None:
            return _error(404, "bridge_not_found", "Unbekannter Pfad.")
        target: Target | None = self.hass.data.get(DOMAIN, {}).get("target")
        if target is None:
            return _error(503, "bridge_not_configured", "Die Integration Solar Visor ist nicht eingerichtet.")
        if (request.content_length or 0) > rules.MAX_BODY:
            return _error(413, "bridge_too_large", "Anfrage zu groß.")
        query = rules.query_string(request.rel_url.raw_query_string)
        if query is None:
            return _error(414, "bridge_too_long", "Anfrage zu lang.")
        url = f"{target.base(found.target)}/{found.path}" + (f"?{query}" if query else "")
        if target.slots.locked():
            return _error(503, "bridge_busy", "Hub ausgelastet – bitte gleich erneut versuchen.")
        timeout = rules.READ_TIMEOUT_S if found.target == rules.READ else rules.ADMIN_TIMEOUT_S
        # capacity first, then one deadline over upload, forwarding and answer: a client that withholds its body
        # cannot hold a slot or a connection beyond it (Codex review P2)
        async with target.slots:
            try:
                async with asyncio.timeout(timeout):
                    return await self._exchange(request, target, found, url)
            except TimeoutError:
                return _error(504, "bridge_timeout", "Zeitüberschreitung beim Hub.")

    async def _exchange(self, request: web.Request, target: Target, found: rules.Route, url: str) -> web.Response:
        body = None
        if request.method in ("POST", "PUT"):
            body = await _read_limited(request.content, rules.MAX_BODY)
            if body is None:
                return _error(413, "bridge_too_large", "Anfrage zu groß.")
            if problem := rules.enroll_problem(found.path, body):
                return _error(403, problem, "Über Home Assistant nur mit dem Kopplungstext koppeln (QR-Code scannen "
                                            "oder Text einfügen) – den Code von Hand nur im Heimnetz eingeben.")
        try:
            async with target.session.request(request.method, url, headers=rules.request_headers(request.headers),
                                              data=body, allow_redirects=False,
                                              timeout=ClientTimeout(total=None, sock_connect=rules.CONNECT_TIMEOUT_S)) as resp:
                data = await _read_limited(resp.content, rules.MAX_RESPONSE)
                if data is None:
                    return _error(502, "bridge_too_large", "Antwort des Hubs zu groß.")
                return web.Response(status=resp.status, body=data, headers=rules.response_headers(resp.headers))
        except ClientError as err:
            _LOG.debug("Solar Visor hub not reachable: %s", type(err).__name__)
            return _error(502, "bridge_hub_unreachable", "Home Assistant ist erreichbar, der Solar-Visor-Hub nicht.")
