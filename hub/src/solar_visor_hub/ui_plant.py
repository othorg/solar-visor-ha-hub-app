"""Plant editing of the web interface (P4 detail plan §2, §3): draft, check, activation, restore.

- The draft lives in ``plant.draft.json`` next to the plant; every save gets a new random id. A writer names the id it
  started from – another tab's save in between is a conflict (409), never overwritten.
- Activation names **both** the active revision and the draft id it was checked with; both are checked under one
  lock (Codex review of the P4 plan). Before anything is written the candidate is validated, the legacy rules apply and
  the runtime is built on trial; the plant replaced is kept in ``plant.prev.json``.
- The new plant takes effect with a restart of the hub process (``request_restart``), after the answer has been sent.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .periods import period_bounds, zone
from .plant import Plant, PlantError, PlantStore, Problem, RevisionConflict, dump, parse
from .plant_archive import PlantArchive
from .plant_legacy import legacy_rules
from .plant_live import PlantLive
from .runtime import choose_mode

log = logging.getLogger("solar_visor_hub.ui")

# every problem code the plant model, the legacy rules and the activation report – the interface translates them
# (contracts/ui_codes.json; a test compares this list with the sources)
PROBLEM_CODES = (
    "domain_mismatch", "entity_counted_twice", "invalid_balance", "invalid_entity", "invalid_flag",
    "invalid_freshness_from", "invalid_name", "invalid_profile", "invalid_pv_peak", "invalid_revision", "invalid_role",
    "invalid_rscp_pv",
    "invalid_sign", "invalid_stale_after", "invalid_string_id", "invalid_strings", "invalid_trust", "invalid_valid_from",
    "invalid_variant", "legacy_not_representable", "legacy_profile_unavailable", "module_required", "name_missing", "not_an_object", "point_missing",
    "point_not_in_variant", "profile_reserved", "runtime_failed", "trust_needs_counter", "unit_mismatch",
    "unknown_field", "unknown_module", "unknown_point", "unsupported_schema", "retroactive_invalid",
)


class ProblemView(BaseModel):
    path: str
    code: str
    message: str


class DraftView(BaseModel):
    id: str
    base_revision: int
    saved_at: datetime
    plant: dict[str, Any]


class PlantView(BaseModel):
    mode: str
    active: dict[str, Any] | None
    active_revision: int                 # stored (0: none yet)
    running_revision: int | None         # what the process runs – differs until the restart
    draft: DraftView | None
    problems: list[ProblemView]          # of the draft, else of the active plant
    has_previous: bool
    startup_error: str | None


class DraftIn(BaseModel):
    plant: dict[str, Any]
    draft_id: str | None = None          # the draft this edit started from (None: there was none)


class DraftSaved(BaseModel):
    draft_id: str
    problems: list[ProblemView]


class CandidateIn(BaseModel):
    plant: dict[str, Any]


class Checked(BaseModel):
    problems: list[ProblemView]


class Retroactive(BaseModel):
    metric_id: str
    from_: date = Field(alias="from")          # local day from which the new sensor applies


class ActivateIn(BaseModel):
    active_revision: int
    draft_id: str
    retroactive: list[Retroactive] = []


class RestoreIn(BaseModel):
    active_revision: int


class Activated(BaseModel):
    revision: int
    restarting: bool


def problems_of(raw: Any) -> list[ProblemView]:
    try:
        parse(raw)
    except PlantError as exc:
        return [ProblemView(path=p.path, code=p.code, message=p.message) for p in exc.problems]
    return []


def trial_build(plant: Plant) -> None:
    """The runtime parts a start builds from the plant, without starting anything (no session, no files)."""
    try:
        mode = choose_mode(plant)
        PlantLive(plant)
        _ = mode.projection
    except PlantError:
        raise
    except Exception as exc:   # noqa: BLE001 – reported as a problem of the candidate, the old plant stays
        raise PlantError([Problem("", "runtime_failed", f"Laufzeit lässt sich nicht aufbauen ({type(exc).__name__})")]) \
            from exc


class DraftStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()

    def get(self) -> DraftView | None:
        try:
            return DraftView.model_validate(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as exc:
            log.warning("draft unreadable (%s) – ignored", type(exc).__name__)
            return None

    def _check(self, expected: str | None) -> DraftView | None:
        current = self.get()
        if (current.id if current else None) != expected:
            raise HTTPException(status_code=409, detail={"code": "draft_changed",
                                                         "draft_id": current.id if current else None})
        return current

    def save(self, plant: dict[str, Any], expected: str | None, base_revision: int) -> DraftView:
        with self.lock:
            self._check(expected)
            draft = DraftView(id=secrets.token_hex(8), base_revision=base_revision, saved_at=datetime.now(UTC),
                              plant=plant)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            tmp.write_text(draft.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            return draft

    def delete(self, expected: str | None) -> None:
        with self.lock:
            self._check(expected)
            self.path.unlink(missing_ok=True)


@dataclass
class PlantEditing:
    plant_path: Path
    mode: str
    running_revision: Callable[[], int | None]
    request_restart: Callable[[], None]
    startup_error: str | None = None
    archive_path: Path | None = None          # generic history (mode plant): source overrides for retroactive changes
    timezone: Callable[[], str | None] = lambda: None

    @property
    def draft_path(self) -> Path:
        return self.plant_path.with_name("plant.draft.json")

    @property
    def previous_path(self) -> Path:
        return self.plant_path.with_name("plant.prev.json")


def register(app: FastAPI, ed: PlantEditing) -> None:
    drafts = DraftStore(ed.draft_path)

    def store() -> PlantStore:
        return PlantStore(ed.plant_path, rules=(legacy_rules,), tolerant=True)

    def archive() -> PlantArchive | None:
        return PlantArchive(ed.archive_path) if ed.archive_path is not None and ed.archive_path.exists() else None

    def archive_revision() -> int:
        a = archive()
        return a.max_revision() if a is not None else 0

    def stored() -> tuple[dict[str, Any] | None, int]:
        """The active plant as stored and its revision – an unreadable file is no plant, but keeps its number
        (the repair becomes the next revision, never one the archive knows already)."""
        plant, revision, readable = store().state(archive_revision())
        return (dump(plant) if plant is not None else None), revision

    def today() -> date:
        return datetime.now(zone(ed.timezone())).date()          # the plant's calendar, not UTC (Codex P4 N6)

    @app.get("/api/plant", response_model=PlantView)
    def get_plant() -> PlantView:
        active, revision = stored()
        draft = drafts.get()
        return PlantView(mode=ed.mode, active=active, active_revision=revision, running_revision=ed.running_revision(),
                         draft=draft, problems=problems_of(draft.plant if draft else active) if (draft or active) else [],
                         has_previous=ed.previous_path.exists(), startup_error=ed.startup_error)

    @app.put("/api/plant/draft", response_model=DraftSaved)
    def save_draft(body: DraftIn) -> DraftSaved:
        draft = drafts.save(body.plant, body.draft_id, stored()[1])
        return DraftSaved(draft_id=draft.id, problems=problems_of(body.plant))

    @app.delete("/api/plant/draft", status_code=204)
    def discard_draft(draft_id: str = Query()) -> None:
        drafts.delete(draft_id)

    @app.get("/api/plant/export")
    def export_plant() -> JSONResponse:
        """The active plant as file (``plant.json``) – no secrets in it, ever (credentials live apart)."""
        active, _ = stored()
        if active is None:
            raise HTTPException(status_code=404, detail={"code": "no_plant"})
        return JSONResponse(active, headers={"Content-Disposition": 'attachment; filename="solar-visor-plant.json"'})

    @app.post("/api/plant/import", response_model=DraftSaved)
    def import_plant(body: DraftIn) -> DraftSaved:
        """An exported plant becomes the draft – it is checked and activated like any edit, never directly. It has to
        be a plant at least in its form (an object of modules), else the interface could not show it (Codex P4 M4)."""
        if not isinstance(body.plant.get("modules"), dict):
            raise HTTPException(status_code=422, detail={"code": "not_a_plant"})
        plant = {k: v for k, v in body.plant.items() if k != "revision"}
        draft = drafts.save(plant, body.draft_id, stored()[1])
        return DraftSaved(draft_id=draft.id, problems=problems_of(plant))

    @app.post("/api/plant/validate", response_model=Checked)
    def validate(body: CandidateIn) -> Checked:
        return Checked(problems=problems_of(body.plant))

    def retro_check(retro: list[Retroactive], written: list[int]) -> Callable[[Plant], None]:
        """Runs under the plant lock before the plant is written: trial build; a retroactive change needs the generic
        history, a really changed sensor, a past day and must keep every archived revision valid. Its overrides and the
        recomputation are written here in one transaction – the plant follows; if that fails they are withdrawn."""
        def check(plant: Plant) -> None:
            trial_build(plant)
            if not retro:
                return
            current = store().current
            arch = archive()
            raw = dump(plant)
            problems = []

            def refuse(metric_id: str) -> None:
                path = "modules." + metric_id.replace(".", ".points.", 1) + ".entity"
                problems.append(Problem(path, "retroactive_invalid",
                                        "rückwirkend nur für einen geänderten Sensor einer Anlage mit eigener Historie"))
            for r in retro:
                new, old = plant.points().get(r.metric_id), current.points().get(r.metric_id) if current else None
                if (ed.mode != "plant" or arch is None or r.metric_id.count(".") != 1 or new is None or old is None
                        or new.entity == old.entity or r.from_ >= today()):
                    refuse(r.metric_id)
            if not problems and arch is not None:
                points = [(r.metric_id, raw["modules"][r.metric_id.split(".")[0]]["points"][r.metric_id.split(".")[1]])
                          for r in retro]
                for metric_id in arch.check_overrides(points):
                    refuse(metric_id)            # e.g. the sensor was part of the balance elsewhere in an old revision
            if problems:
                raise PlantError(problems)
            assert arch is not None
            tz = zone(ed.timezone())
            days = arch.add_overrides([(m, pt, period_bounds("day", r.from_, tz)[0]) for (m, pt), r in zip(points, retro)],
                                      plant.revision, datetime.now(UTC), min(r.from_ for r in retro))
            written.append(plant.revision)
            log.info("%d retroactive sensor change(s); %d archived day(s) to be computed anew", len(retro), days)
        return check

    def activate(candidate: dict[str, Any], expected: int, retro: list[Retroactive] | None = None) -> Activated:
        written: list[int] = []
        try:
            plant = store().activate(candidate, expected, keep_previous=ed.previous_path,
                                     check=retro_check(retro or [], written), repair=True,
                                     min_revision=archive_revision())
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "plant_changed", "active_revision": exc.current}) \
                from None
        except PlantError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid", "problems": [
                ProblemView(path=p.path, code=p.code, message=p.message).model_dump() for p in exc.problems]}) from None
        except BaseException:
            withdraw(written)                    # the plant was not written: its overrides must not stay
            raise
        log.info("plant revision %s activated from the interface – restarting", plant.revision)
        ed.request_restart()
        return Activated(revision=plant.revision, restarting=True)

    def withdraw(revisions: list[int]) -> None:
        arch = archive()
        if arch is None:
            return
        tz = zone(ed.timezone())
        for revision in revisions:
            days = arch.withdraw_overrides(revision, lambda t: t.astimezone(tz).date())
            if days:
                log.info("source overrides of revision %s withdrawn; %d archived day(s) to be computed anew", revision, days)

    @app.post("/api/plant/activate", response_model=Activated)
    def activate_draft(body: ActivateIn) -> Activated:
        with drafts.lock:
            draft = drafts._check(body.draft_id)
            assert draft is not None
            result = activate(draft.plant, body.active_revision, body.retroactive)
            drafts.path.unlink(missing_ok=True)
            return result

    @app.post("/api/plant/restore", response_model=Activated)
    def restore_previous(body: RestoreIn) -> Activated:
        try:
            previous = json.loads(ed.previous_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail={"code": "no_previous"}) from None
        except (ValueError, OSError):
            raise HTTPException(status_code=422, detail={"code": "previous_unreadable"}) from None
        if isinstance(previous, dict):
            previous = {**previous, "revision": 0}       # becomes the next revision; the number is the store's
        with drafts.lock:
            result = activate(previous, body.active_revision)
            # back to how it was: the retroactive sensor changes of the plant just replaced go as well (Codex P4 M6)
            withdraw([body.active_revision])
            return result

