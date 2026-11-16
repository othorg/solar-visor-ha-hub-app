# Solar Visor hub as a Home Assistant add-on. The build context is produced by scripts/build-context.sh: it adds the hub wheel
# and the hashed requirements next to this file, so the image never copies from outside its own folder.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOLAR_VISOR_RUNTIME=addon

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin solarvisor

COPY requirements.txt solar_visor_hub-*.whl /tmp/pkg/
RUN pip install --no-cache-dir --require-hashes -r /tmp/pkg/requirements.txt \
    && pip install --no-cache-dir --no-deps /tmp/pkg/solar_visor_hub-*.whl \
    && rm -rf /tmp/pkg

COPY run.sh /run.sh
COPY solar-visor-hub-cli /usr/local/bin/solar-visor-hub-cli
COPY BUILD /BUILD
RUN chmod 755 /run.sh /usr/local/bin/solar-visor-hub-cli

# licenses of the app and of the web dependencies bundled into the interface
COPY licenses /usr/share/doc/solar-visor-hub

CMD ["/run.sh"]
