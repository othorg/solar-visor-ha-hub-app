"""InfluxDB connection of the add-on (address, database, retention policy) – set in the web interface.

The add-on used to take these from its Home Assistant options; they live in ``/data/influx.json`` now so that the whole
setup happens in one place (user decision 2026-11-15). The token stays a secret file (``secrets/influx_token``).
Existing options are taken over once (``migrate_from_options``). A change applies at once: the history reads address
and token through ``InfluxConnection``, which never pairs one address with another address's token.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import ConfigError, store_secret

log = logging.getLogger("solar_visor_hub.influx_config")
FILE_NAME = "influx.json"
_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
OPTION_KEYS = ("influx_url", "influx_db", "influx_rp")


@dataclass(frozen=True)
class InfluxConfig:
    url: str
    database: str
    retention_policy: str | None = None     # None: the database's default policy


def validate(cfg: InfluxConfig) -> InfluxConfig:
    url = cfg.url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("address must be http(s)://host[:port]")
    if parts.username or parts.password or parts.query or parts.fragment or len(url) > 200:
        raise ConfigError("address without credentials, query or fragment")
    try:
        parts.port                          # noqa: B018 – raises for a port out of range
    except ValueError:
        raise ConfigError("port out of range") from None
    if not _NAME.match(cfg.database.strip()):
        raise ConfigError("database: 1–64 letters, digits, _ . -")
    rp = (cfg.retention_policy or "").strip() or None
    if rp is not None and not _NAME.match(rp):
        raise ConfigError("retention policy: 1–64 letters, digits, _ . -")
    return InfluxConfig(url=url.rstrip("/"), database=cfg.database.strip(), retention_policy=rp)


class InfluxConfigStore:
    """``influx.json`` in the data directory (atomic write); ``current`` is None while nothing was stored."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    @property
    def current(self) -> InfluxConfig | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return validate(InfluxConfig(url=raw["url"], database=raw["database"],
                                         retention_policy=raw.get("retention_policy")))
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, KeyError, ConfigError):
            log.warning("influx config unreadable – ignored")
            return None

    def save(self, cfg: InfluxConfig) -> InfluxConfig:
        cfg = validate(cfg)
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(cfg)))
            os.replace(tmp, self.path)
        return cfg


def from_options(options: dict[str, str], default: InfluxConfig) -> InfluxConfig | None:
    """The connection the former add-on options described (empty strings count as not set; ``none`` = no policy);
    None when none of them was set."""
    if not any(k in options for k in OPTION_KEYS):
        return None
    rp = options.get("influx_rp", default.retention_policy or "")
    return InfluxConfig(url=options.get("influx_url", default.url), database=options.get("influx_db", default.database),
                        retention_policy=None if rp.strip().lower() in ("", "none") else rp)


def migrate_from_options(store: InfluxConfigStore, options: dict[str, str], default: InfluxConfig) -> bool:
    """Takes the former add-on options over once: only while nothing is stored and only valid values."""
    if store.path.exists():
        return False
    cfg = from_options(options, default)
    if cfg is None:
        return False
    try:
        store.save(cfg)
    except (ConfigError, OSError) as exc:
        log.warning("influx options not taken over: %s", type(exc).__name__)
        return False
    log.info("influx connection taken over from the add-on options")
    return True


class TokenRequired(ConfigError):
    """A new address while a token is stored: the token has to be entered again."""


def origin(url: str) -> tuple[str, str, int | None]:
    p = urlsplit(url)
    return p.scheme, (p.hostname or "").lower(), p.port or {"http": 80, "https": 443}.get(p.scheme)


class InfluxConnection:
    """Address and token of the history as one unit (Codex review 2026-11-15): ``update`` checks and writes token and
    address under one lock and starts a new generation; ``token(generation)`` refuses a token for a connection that
    changed since the caller built its client – so a request for address A never carries the token stored for address B.
    If a failed save cannot be undone, the token is removed (it has to be entered again) – fail closed."""

    def __init__(self, store: InfluxConfigStore | None, fallback: InfluxConfig, token_file: Path,
                 read_token: Callable[[], str]) -> None:
        self._store, self._fallback, self._token_file, self._read_token = store, fallback, token_file, read_token
        self._lock = threading.Lock()
        self._generation = 0
        self._closed = False

    @property
    def configurable(self) -> bool:
        return self._store is not None

    def _current(self) -> InfluxConfig:
        return (self._store.current if self._store is not None else None) or self._fallback

    def config(self) -> tuple[InfluxConfig, int]:
        with self._lock:
            return self._current(), self._generation

    def token(self, generation: int) -> str:
        with self._lock:
            if self._closed or generation != self._generation:
                raise ConfigError("InfluxDB connection changed")
            return self._read_token()

    def _token_stored(self) -> bool:
        return self._token_file.exists() and self._token_file.stat().st_size > 0

    def update(self, cfg: InfluxConfig, token: str | None) -> InfluxConfig:
        """Validates first; inside the lock: a new address needs the token again (``TokenRequired``), then token and
        address are written together – a failed address write restores the previous token or, if that fails too,
        closes the connection."""
        if self._store is None:
            raise ConfigError("InfluxDB connection not configurable here")
        cfg = validate(cfg)
        with self._lock:
            if token is None and self._token_stored() and origin(cfg.url) != origin(self._current().url):
                raise TokenRequired("re-enter the token for a new address")
            previous = self._token_file.read_text(encoding="utf-8").strip() if token is not None and self._token_stored() else None
            if token is not None:
                self._token_file.parent.mkdir(mode=0o700, exist_ok=True)
                store_secret(self._token_file, token)
            try:
                self._store.save(cfg)
            except OSError:
                self._generation += 1               # clients of the old pairing re-check before their next request
                if token is not None:
                    try:
                        if previous:
                            store_secret(self._token_file, previous)
                        else:
                            self._token_file.unlink(missing_ok=True)
                    except OSError:
                        # undo failed: the stored token belongs to the other address – remove it (durable, also over a
                        # restart); only if even that fails, refuse every token in this process
                        try:
                            self._token_file.unlink(missing_ok=True)
                            log.error("influx token could not be restored – removed, enter it again")
                        except OSError:
                            self._closed = True
                            log.error("influx token could not be restored or removed – connection closed")
                raise
            self._generation += 1
            if token is not None:
                self._closed = False                # only a freshly entered token reopens a closed connection
        return cfg
