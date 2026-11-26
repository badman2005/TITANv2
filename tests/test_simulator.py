import random

import pytest

import edge_simulator as sim
from app.geo import haversine_m


def tx_for(client):
    def call(method, path, body=None):
        r = client.request(method, path, json=body)
        return r.status_code, r.json()
    return call


def go(client, *argv, transport=None):
    return sim.main(["--delay", "0", "--seed", "1", "--ticks", "60", *argv], transport=transport or tx_for(client))


def moves(summary):
    return [(a, b) for _, a, b in summary["stats"].transitions]


# ---------------------------------------------------------------- pure functions
def test_route_starts_at_origin_and_ends_at_destination():
    pts = sim.build_route(sim.BASES["ALPHA"], sim.BASES["BRAVO"], 60, random.Random(1))
    assert len(pts) == 60
    assert haversine_m(pts[0][0], pts[0][1], *sim.BASES["ALPHA"]) < 50
    assert haversine_m(pts[-1][0], pts[-1][1], *sim.BASES["BRAVO"]) < 50
    assert pts[0][2] == "DEPOT" and pts[30][2] == "ROUTE" and pts[-1][2] == "ARRIVED"


def test_normal_temperature_never_leaves_the_band():
    rng = random.Random(7)
    assert all(2.0 < sim.temperature(i, "normal", 30, rng) < 8.0 for i in range(2000))


def test_fault_ramps_cross_the_limits():
    rng = random.Random(7)
    assert max(sim.temperature(i, "thermal", 20, rng) for i in range(20, 40)) > 8.0
    assert min(sim.temperature(i, "freeze", 20, rng) for i in range(20, 40)) < 2.0


def test_clock_never_goes_before_last_seen():
    from datetime import timedelta
    now = sim.utcnow()
    start, step = sim.plan_clock(30, 60, now - timedelta(minutes=10))
    assert start > now - timedelta(minutes=10) and step > 0
    assert sim.plan_clock(30, 60, now + timedelta(seconds=5)) == (None, 0.0)  # no room -> live clock
    assert sim.plan_clock(30, 0, None) == (None, 0.0)


def test_bad_route_is_rejected():
    with pytest.raises(SystemExit) as e:
        sim.parse_args(["--route", "ALPHA-MARS"])
    assert e.value.code == 2


# ---------------------------------------------------------------- scenarios, end to end
def test_normal_trip_completes_safely(client):
    s = go(client, "--scenario", "normal")
    assert s["verify"]["safe"] and s["stats"].lockouts == 0 and s["stats"].sent == 60
    assert moves(s) == [("AT_BASE", "IN_TRANSIT"), ("IN_TRANSIT", "AT_BATTLEFIELD")]
    arrived = client.get("/api/inventory", params={"movement_status": "AT_BATTLEFIELD"}).json()
    assert arrived["total"] == 2 and arrived["items"][0]["storage"]["base_code"] == "BRAVO"


@pytest.mark.parametrize("scenario,code", [("thermal", "THERMAL_EXCURSION"), ("freeze", "THERMAL_EXCURSION"),
                                           ("tamper", "TAMPER_DETECTED"), ("mkt", "MKT_BREACH")])
def test_fault_scenarios_lock_the_crate(client, scenario, code):
    s = go(client, "--scenario", scenario)
    assert s["stats"].lockouts == 1
    assert not s["verify"]["safe"] and s["verify"]["verdict"] == "COMPROMISED — DO NOT USE"
    crate = client.get("/api/inventory", params={"item_type": "VACCINE", "movement_status": "AT_BATTLEFIELD"}).json()["items"][0]
    assert crate["is_locked_out"] and code in crate["crate"]["lockout_reason"]
    assert crate["item_status"] == "QUARANTINED"


def test_offline_buffering_then_burst_sync_loses_nothing(client):
    s = go(client, "--scenario", "offline")
    st = s["stats"]
    assert st.buffered > 5 and st.rejected == 0 and st.out_of_order == 0
    assert st.sent + st.synced == 60  # every reading delivered exactly once
    assert s["verify"]["safe"]
    assert moves(s) == [("AT_BASE", "IN_TRANSIT"), ("IN_TRANSIT", "AT_BATTLEFIELD")]  # same story as an online trip


def test_excursion_inside_dead_zone_is_caught_at_sync(client):
    s = go(client, "--scenario", "offline_thermal")
    assert s["stats"].lockouts == 1 and not s["verify"]["safe"]
    assert any(r.startswith("COLD_CHAIN_BREACH") for r in s["verify"]["reasons"])


def test_replayed_burst_is_ignored(client):
    s = go(client, "--scenario", "offline", "--resend-dupes")
    st = s["stats"]
    # the burst = buffered readings + the reading from the tick when the link came back
    assert st.duplicates == st.synced > st.buffered > 0 and st.sent + st.synced == 60
    assert s["verify"]["chain"]["valid"]


def test_rerun_on_same_crate_is_accepted(client):
    go(client, "--scenario", "normal", "--ticks", "20")
    s = go(client, "--scenario", "normal", "--ticks", "20")
    assert s["stats"].rejected == 0 and s["stats"].out_of_order == 0 and s["stats"].sent == 20


def test_live_clock_mode(client):
    s = go(client, "--sim-step", "0", "--ticks", "15")
    assert s["stats"].sent == 15 and s["verify"]["safe"]


def test_network_outage_buffers_and_recovers(client):
    inner, calls = tx_for(client), {"n": 0}

    def flaky(method, path, body=None):
        if method == "POST" and path.startswith("/api/telemetry/ingest"):
            calls["n"] += 1
            if 5 <= calls["n"] < 10:
                raise sim.NetError("connection refused")
        return inner(method, path, body)

    s = go(client, "--scenario", "normal", transport=flaky)
    st = s["stats"]
    assert st.buffered >= 5 and st.rejected == 0 and st.sent + st.synced == 60
    assert s["verify"]["safe"]


def test_unreachable_api_exits_cleanly():
    def dead(*a, **k):
        raise sim.NetError("refused")
    with pytest.raises(SystemExit) as e:
        sim.main(["--delay", "0"], transport=dead)
    assert e.value.code == 1


def test_unknown_crate_exits_cleanly(client):
    with pytest.raises(SystemExit) as e:
        go(client, "--crate-id", "999")
    assert e.value.code == 1


def test_select_crate_by_qr(client):
    s = go(client, "--qr", "TITAN-CRATE-0002", "--route", "ALPHA-CHARLIE", "--ticks", "20")
    assert s["crate_id"] == 2 and s["stats"].rejected == 0
