"""Remote access over Home Assistant (plan ha-remote-access §3.2): the ``solar_visor`` integration forwards
``/api/solar_visor/…`` on Home Assistant's own address to the hub's listeners and marks every forwarded request with
``X-Solar-Visor-Bridge: <version>``.

The hub answers ``/health/ready`` with the bridge versions it understands (the integration checks them instead of
equal release numbers) and remembers when it last saw a forwarded request – the interface hints at a missing
integration. The header grants nothing: tokens and invitations stay the only authentication.
"""

from __future__ import annotations

import threading
import time
from typing import Any

BRIDGE_VERSIONS = (1,)
HEADER = "X-Solar-Visor-Bridge"
VERSIONS_HEADER = "X-Solar-Visor-Bridge-Versions"
SEEN_WINDOW_S = 7 * 24 * 3600            # an integration that forwarded within a week counts as set up
API_PATH = "/api/solar_visor"            # where the integration serves the hub on Home Assistant's address


class BridgeSeen:
    """When the hub last saw a request forwarded by the integration (process-wide, both listeners)."""

    def __init__(self) -> None:
        self._last: float | None = None
        self._lock = threading.Lock()

    def mark(self, now: float | None = None) -> None:
        with self._lock:
            self._last = time.monotonic() if now is None else now

    def recently(self, now: float | None = None) -> bool:
        with self._lock:
            last = self._last
        return last is not None and (time.monotonic() if now is None else now) - last < SEEN_WINDOW_S

    def reset(self) -> None:
        with self._lock:
            self._last = None


SEEN = BridgeSeen()


def note_bridge(headers: Any) -> None:
    """Called for every request on the read and admin listener."""
    if headers.get(HEADER) is not None:
        SEEN.mark()


def ha_url(urls: dict[str, Any] | None) -> str | None:
    """The address the apps should use over Home Assistant: Home Assistant Cloud before the configured external URL –
    the internal URL is never suggested (it fails away from home). ``None`` without either."""
    if not urls:
        return None
    for key in ("cloud", "external", "external_url"):
        value = urls.get(key)
        if isinstance(value, str) and value.startswith(("https://", "http://")) and not any(c.isspace() for c in value):
            return value.rstrip("/") + API_PATH
    return None
