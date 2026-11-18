"""What the integration forwards, and how – without any Home Assistant import, so it is tested on its own
(plan ha-remote-access §3.2).

Only two targets exist, both fixed at setup: the hub's read listener (GET of ``health/ready`` and ``v2/…``) and its
admin listener (``admin/…``). Pairing over Home Assistant takes the pairing text's invitation only – the 8-digit code
is for typing in at home. Everything else is 404. The hub's tokens stay the only authentication.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

BRIDGE_VERSION = 1
BRIDGE_HEADER = "X-Solar-Visor-Bridge"
VERSIONS_HEADER = "X-Solar-Visor-Bridge-Versions"

READ, ADMIN = "read", "admin"
MAX_BODY = 16 * 1024                 # admin bodies are small (pairing, mapping, RSCP settings)
MAX_RESPONSE = 8 * 1024 * 1024       # energy year / compare month stay far below
READ_TIMEOUT_S = 30.0                # long computations answer 202 and are polled
ADMIN_TIMEOUT_S = 20.0               # connection tests of the hub wait up to ~10 s
CONNECT_TIMEOUT_S = 5.0              # a stopped add-on: "hub unreachable" quickly, not after the whole deadline
MAX_CONCURRENT = 8

_SEGMENT = re.compile(r"^[A-Za-z0-9_.\-]+$")
_READ = re.compile(r"^(health/ready|v2/[A-Za-z0-9_.\-/]+)$")
_ADMIN = re.compile(r"^admin/(health|v1/[A-Za-z0-9_.\-/]+)$")
_ADMIN_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})
ENROLL = "admin/v1/devices/enroll"

REQUEST_HEADERS = ("Authorization", "If-None-Match", "Accept", "Accept-Encoding", "Content-Type")
RESPONSE_HEADERS = ("Content-Type", "Content-Encoding", "Vary", "ETag", "Cache-Control", "WWW-Authenticate")


@dataclass(frozen=True)
class Route:
    target: str                      # READ or ADMIN
    path: str                        # relative path on the hub, without a leading slash


def route(method: str, path: str) -> Route | None:
    """The hub listener for ``method path`` (path as Home Assistant matched it, decoded, without the prefix), or
    ``None`` – then the integration answers 404 without contacting the hub."""
    if not path or path.startswith("/") or path.endswith("/") or len(path) > 300:
        return None
    if any(seg in ("", ".", "..") or not _SEGMENT.match(seg) for seg in path.split("/")):
        return None
    if _READ.match(path):
        return Route(READ, path) if method == "GET" else None
    if _ADMIN.match(path) and method in _ADMIN_METHODS:
        return Route(ADMIN, path)
    return None


def enroll_problem(path: str, body: bytes) -> str | None:
    """Pairing over Home Assistant only with the invitation (192 bit) – an 8-digit code could be guessed from anywhere.
    Returns an error code, or ``None`` if the request may pass."""
    if path != ENROLL:
        return None
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return "bridge_invalid_request"
    if not isinstance(data, dict) or "code" in data or not isinstance(data.get("invite"), str):
        return "bridge_invite_required"
    return None


def query_string(raw: str) -> str | None:
    """The query string as received (``a=1&b=x,y``, still percent-encoded) – forwarded unchanged; ``""`` without one,
    ``None`` if it is too long (answered 414, never forwarded shortened)."""
    if not raw:
        return ""
    return raw if len(raw) <= 2000 else None


def request_headers(headers: dict[str, str] | object) -> dict[str, str]:
    get = headers.get  # type: ignore[attr-defined]
    out = {name: get(name) for name in REQUEST_HEADERS if get(name) is not None}
    out[BRIDGE_HEADER] = str(BRIDGE_VERSION)
    return out


def response_headers(headers: dict[str, str] | object) -> dict[str, str]:
    get = headers.get  # type: ignore[attr-defined]
    return {name: get(name) for name in RESPONSE_HEADERS if get(name) is not None}


def compatible(versions_header: str | None) -> bool:
    """Whether the hub speaks this bridge version (``X-Solar-Visor-Bridge-Versions: 1,2``)."""
    if not versions_header:
        return False
    return str(BRIDGE_VERSION) in {v.strip() for v in versions_header.split(",")}


def error_body(code: str, message: str) -> bytes:
    """Errors in the hub's shape (``{"detail": {"code": …, "message": …}}``); every code of the integration starts with
    ``bridge_`` – the apps tell them from the hub's own errors by that prefix and show ``message``."""
    return json.dumps({"detail": {"code": code, "message": message}}).encode()
