#!/usr/bin/env python3
"""Project TITAN — Module 2: edge telemetry simulator (no hardware needed).

Pretends to be the sensor box inside a cold-chain crate: drives it along a GPS route between two
bases, emits temperature / humidity / battery / LDR-tamper readings, injects faults, and buffers
readings while "offline" before burst-syncing them. Pure standard library.

    python edge_simulator.py                                  # crate 1, ALPHA -> BRAVO, clean trip
    python edge_simulator.py --scenario thermal               # cooling failure -> lockout
    python edge_simulator.py --scenario tamper                # LDR light-ingress -> lockout
    python edge_simulator.py --scenario offline               # loses signal mid-route, burst-syncs later
    python edge_simulator.py --scenario offline_thermal       # excursion happens WHILE offline
    python edge_simulator.py --list                           # all scenarios / routes

Re-running on the same crate: the simulator reads the crate's last_seen_at and fits its device clock
after it. A crate that already arrived stays at the destination, so use a fresh crate or
`./setup.sh --reset` to replay a whole trip.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

# Same coordinates as seed.py
BASES = {
    "ALPHA": (-15.4167, 28.2833),
    "BRAVO": (-14.4469, 28.4464),
    "CHARLIE": (-15.7667, 28.1833),
}
SCENARIOS = {
    "normal": "clean trip, temperature stays 2-8 C",
    "thermal": "cooling failure: temperature ramps above 8 C mid-route",
    "freeze": "over-cooling: temperature ramps below 2 C mid-route",
    "tamper": "LDR light sensor trips for 3 readings (crate opened)",
    "mkt": "temperatures look fine but the device reports an MKT breach (8.6 C)",
    "offline": "signal lost for a stretch; readings buffered, then burst-synced",
    "offline_thermal": "signal lost AND the excursion happens inside the dead zone",
}
Transport = Callable[..., "tuple[int, dict]"]


# ------------------------------------------------------------------ small helpers
class NetError(Exception):
    """Network failure or 5xx: the reading should be buffered and retried."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_http_transport(base_url: str, timeout: float = 10.0, insecure: bool = False) -> Transport:
    base = base_url.rstrip("/")
    ctx = ssl._create_unverified_context() if insecure else None  # self-signed dev certificates

    def call(method: str, path: str, body: Optional[dict] = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except ValueError:
                payload = {}
            if e.code >= 500:
                raise NetError(f"server error {e.code}") from e
            return e.code, payload
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise NetError(str(e)) from e

    return call


class Out:
    """Tiny ANSI colour helper (disabled when not a TTY)."""

    def __init__(self, enabled: bool):
        self.on = enabled

    def c(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.on else s

    red = lambda self, s: self.c("31;1", s)      # noqa: E731
    green = lambda self, s: self.c("32", s)      # noqa: E731
    yellow = lambda self, s: self.c("33", s)     # noqa: E731
    cyan = lambda self, s: self.c("36", s)       # noqa: E731
    dim = lambda self, s: self.c("2", s)         # noqa: E731


# ------------------------------------------------------------------ physics of the fake device
def build_route(origin: tuple, dest: tuple, ticks: int, rng: random.Random, noise_m: float = 4.0) -> list[tuple]:
    """GPS waypoints: dwell at origin, drive a gently curved road, dwell at destination.
    Returns (lat, lng, phase) per tick. The last travel point lands on the destination."""
    dwell = max(2, ticks // 15)
    travel = max(1, ticks - 2 * dwell)
    deg = noise_m / 111_000.0  # GPS jitter in degrees
    (o_lat, o_lng), (d_lat, d_lng) = origin, dest
    dlat, dlng = d_lat - o_lat, d_lng - o_lng
    norm = math.hypot(dlat, dlng) or 1.0
    perp_lat, perp_lng = -dlng / norm, dlat / norm
    bend = 0.06 * norm  # road curvature: bows out ~6% of trip length
    pts = []
    for i in range(ticks):
        if i < dwell:
            t, phase = 0.0, "DEPOT"
        elif i >= dwell + travel:
            t, phase = 1.0, "ARRIVED"
        else:
            t, phase = (i - dwell + 1) / travel, "ROUTE"
        off = bend * math.sin(math.pi * t)
        pts.append((o_lat + dlat * t + perp_lat * off + rng.gauss(0, deg),
                    o_lng + dlng * t + perp_lng * off + rng.gauss(0, deg), phase))
    return pts


def temperature(i: int, scenario: str, fault_at: int, rng: random.Random) -> float:
    base = 4.6 + 0.6 * math.sin(i / 7.0) + rng.gauss(0, 0.12)  # compressor duty cycle + sensor noise
    k = i - fault_at + 1
    if scenario in ("thermal", "offline_thermal") and k > 0:
        base += min(0.8 * k, 7.0)      # runs away past 8 C after a few readings
    elif scenario == "freeze" and k > 0:
        base -= min(0.8 * k, 7.0)      # drops below 2 C after a few readings
    return round(base, 2)


def tamper_flag(i: int, scenario: str, fault_at: int) -> bool:
    return scenario == "tamper" and fault_at <= i < fault_at + 3


def device_mkt(i: int, scenario: str, fault_at: int) -> Optional[float]:
    return 8.6 if scenario == "mkt" and i >= fault_at else None


@dataclass
class Plan:
    ticks: int
    scenario: str
    fault_at: int
    off_from: Optional[int]
    off_to: Optional[int]

    def offline(self, i: int) -> bool:
        return self.off_from is not None and self.off_to is not None and self.off_from <= i < self.off_to


def make_plan(ticks: int, scenario: str, fault_at: Optional[int], off_from: Optional[int], off_to: Optional[int]) -> Plan:
    if fault_at is None:
        fault_at = int(ticks * 0.4) if scenario == "offline_thermal" else ticks // 2
    if scenario.startswith("offline") and off_from is None:
        off_from, off_to = int(ticks * 0.35), int(ticks * 0.55)
    if off_from is not None and off_to is None:
        off_to = off_from + max(3, ticks // 5)
    if off_from is not None and off_to is not None and off_to <= off_from:
        off_to = off_from + 1
    return Plan(ticks, scenario, fault_at, off_from, off_to)


def plan_clock(ticks: int, step_s: float, last_seen: Optional[datetime]) -> tuple[Optional[datetime], float]:
    """Virtual device clock so a short run can look like an hour-long trip.
    Returns (start, effective_step_s) or (None, 0) for a live clock (stamp = send time)."""
    now = utcnow()
    if step_s <= 0:
        return None, 0.0
    start = now - timedelta(seconds=ticks * step_s)
    if last_seen is not None and start <= last_seen:
        start = last_seen + timedelta(seconds=1)  # never go backwards relative to what the server has seen
    span = (now - start).total_seconds()
    if span < ticks:  # no room left: fall back to live clock
        return None, 0.0
    return start, span / ticks


# ------------------------------------------------------------------ the run
@dataclass
class Stats:
    sent: int = 0
    buffered: int = 0
    synced: int = 0
    duplicates: int = 0
    rejected: int = 0
    lockouts: int = 0
    out_of_order: int = 0
    transitions: list = field(default_factory=list)


def parse_latlng(s: str) -> tuple:
    a, b = s.split(",")
    return float(a), float(b)


def lookup_crate(tx: Transport, crate_id: Optional[int], qr: Optional[str]) -> Optional[dict]:
    status, body = tx("GET", "/api/inventory?limit=500")
    if status != 200:
        return None
    for it in body.get("items", []):
        c = it["crate"]
        if (crate_id is not None and c["crate_id"] == crate_id) or (qr and c["qr_code_payload"] == qr):
            return c
    return None


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def run(args: argparse.Namespace, transport: Optional[Transport] = None, out: Optional[Out] = None) -> dict:
    out = out or Out(sys.stdout.isatty())
    say = lambda s="": print(s, flush=True)  # noqa: E731
    tx = transport or make_http_transport(args.api, insecure=getattr(args, 'insecure', False))
    rng = random.Random(args.seed)

    # ---- preflight ---------------------------------------------------------
    try:
        status, _ = tx("GET", "/api/health")
    except NetError as e:
        say(out.red(f"Cannot reach the TITAN API at {args.api}: {e}"))
        say("Is the server running?  ->  ./run.sh")
        raise SystemExit(1)
    crate = lookup_crate(tx, args.crate_id, args.qr)
    if crate is None:
        say(out.red("Crate not found in /api/inventory (it needs at least one medical item). Run ./setup.sh to seed."))
        raise SystemExit(1)
    crate_id, qr = crate["crate_id"], crate["qr_code_payload"]

    origin = parse_latlng(args.origin) if args.origin else BASES[args.route.split("-")[0].upper()]
    dest = parse_latlng(args.dest) if args.dest else BASES[args.route.split("-")[1].upper()]
    plan = make_plan(args.ticks, args.scenario, args.fault_at, args.offline_from, args.offline_to)
    route = build_route(origin, dest, args.ticks, rng)
    start, eff_step = plan_clock(args.ticks, args.sim_step, parse_iso(crate.get("last_seen_at")))
    device_id = args.device_id or f"EDGE-SIM-{crate_id:04d}"
    seq_base = int(time.time() * 1000)  # monotonic across runs so reruns are never mistaken for duplicates

    say(out.cyan(f"TITAN edge simulator — crate {crate_id} ({qr}) as {device_id}"))
    say(f"  route {args.route if not args.origin else 'custom'}  scenario={plan.scenario}  ticks={plan.ticks}"
        f"  clock={'live' if start is None else f'virtual {eff_step:.0f}s/tick'}")
    if plan.scenario != "normal":
        say(f"  fault at tick {plan.fault_at}" + (f"   offline ticks {plan.off_from}-{plan.off_to - 1}" if plan.off_from is not None else ""))
    say()

    st = Stats()
    buffer: list[tuple[int, dict]] = []
    locked_announced = bool(crate.get("is_locked_out"))
    last_movement = crate.get("movement_status")

    def describe(tick: int, p: dict, r: dict, prefix: str = "") -> None:
        nonlocal locked_announced, last_movement
        mv = r["movement_status"]
        line = (f"{prefix}tick {tick + 1:03d}/{plan.ticks}  {p['recorded_at'][11:19]}Z  "
                f"({p['lat']:.4f}, {p['lng']:.4f})  {p['temperature_c']:5.2f}°C  {mv}")
        if r.get("out_of_order"):
            st.out_of_order += 1
            line += out.yellow("  [out-of-order: older than server's last_seen]")
        say(line)
        if r["previous_movement_status"] != mv:
            st.transitions.append((tick + 1, r["previous_movement_status"], mv))
            say(out.cyan(f"      ► geofence: {r['previous_movement_status']} → {mv}"))
        for v in r.get("violations", []):
            say(out.red(f"      ✖ {v['detail']}"))
        if r["is_locked_out"] and not locked_announced:
            locked_announced = True
            st.lockouts += 1
            say(out.red(f"      ■ CRATE LOCKED OUT — {r['integrity_status']}: {r['lockout_reason']}"))
        last_movement = mv

    def flush() -> bool:
        """Burst-sync the buffer in chunks. Returns True if everything was delivered."""
        nonlocal buffer
        while buffer:
            chunk = buffer[:500]
            try:
                status, res = tx("POST", "/api/telemetry/ingest/batch", {"readings": [p for _, p in chunk]})
            except NetError:
                return False
            if status != 200:
                say(out.red(f"      batch rejected ({status}): {res}"))
                st.rejected += len(chunk)
                buffer = buffer[500:]
                continue
            st.synced += res["processed"] - res["duplicates"]
            st.duplicates += res["duplicates"]
            st.rejected += res["failed"]
            say(out.green(f"  ⇧ BURST SYNC: {len(chunk)} buffered readings → processed {res['processed']}, "
                          f"duplicates {res['duplicates']}, failed {res['failed']}"))
            for item in res["results"]:  # server returns them in device-time order
                tick, p = chunk[item["index"]]
                if not item["ok"]:
                    say(out.red(f"      tick {tick + 1:03d} rejected: {item['error']}"))
                elif not item["result"]["duplicate"]:
                    describe(tick, p, item["result"], prefix="    ↳ ")
            if args.resend_dupes:
                _, again = tx("POST", "/api/telemetry/ingest/batch", {"readings": [p for _, p in chunk]})
                st.duplicates += again["duplicates"]
                say(out.dim(f"      replayed same batch (retry safety check): {again['duplicates']}/{len(chunk)} ignored as duplicates"))
            buffer = buffer[500:]
        return True

    # ---- main loop ----------------------------------------------------------
    for i in range(plan.ticks):
        lat, lng, phase = route[i]
        when = utcnow() if start is None else start + timedelta(seconds=eff_step * i)
        payload = {
            "crate_id": crate_id, "device_id": device_id, "sequence": seq_base + i,
            "recorded_at": stamp(when), "lat": round(lat, 6), "lng": round(lng, 6),
            "temperature_c": temperature(i, plan.scenario, plan.fault_at, rng),
            "humidity_pct": round(max(5.0, min(95.0, 38 + rng.gauss(0, 1.5))), 1),
            "battery_pct": round(max(1.0, 100 - i * 0.12), 1),
            "ldr_tamper": tamper_flag(i, plan.scenario, plan.fault_at),
        }
        mkt = device_mkt(i, plan.scenario, plan.fault_at)
        if mkt is not None:
            payload["mkt_c"] = mkt

        if plan.offline(i):  # radio dead: store-and-forward
            buffer.append((i, payload))
            st.buffered += 1
            say(out.yellow(f"tick {i + 1:03d}/{plan.ticks}  {payload['recorded_at'][11:19]}Z  "
                           f"({payload['lat']:.4f}, {payload['lng']:.4f})  {payload['temperature_c']:5.2f}°C  "
                           f"OFFLINE — buffered ({len(buffer)} waiting)"))
        else:
            if buffer:
                buffer.append((i, payload))
                if not flush():
                    st.buffered += 1
                    say(out.yellow(f"tick {i + 1:03d}  link still down — {len(buffer)} readings waiting"))
            else:
                try:
                    status, res = tx("POST", "/api/telemetry/ingest", payload)
                    if status == 200:
                        st.sent += 1
                        describe(i, payload, res)
                    else:
                        st.rejected += 1
                        say(out.red(f"tick {i + 1:03d}  REJECTED {status}: {res.get('detail', res)}"))
                except NetError as e:  # real network failure -> behave like a real device
                    buffer.append((i, payload))
                    st.buffered += 1
                    say(out.yellow(f"tick {i + 1:03d}  network error ({e}) — buffered ({len(buffer)} waiting)"))
        if args.delay > 0 and i < plan.ticks - 1:
            time.sleep(args.delay)

    if buffer:  # trip over: try to deliver whatever is left
        say(out.yellow(f"\n  end of trip with {len(buffer)} undelivered readings — final sync attempt"))
        if not flush():
            say(out.red(f"  {len(buffer)} readings could not be delivered (API unreachable)"))

    # ---- verdict -------------------------------------------------------------
    summary = {"crate_id": crate_id, "stats": st, "verify": None}
    if not args.no_verify:
        try:
            status, v = tx("GET", f"/api/crates/{crate_id}/verify")
        except NetError:
            status, v = 0, {}
        if status == 200:
            summary["verify"] = v
            colour = out.green if v["safe"] else out.red
            say(f"\n{colour('━━ VERDICT: ' + v['verdict'] + ' ━━')}")
            for r in v["reasons"]:
                say(out.red(f"   • {r}"))
            for w in v["warnings"]:
                say(out.yellow(f"   • {w}"))
            say(f"   hash chain: {'valid' if v['chain']['valid'] else 'BROKEN'} ({v['chain']['length']} records)")
    say(f"\nsent {st.sent} live | buffered {st.buffered} | synced {st.synced} | duplicates ignored {st.duplicates} "
        f"| rejected {st.rejected} | lockouts {st.lockouts}")
    return summary


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="TITAN edge telemetry simulator",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8000", help="TITAN API base URL")
    ap.add_argument("--crate-id", type=int, help="crate to simulate (default 1 if --qr not given)")
    ap.add_argument("--qr", help="crate QR payload, e.g. TITAN-CRATE-0001")
    ap.add_argument("--device-id", help="default EDGE-SIM-<crate>")
    ap.add_argument("--route", default="ALPHA-BRAVO", help="ORIGIN-DEST using seeded bases: " + ", ".join(BASES))
    ap.add_argument("--origin", help="custom 'lat,lng' (overrides --route origin)")
    ap.add_argument("--dest", help="custom 'lat,lng' (overrides --route destination)")
    ap.add_argument("--scenario", default="normal", choices=list(SCENARIOS))
    ap.add_argument("--ticks", type=int, default=60, help="number of readings in the trip")
    ap.add_argument("--delay", type=float, default=0.4, help="real seconds between readings (0 = as fast as possible)")
    ap.add_argument("--sim-step", type=float, default=60.0,
                    help="device-clock seconds per reading (virtual clock; 0 = live clock using send time)")
    ap.add_argument("--fault-at", type=int, help="tick at which the fault starts (default: mid-trip)")
    ap.add_argument("--offline-from", type=int, help="first offline tick (any scenario)")
    ap.add_argument("--offline-to", type=int, help="first tick back online")
    ap.add_argument("--resend-dupes", action="store_true", help="replay each burst once more to prove idempotency")
    ap.add_argument("--insecure", action="store_true", help="accept self-signed HTTPS certificates")
    ap.add_argument("--no-verify", action="store_true", help="skip the final /verify call")
    ap.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible runs")
    ap.add_argument("--list", action="store_true", help="list scenarios and routes, then exit")
    a = ap.parse_args(argv)
    if a.list:
        print("Scenarios:")
        for k, v in SCENARIOS.items():
            print(f"  {k:<16}{v}")
        print("\nBases:", ", ".join(f"{k} {v}" for k, v in BASES.items()))
        raise SystemExit(0)
    if a.crate_id is None and not a.qr:
        a.crate_id = 1
    if a.ticks < 10:
        ap.error("--ticks must be at least 10")
    try:
        if not a.origin:
            assert a.route.split("-")[0].upper() in BASES
        if not a.dest:
            assert a.route.split("-")[1].upper() in BASES
    except (AssertionError, IndexError):
        ap.error(f"--route must look like ALPHA-BRAVO using {list(BASES)} (or give --origin/--dest)")
    return a


def main(argv=None, transport: Optional[Transport] = None) -> dict:
    try:
        return run(parse_args(argv), transport)
    except KeyboardInterrupt:
        print("\nstopped.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
