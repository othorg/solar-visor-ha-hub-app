"""The public package's neutral profile (plan hub-publish P4): no installation's data, and without a real profile the
existing-plant calculation stays shut (fail closed)."""

from __future__ import annotations

import pytest

from solar_visor_hub import profile_placeholder as ph
from solar_visor_hub import runtime
from solar_visor_hub.plant import LEGACY_PROFILE


def test_placeholder_carries_no_plant_data() -> None:
    assert ph.AVAILABLE is False
    assert ph.SERIES == [] and ph.EXTRA_SERIES == [] and ph.EVENTS == () and ph.PLANT_NAMES == {}
    assert ph.PV_PEAK_W is None and ph.KOSTAL_AC_MEASUREMENT is None and ph.PIPELINE_EVIDENCE == ""
    assert ph.INFLUX_DEFAULT == ("", "", "") and ph.RSCP_HOST == "" and ph.STANDALONE_HA_URL == ""
    assert ph.LEGACY_END == ph.T_MIN == ph.IOMETER_FROM and ph.DATA_SINCE == ph.T_MIN.date()


class _Plant:
    def __init__(self, profile: str | None) -> None:
        self.profile = profile


def test_without_a_profile_a_legacy_plant_stays_in_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime.profile, "AVAILABLE", False)
    mode = runtime.choose_mode(_Plant(LEGACY_PROFILE), addon=True)            # type: ignore[arg-type]
    assert mode.name == runtime.SETUP and mode.error == runtime.NO_PROFILE
    standalone = runtime.choose_mode(None, addon=False)
    assert standalone.name == runtime.SETUP and standalone.error == runtime.NO_PROFILE
    assert runtime.choose_mode(None, addon=True).name == runtime.SETUP and runtime.choose_mode(None, addon=True).error is None


def test_without_the_own_profile_everything_imports_and_the_legacy_stays_shut() -> None:
    """A fresh interpreter in which ``own_profile`` does not exist – as in the public package."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent('''
        import importlib.abc, sys
        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name == "solar_visor_hub.own_profile":
                    raise ModuleNotFoundError(name=name)
                return None
        sys.meta_path.insert(0, Block())
        import solar_visor_hub.server, solar_visor_hub.cli, solar_visor_hub.ui, solar_visor_hub.app
        from solar_visor_hub import epochs, plant_events, plant_legacy, profile, runtime, settings, sun
        from solar_visor_hub.plant import LEGACY_PROFILE
        assert profile.AVAILABLE is False and epochs.SERIES == [] and plant_events.EVENTS == ()
        assert sun.DEFAULT_LAT == 0.0 and settings.INFLUX_URL == "" and plant_legacy.NAMES == {}
        class P:
            profile = LEGACY_PROFILE
        assert runtime.choose_mode(P(), addon=True).error == runtime.NO_PROFILE
        assert plant_legacy.legacy_mapping(P()) is None
        assert [p.code for p in plant_legacy.legacy_rules(P(), P())] == ["legacy_profile_unavailable"]
        print("ok")
    ''')
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith("ok")
