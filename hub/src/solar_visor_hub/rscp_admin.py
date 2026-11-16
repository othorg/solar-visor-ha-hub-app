"""E3/DC RSCP settings and credentials – one implementation for the app administration (port B) and the web interface.

Credentials are write-only: answers say only whether they exist. Everything is validated before anything is written;
stored credentials never go to a new address or key unless the password is entered again (review 2026-10-05).
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any

from .config import ConfigError, SecretFiles, store_secret
from .models import RscpConfigUpdate, RscpConfigView
from .rscp import RscpCollector, RscpConfigStore
from .rscp import validate as validate_rscp


class RscpUpdateError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    @property
    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _present(path: Any) -> bool:
    return bool(path is not None and path.exists() and path.stat().st_size > 0)


class RscpAdmin:
    def __init__(self, collector: RscpCollector, store: RscpConfigStore, secrets: SecretFiles) -> None:
        self.collector, self.store, self.secrets = collector, store, secrets
        self._lock = threading.Lock()        # credential and settings writes one at a time

    def view(self) -> RscpConfigView:
        cfg, view, sec = self.store.current, self.collector.view(), self.secrets
        return RscpConfigView(enabled=cfg.enabled, host=cfg.host, port=cfg.port, power_interval_s=cfg.power_interval_s,
                              system_interval_s=cfg.system_interval_s, user_set=_present(sec.rscp_user),
                              password_set=_present(sec.rscp_password), key_set=_present(sec.rscp_key),
                              state=view["state"], device=view["device"])

    def update(self, body: RscpConfigUpdate) -> RscpConfigView:
        st, sec = self.store, self.secrets
        changes = {k: getattr(body, k) for k in ("enabled", "host", "port", "power_interval_s", "system_interval_s")
                   if getattr(body, k) is not None}
        try:
            validate_rscp(replace(st.current, **changes))
        except ConfigError as exc:
            raise RscpUpdateError(422, "invalid_config", str(exc)) from None
        cur = st.current
        redirects = (("host" in changes and changes["host"] != cur.host) or ("port" in changes and changes["port"] != cur.port)
                     or body.key is not None)
        if redirects and _present(sec.rscp_password) and body.password is None:
            raise RscpUpdateError(422, "password_required", "re-enter the password to change address or key")
        updates = [(v, t) for v, t in ((body.user, sec.rscp_user), (body.password, sec.rscp_password),
                                       (body.key, sec.rscp_key)) if v is not None]
        if body.key is not None and len(body.key.encode("utf-8")) > 32:
            raise RscpUpdateError(422, "invalid_secret", "key longer than 32 bytes")
        if any(t is None or any(c in v for c in "\r\n\t") for v, t in updates):
            raise RscpUpdateError(422, "invalid_secret", "invalid characters")
        with self._lock:
            try:
                for value, target in updates:
                    target.parent.mkdir(mode=0o700, exist_ok=True)     # type: ignore[union-attr]
                    store_secret(target, value, allow_spaces=True)      # type: ignore[arg-type]
            except OSError:
                raise RscpUpdateError(503, "not_writable", "credentials not writable on the hub") from None
            if changes:
                st.update(**changes)
        self.collector.reconfigure()
        return self.view()
