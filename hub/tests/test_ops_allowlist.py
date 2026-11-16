"""P0 criterion 3: list of allowed outbound operations, enforced and statically checked."""

from pathlib import Path

import pytest

from solar_visor_hub.ops_allowlist import (
    FORBIDDEN_SOURCE_FRAGMENTS,
    ForbiddenOperation,
    check_ha_ws,
    check_influx,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "solar_visor_hub"


@pytest.mark.parametrize("msg", ["get_states", "subscribe_entities", "render_template", "unsubscribe_events"])
def test_allowed_ha_messages(msg):
    check_ha_ws(msg)


@pytest.mark.parametrize("msg", ["call_service", "fire_event", "execute_script", "config/entity_registry/update"])
def test_forbidden_ha_messages(msg):
    with pytest.raises(ForbiddenOperation):
        check_ha_ws(msg)


def test_influx_read_only():
    check_influx("/query", 'SELECT last("value") FROM "sensor.x"')
    check_influx("/query", "SHOW MEASUREMENTS")
    for bad in ("DROP MEASUREMENT x", 'SELECT * INTO "y" FROM "x"', "DELETE FROM x", "SELECT 1; DROP MEASUREMENT x"):
        with pytest.raises(ForbiddenOperation):
            check_influx("/query", bad)
    with pytest.raises(ForbiddenOperation):
        check_influx("/api/v2/write", None)


def test_source_tree_contains_no_write_paths():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "ops_allowlist.py":
            continue
        text = path.read_text(encoding="utf-8")
        offenders += [f"{path.name}: {frag}" for frag in FORBIDDEN_SOURCE_FRAGMENTS if frag in text]
    assert not offenders, offenders
