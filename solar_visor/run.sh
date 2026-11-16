#!/bin/sh
# Add-on entry point: the Supervisor mounts /data as root, the hub runs as its own user.
# options.json stays with the Supervisor, which rewrites it as root with mode 0600 on every start: the hub user gets
# read access through its group. Everything else in /data belongs to the hub.
set -eu
mkdir -p /data/secrets
chown solarvisor:solarvisor /data
find /data -mindepth 1 ! -name options.json -exec chown solarvisor:solarvisor {} +
chmod 700 /data/secrets
if [ -f /data/options.json ]; then
    chgrp solarvisor /data/options.json
    chmod 640 /data/options.json
fi
echo "Solar Visor hub $(cat /BUILD 2>/dev/null || echo '?') starting"
exec setpriv --reuid=solarvisor --regid=solarvisor --clear-groups python -m solar_visor_hub.server
