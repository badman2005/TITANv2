"""Demo control: lets the PWA launch edge_simulator.py runs (enabled when TITAN_DEMO=1, the default).

Each run is a child process of the API that talks to the API over HTTP exactly like a real edge device.
Arguments are validated and passed as an argv list (never through a shell). Turn this off with
TITAN_DEMO=0 for anything beyond a local demo.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .config import settings

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import edge_simulator as _sim  # noqa: E402  (scenario/base catalogue lives with the simulator)

router = APIRouter(prefix="/api/demo", tags=["demo"])
MAX_RUNNING = 3
_runs: dict[str, "Run"] = {}
_lock = threading.Lock()


class Run:
    def __init__(self, crate_id: int, scenario: str, route: str, argv: list[str]):
        self.id = uuid.uuid4().hex[:8]
        self.crate_id, self.scenario, self.route = crate_id, scenario, route
        self.lines: list[str] = []
        self.started_at = time.time()
        self.ended_at: Optional[float] = None
        self.returncode: Optional[int] = None
        self.stopped = False
        self.proc = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     bufsize=1, env={**__import__("os").environ, "PYTHONUNBUFFERED": "1"})
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))
        self.returncode = self.proc.wait()
        self.ended_at = time.time()

    @property
    def running(self) -> bool:
        return self.returncode is None

    def summary(self) -> dict:
        return {"id": self.id, "crate_id": self.crate_id, "scenario": self.scenario, "route": self.route,
                "running": self.running, "returncode": self.returncode, "stopped": self.stopped,
                "started_at": self.started_at, "ended_at": self.ended_at, "total_lines": len(self.lines)}


class SimRequest(BaseModel):
    crate_id: int = Field(..., ge=1)
    scenario: Literal["normal", "thermal", "freeze", "tamper", "mkt", "offline", "offline_thermal"] = "normal"
    route: str = Field("ALPHA-BRAVO", max_length=40)
    ticks: int = Field(60, ge=10, le=300)
    delay: float = Field(0.4, ge=0, le=5)
    offline_from: Optional[int] = Field(None, ge=0, le=299)
    offline_to: Optional[int] = Field(None, ge=1, le=300)


def _require_enabled() -> None:
    if not settings.demo_enabled:
        raise HTTPException(404, "Demo control is disabled (TITAN_DEMO=0)")


@router.get("/config")
def config():
    return {"enabled": settings.demo_enabled, "scenarios": _sim.SCENARIOS, "routes": [
        f"{a}-{b}" for a in _sim.BASES for b in _sim.BASES if a != b]}


@router.post("/runs")
def start_run(req: SimRequest):
    _require_enabled()
    try:
        o, d = req.route.upper().split("-")
        assert o in _sim.BASES and d in _sim.BASES and o != d
    except (ValueError, AssertionError):
        raise HTTPException(422, f"route must be ORIGIN-DEST using {list(_sim.BASES)}")
    with _lock:
        live = [r for r in _runs.values() if r.running]
        if any(r.crate_id == req.crate_id for r in live):
            raise HTTPException(409, f"A simulation is already running for crate {req.crate_id}")
        if len(live) >= MAX_RUNNING:
            raise HTTPException(409, f"At most {MAX_RUNNING} simulations can run at once")
        argv = [sys.executable, str(ROOT / "edge_simulator.py"), "--api", settings.self_url,
                "--crate-id", str(req.crate_id), "--scenario", req.scenario, "--route", f"{o}-{d}",
                "--ticks", str(req.ticks), "--delay", str(req.delay)]
        if req.offline_from is not None:
            argv += ["--offline-from", str(req.offline_from)]
            if req.offline_to is not None:
                argv += ["--offline-to", str(req.offline_to)]
        if settings.self_url.startswith("https"):
            argv.append("--insecure")
        run = Run(req.crate_id, req.scenario, f"{o}-{d}", argv)
        _runs[run.id] = run
    return run.summary()


@router.get("/runs")
def list_runs():
    _require_enabled()
    return sorted((r.summary() for r in _runs.values()), key=lambda r: -r["started_at"])


@router.get("/runs/{run_id}")
def get_run(run_id: str, since: int = 0):
    _require_enabled()
    run = _runs.get(run_id)
    if run is None:
        raise HTTPException(404, "Unknown run")
    return {**run.summary(), "lines": run.lines[max(0, since):]}


@router.post("/runs/{run_id}/stop")
def stop_run(run_id: str):
    _require_enabled()
    run = _runs.get(run_id)
    if run is None:
        raise HTTPException(404, "Unknown run")
    if run.running:
        run.stopped = True
        run.proc.terminate()
    return run.summary()


def stop_all() -> None:
    for r in _runs.values():
        if r.running:
            r.proc.terminate()
