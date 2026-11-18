"""Constants of the Solar Visor integration (plan ha-remote-access §3.2)."""

DOMAIN = "solar_visor"
CONF_HOST = "host"
CONF_READ_PORT = "read_port"
CONF_ADMIN_PORT = "admin_port"
DEFAULT_READ_PORT = 8080
DEFAULT_ADMIN_PORT = 8081
# the add-on's host name on the Supervisor network: public add-on repository first, then a local add-on
CANDIDATE_HOSTS = ("81b20def-solar-visor", "local-solar-visor")
