"""Hub CLI (run on the hub host).

    python -m solar_visor_hub.cli pair [--scope read|admin]   → prints a one-time pairing code (10 min)
    python -m solar_visor_hub.cli devices                      → list paired devices (no tokens)
    python -m solar_visor_hub.cli revoke <device_id>
    python -m solar_visor_hub.cli set-secret ha|influx         → reads the token from stdin (never echoed),
                                                                 writes it atomically with mode 0600
    python -m solar_visor_hub.cli set-secret rscp-user|rscp-password|rscp-key   → E3/DC RSCP credentials (same way;
                                                                 inner spaces are kept – passwords may contain them)
    python -m solar_visor_hub.cli rscp-config [--enable|--disable] [--host IP] [--port N]
                                             [--power-interval S] [--system-interval S]   → E3/DC RSCP settings
                                                                 (shown without credentials; the running hub picks
                                                                 them up within seconds)
    python -m solar_visor_hub.cli backup [--to DIR] [--keep N] [--with-cache]
    python -m solar_visor_hub.cli restore <archive> [--force]  → verifies first; service must be stopped
    python -m solar_visor_hub.cli plant import-legacy          → plant.json from the mapping in use (once)
    python -m solar_visor_hub.cli plant export                 → the plant as JSON (no secrets in it)
    python -m solar_visor_hub.cli plant validate <file>        → all problems of a plant file
    python -m solar_visor_hub.cli plant import <file>          → validate and activate (next revision)
    python -m solar_visor_hub.cli history probe [entity …]     → read-only recorder measurements (JSON)
    python -m solar_visor_hub.cli history day <date …>         → generic day calculation straight from HA (JSON)
    python -m solar_visor_hub.cli history archive              → what the archive holds
    python -m solar_visor_hub.cli history recompute <date …>   → recompute archived days explicitly
Secrets are set on the hub host – exception: the RSCP credentials may also be set from the app through the LAN
admin port (user decision 2026-10-05).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import profile
from .config import ConfigError, store_secret
from .devices import DeviceStore
from .settings import Settings, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="solar-visor-hub")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_pair = sub.add_parser("pair")
    p_pair.add_argument("--scope", choices=("read", "admin"), default="read")
    p_pair.add_argument("--qr", action="store_true", help="also the pairing text and its QR code (like the interface)")
    p_pair.add_argument("--read-url", help="read address of the apps (default: as saved in the interface)")
    p_pair.add_argument("--admin-url", help="admin address of the apps (default: as saved in the interface)")
    sub.add_parser("devices")
    p_rev = sub.add_parser("revoke")
    p_rev.add_argument("device_id", type=int)
    p_sec = sub.add_parser("set-secret")
    p_sec.add_argument("name", choices=("ha", "influx", "rscp-user", "rscp-password", "rscp-key"))
    p_rscp = sub.add_parser("rscp-config")
    on_off = p_rscp.add_mutually_exclusive_group()
    on_off.add_argument("--enable", dest="enabled", action="store_const", const=True)
    on_off.add_argument("--disable", dest="enabled", action="store_const", const=False)
    p_rscp.add_argument("--host")
    p_rscp.add_argument("--port", type=int)
    p_rscp.add_argument("--power-interval", type=int, dest="power_interval_s")
    p_rscp.add_argument("--system-interval", type=int, dest="system_interval_s")
    p_bak = sub.add_parser("backup")
    p_bak.add_argument("--to", type=Path, default=Path("/var/backups/solar-visor"))
    p_bak.add_argument("--keep", type=int, default=14)
    p_bak.add_argument("--with-cache", action="store_true")
    p_res = sub.add_parser("restore")
    p_res.add_argument("archive", type=Path)
    p_res.add_argument("--force", action="store_true")
    p_plant = sub.add_parser("plant")
    plant_sub = p_plant.add_subparsers(dest="plant_cmd", required=True)
    plant_sub.add_parser("import-legacy")
    plant_sub.add_parser("export")
    plant_sub.add_parser("validate").add_argument("file", type=Path)
    plant_sub.add_parser("import").add_argument("file", type=Path)
    p_history = sub.add_parser("history")
    history_sub = p_history.add_subparsers(dest="history_cmd", required=True)
    history_sub.add_parser("probe").add_argument("entities", nargs="*")
    p_day = history_sub.add_parser("day")
    p_day.add_argument("dates", nargs="+")
    p_day.add_argument("--tier", choices=("raw", "hour", "auto"), default="auto")
    history_sub.add_parser("archive")
    history_sub.add_parser("recompute").add_argument("dates", nargs="+")
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.cmd == "plant":
        return plant_command(args, settings)
    if args.cmd == "history":
        return history_command(args, settings)
    if args.cmd == "rscp-config":
        from .rscp import RscpConfigStore
        st = RscpConfigStore(settings.data_dir / "rscp.json")
        changes = {k: getattr(args, k) for k in ("enabled", "host", "port", "power_interval_s", "system_interval_s")
                   if getattr(args, k) is not None}
        try:
            cfg = st.update(**changes) if changes else st.current
        except ConfigError as exc:
            print(f"abgelehnt: {exc}", file=sys.stderr)
            return 2
        s = settings.secrets
        have = {n: bool(p and p.exists() and p.stat().st_size > 0) for n, p in
                (("Benutzer", s.rscp_user), ("Passwort", s.rscp_password), ("RSCP-Schlüssel", s.rscp_key))}
        print(f"RSCP {'aktiv' if cfg.enabled else 'inaktiv'} · {cfg.host}:{cfg.port} · Leistungsdaten {cfg.power_interval_s} s"
              f" · Systemdaten {cfg.system_interval_s} s · " + ", ".join(f"{n} {'gesetzt' if v else 'fehlt'}" for n, v in have.items()))
        return 0
    if args.cmd in ("backup", "restore"):
        from . import backup
        try:
            if args.cmd == "backup":
                print(f"Sicherung: {backup.create(settings.data_dir, args.to, keep=args.keep, with_cache=args.with_cache)}")
            else:
                files = backup.restore(args.archive, settings.data_dir, force=args.force)
                print(f"wiederhergestellt: {', '.join(files)} – Dienst jetzt starten")
        except backup.BackupError as exc:
            print(f"abgebrochen: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "set-secret":
        s = settings.secrets
        target = {"ha": s.ha_token, "influx": s.influx_token, "rscp-user": s.rscp_user,
                  "rscp-password": s.rscp_password, "rscp-key": s.rscp_key}[args.name]
        assert target is not None
        if args.name.startswith("rscp-"):
            value = (getpass.getpass(f"{args.name}: ") if sys.stdin.isatty() else sys.stdin.readline()).rstrip("\r\n")
            if args.name == "rscp-key" and len(value.encode("utf-8")) > 32:
                print("abgelehnt: RSCP-Schlüssel länger als 32 Bytes", file=sys.stderr)
                return 2
            target.parent.mkdir(mode=0o700, exist_ok=True)
            return write_secret(target, value, allow_spaces=True)
        value = getpass.getpass("Token: ") if sys.stdin.isatty() else sys.stdin.readline()
        return write_secret(target, value.strip())

    store = DeviceStore(settings.devices_db)
    if args.cmd == "pair":
        code = store.create_pairing_code(args.scope)
        print(f"Kopplungscode ({args.scope}, 10 min gültig): {code}")
        if args.qr:
            from .ui import Addresses, UiSettings, pairing_payload
            saved = UiSettings(settings.data_dir / "ui.json").addresses()
            try:
                addresses = Addresses(read_url=args.read_url or saved.read_url, admin_url=args.admin_url or saved.admin_url)
            except ValueError:
                print("ungültige Adresse (http:// oder https:// erwartet)", file=sys.stderr)
                return 2
            payload = pairing_payload(addresses, code)
            if payload is None:
                print("Für den Kopplungstext fehlen die Adressen (--read-url/--admin-url oder in der Oberfläche).",
                      file=sys.stderr)
                return 2
            import segno
            print(payload)
            segno.make(payload, error="m").terminal(compact=True)
    elif args.cmd == "devices":
        for d in store.list_devices():
            print(f"{d.device_id:3}  {d.name:24} {d.scope:5}  erstellt {d.created_at}  zuletzt {d.last_used_at or '-'}"
                  f"{'  WIDERRUFEN' if d.revoked else ''}")
    elif args.cmd == "revoke":
        print("widerrufen" if store.revoke(args.device_id) else "nicht gefunden")
    return 0


def plant_command(args: argparse.Namespace, settings: Settings) -> int:
    """Plant model from the command line (same functions as the web interface will use, plan ha-addon 4.8)."""
    import json

    from .config import load_mapping
    from .mapping_store import MappingStore
    from .plant import PlantError, PlantStore, RevisionConflict, dump, parse
    from .plant_legacy import LEGACY_PROFILE, import_legacy, legacy_rules

    def problems(exc: PlantError) -> int:
        for p in exc.problems:
            print(f"{p.path}: {p.message}", file=sys.stderr)
        return 2

    def read(path: Path) -> object:
        return json.loads(path.read_text(encoding="utf-8"))

    try:
        if args.plant_cmd == "validate":
            parse(read(args.file))
            print("gültig")
            return 0
        store = PlantStore(settings.plant_path, rules=(legacy_rules,))
        if args.plant_cmd == "export":
            if store.current is None:
                print("keine Anlage eingerichtet", file=sys.stderr)
                return 2
            print(json.dumps(dump(store.current), indent=2, ensure_ascii=False))
            return 0
        if args.plant_cmd == "import-legacy":
            if not profile.AVAILABLE:                       # fail closed (plan hub-publish P4)
                print("diese Installation enthält kein Anlagenprofil für die Bestandsrechnung", file=sys.stderr)
                return 2
            if store.current is not None:
                print("abgebrochen: es gibt bereits eine Anlage (plant.json)", file=sys.stderr)
                return 2
            effective = MappingStore(load_mapping(), settings.mapping_overrides).current
            # the one place that may set the legacy profile: a store without the activation rules
            plant = PlantStore(settings.plant_path).activate(import_legacy(effective), expected_revision=0)
            assert plant.profile == LEGACY_PROFILE
            print(f"Anlage aus der bisherigen Zuordnung übernommen ({len(plant.points())} Datenpunkte, Revision "
                  f"{plant.revision}). Wirksam nach dem Neustart des Hubs.")
            return 0
        candidate = read(args.file)
        # the revision in the file is the one the edit started from (plant export): a change made meanwhile is a
        # conflict, not something to overwrite
        started_from = candidate.get("revision") if isinstance(candidate, dict) else None
        if isinstance(started_from, bool) or not isinstance(started_from, int):
            print("abgebrochen: die Datei nennt keine Revision (aus 'plant export' übernehmen, bei einer neuen Anlage 0)",
                  file=sys.stderr)
            return 2
        plant = store.activate(candidate, expected_revision=started_from)
        print(f"Anlage übernommen (Revision {plant.revision}). Wirksam nach dem Neustart des Hubs.")
        return 0
    except PlantError as exc:
        return problems(exc)
    except RevisionConflict as exc:
        print(f"abgebrochen: die Anlage wurde inzwischen geändert (aktiv ist Revision {exc.current}, die Datei geht "
              f"von {exc.expected} aus) – neu exportieren und die Änderung dort wiederholen", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"abgebrochen: {exc}", file=sys.stderr)
        return 2


def history_command(args: argparse.Namespace, settings: Settings) -> int:
    """Read-only measurements against Home Assistant's recorder (P3 detail plan §9); prints JSON.

    ``probe`` and ``day`` open a short-lived connection of their own (same allowlist): a diagnostic tool beside the
    hub, outside its one-request-at-a-time rule – meant for setup and acceptance, not for regular use (Codex P3 H5)."""
    import asyncio
    import json

    from .config import load_mapping
    from .ha_client import websocket_url
    from .ha_history import HaConnection, probe
    from .plant import PlantStore

    if args.history_cmd == "day":
        return history_day(args, settings)
    if args.history_cmd in ("archive", "recompute"):
        from datetime import date

        from .plant_archive import PlantArchive
        archive = PlantArchive(settings.data_dir / "archive.sqlite3")
        if args.history_cmd == "archive":
            print(json.dumps(archive.summary(), indent=2))
            return 0
        n = archive.mark_stale([date.fromisoformat(t) for t in args.dates])
        print(f"{n} Tag(e) zur Neuberechnung markiert – ältere Tage kommen danach nur noch aus Stundenwerten")
        return 0
    entities = list(args.entities)
    if not entities:
        plant = PlantStore(settings.plant_path).current
        if plant is not None:
            pts = plant.points()
            entities = [pts[k].entity for k in ("pv_1.power", "grid.power") if k in pts]
        elif profile.AVAILABLE:
            m = load_mapping().metrics
            entities = [m["pv_internal"].entity, m["grid_power"].entity]
        else:
            print("keine Anlage eingerichtet und keine Entitäten angegeben", file=sys.stderr)
            return 2

    async def run() -> dict:
        async with HaConnection(settings.ha_ws_url or websocket_url(settings.ha_url), settings.read_ha_token) as ha:
            return await probe(ha.read, entities)

    try:
        print(json.dumps(asyncio.run(run()), indent=2, ensure_ascii=False))
    except (OSError, ConnectionError, ValueError, RuntimeError) as exc:
        print(f"abgebrochen: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


def history_day(args: argparse.Namespace, settings: Settings) -> int:
    """Generic day calculation straight from Home Assistant (no cache, no archive) – for comparisons; prints JSON
    with the energy per category and day."""
    import asyncio
    import json
    from datetime import UTC, date, datetime, timedelta

    from .config import load_mapping
    from .daycalc import coverage
    from .ha_client import websocket_url
    from .ha_history import HaConnection, statistic_ids
    from .periods import period_bounds, quarter_hours, zone
    from .plant import PlantStore, parse
    from .plant_daycalc import TIER_HOUR, TIER_RAW, compute_day
    from .plant_history_source import read_day
    from .plant_legacy import import_legacy
    from .sun import night_intervals

    plant = PlantStore(settings.plant_path).current
    if plant is None:
        if not profile.AVAILABLE:
            print("keine Anlage eingerichtet", file=sys.stderr)
            return 2
        plant = parse(import_legacy(load_mapping()))

    async def run() -> dict:
        out: dict = {}
        async with HaConnection(settings.ha_ws_url or websocket_url(settings.ha_url), settings.read_ha_token) as ha:
            config = await ha.read({"type": "get_config"})
            tz = zone((config or {}).get("time_zone"))
            lat, lon = (config or {}).get("latitude"), (config or {}).get("longitude")
            states = await ha.read({"type": "get_states"})
            wanted = set(plant.entities())
            units = {s["entity_id"]: (s.get("attributes") or {}).get("unit_of_measurement")
                     for s in states or [] if s.get("entity_id") in wanted}
            meta = await statistic_ids(ha.read)
            now = datetime.now(UTC)
            for text in args.dates:
                d = date.fromisoformat(text)
                start, day_end = period_bounds("day", d, tz)
                end = min(day_end, now)
                tier = args.tier if args.tier != "auto" else (TIER_RAW if start >= now - timedelta(days=13) else TIER_HOUR)
                data = await read_day(plant, start, end, tier, ha.read, meta, units)
                nights = night_intervals(start, end, lat=lat, lon=lon) if lat is not None and lon is not None else None
                day = compute_day(plant, quarter_hours(start, day_end, tz), start, end, tier, data, nights=nights)
                out[text] = {"tier": tier, "categories": {
                    cat: {"wh": round(sum(e.wh or 0.0 for e in slots if e.wh is not None), 1)
                          if any(e.wh is not None for e in slots) else None,
                          "coverage": round(coverage(slots), 4),
                          "source": day.sources[cat].method if cat in day.sources else None}
                    for cat, slots in sorted(day.categories.items())}}
        return out

    try:
        print(json.dumps(asyncio.run(run()), indent=2, ensure_ascii=False))
    except (OSError, ConnectionError, ValueError, RuntimeError) as exc:
        print(f"abgebrochen: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


def write_secret(target: Path, value: str, allow_spaces: bool = False) -> int:
    try:
        store_secret(target, value, allow_spaces)
    except ConfigError:
        print("abgelehnt: leerer oder ungültiger Wert", file=sys.stderr)
        return 2
    print(f"{target.name} gespeichert (Inhalt nicht angezeigt). Wirksam bei der nächsten HA-Verbindung bzw. nach "
          "'systemctl restart solar-visor-hub'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
