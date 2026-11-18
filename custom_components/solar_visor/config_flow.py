"""Setup of the Solar Visor integration: where the hub runs (plan ha-remote-access §3.2).

Explicit, one instance. The form is prefilled with the add-on found on the Supervisor network (public repository, then
a local add-on); a hub elsewhere (standalone next to Home Assistant Container) is entered by hand. Saved only when the
hub answers and speaks this bridge version.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from aiohttp import ClientError, ClientTimeout

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import rules
from .const import (
    CANDIDATE_HOSTS,
    CONF_ADMIN_PORT,
    CONF_HOST,
    CONF_READ_PORT,
    DEFAULT_ADMIN_PORT,
    DEFAULT_READ_PORT,
    DOMAIN,
)

_LOG = logging.getLogger(__name__)


class SolarVisorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _probe(self, host: str, port: int) -> str | None:
        """``None`` if the hub answers with a compatible bridge version, else an error key of the form."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(f"http://{host}:{port}/health/ready", allow_redirects=False,
                                   timeout=ClientTimeout(total=5)) as resp:
                if resp.status not in (200, 503):
                    return "cannot_connect"
                return None if rules.compatible(resp.headers.get(rules.VERSIONS_HEADER)) else "incompatible"
        except (ClientError, TimeoutError, ValueError) as err:
            _LOG.debug("no Solar Visor hub at %s:%s: %s", host, port, type(err).__name__)
            return "cannot_connect"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            if problem := await self._probe(host, user_input[CONF_READ_PORT]):
                errors["base"] = problem
            else:
                return self.async_create_entry(title="Solar Visor", data={
                    CONF_HOST: host, CONF_READ_PORT: user_input[CONF_READ_PORT],
                    CONF_ADMIN_PORT: user_input[CONF_ADMIN_PORT]})
            default_host = host
        else:
            default_host = ""
            for candidate in CANDIDATE_HOSTS:
                if await self._probe(candidate, DEFAULT_READ_PORT) is None:
                    default_host = candidate
                    break
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=default_host): str,
            vol.Required(CONF_READ_PORT, default=DEFAULT_READ_PORT): cv.port,
            vol.Required(CONF_ADMIN_PORT, default=DEFAULT_ADMIN_PORT): cv.port,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
