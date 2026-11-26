from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.geo import haversine_m
from app.rules import mean_kinetic_temperature

ALPHA = (-15.4167, 28.2833)
BRAVO = (-14.4469, 28.4464)


def ts(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def reading(client, crate_id=1, minutes_ago=0, pos=ALPHA, temp=4.5, tamper=False, seq=None, device="EDGE-T"):
    body = {"crate_id": crate_id, "device_id": device, "recorded_at": ts(minutes_ago), "lat": pos[0], "lng": pos[1],
            "temperature_c": temp, "ldr_tamper": tamper}
    if seq is not None:
        body["sequence"] = seq
    return client.post("/api/telemetry/ingest", json=body)


def verify(client, crate_id):
    r = client.get(f"/api/crates/{crate_id}/verify")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- pure functions
def test_haversine_one_degree_longitude_at_equator():
    assert abs(haversine_m(0, 0, 0, 1) - 111_195) < 100


def test_mkt_of_constant_series_is_that_temperature():
    assert abs(mean_kinetic_temperature([5.0] * 10) - 5.0) < 1e-6
    assert mean_kinetic_temperature([2, 8]) > 5.0  # MKT weights the warm excursions


# ---------------------------------------------------------------- inventory
def test_inventory_in_transit_has_vehicle_driver_and_live_coordinates(client):
    r = client.get("/api/inventory", params={"movement_status": "IN_TRANSIT"}).json()
    assert r["total"] == 2
    for it in r["items"]:
        t = it["transit"]
        assert t["vehicle_callsign"] == "TITAN-12" and t["driver_name"] == "Cpl. T. Mwansa"
        assert t["dispatched_at"] and t["latitude"] and t["longitude"] and it["storage"] is None


def test_inventory_filters(client):
    assert [i["item_name"] for i in client.get("/api/inventory", params={"expiry_status": "EXPIRED"}).json()["items"]] \
        == ["Tetanus Toxoid Vaccine"]
    soon = client.get("/api/inventory", params={"expiry_status": "EXPIRING_SOON"}).json()["items"]
    assert [i["item_name"] for i in soon] == ["Rabies Immunoglobulin"]
    blood = client.get("/api/inventory", params={"item_type": "BLOOD_PRODUCT"}).json()["items"]
    assert len(blood) == 1 and blood[0]["crate"]["movement_status"] == "IN_TRANSIT"
    bravo = client.get("/api/inventory", params={"base_code": "bravo"}).json()["items"]
    assert len(bravo) == 2 and all(i["storage"]["base_code"] == "BRAVO" for i in bravo)
    inbound = client.get("/api/inventory", params={"base_code": "CHARLIE", "movement_status": "IN_TRANSIT"}).json()
    assert inbound["total"] == 2  # crate 2 is inbound to CHARLIE
    assert client.get("/api/inventory", params={"movement_status": "BOGUS"}).status_code == 422


def test_locked_out_items_are_quarantined_and_not_preliminary_safe(client):
    items = client.get("/api/inventory", params={"base_code": "ALPHA", "movement_status": "IN_STORAGE"}).json()["items"]
    c4 = [i for i in items if i["crate"]["crate_id"] == 4]
    assert c4 and all(i["item_status"] == "QUARANTINED" and not i["preliminary_safe"] for i in c4)
    assert c4[0]["integrity_status"] == "COMPROMISED_THERMAL"


# ---------------------------------------------------------------- verify
def test_verify_safe_crate(client):
    v = verify(client, 1)
    assert v["verdict"] == "SAFE TO ADMINISTER" and v["safe"] and not v["reasons"]
    assert v["checks"]["hash_chain"] == "PASS" and v["chain"]["valid"]
    assert all(i["safe_to_administer"] for i in v["items"])


def test_verify_warns_but_passes_when_expiring_soon(client):
    v = verify(client, 3)
    assert v["safe"] and any(w.startswith("EXPIRING_SOON") for w in v["warnings"])


def test_verify_expired_item_is_compromised(client):
    v = verify(client, 5)
    assert v["verdict"] == "COMPROMISED — DO NOT USE"
    assert any(r.startswith("EXPIRED_ITEM") for r in v["reasons"])
    assert v["checks"]["cold_chain"] == "PASS"


def test_verify_seeded_thermal_lockout(client):
    v = verify(client, 4)
    assert not v["safe"]
    assert any(r.startswith("LOCKOUT") for r in v["reasons"]) and any(r.startswith("COLD_CHAIN_BREACH") for r in v["reasons"])


def test_verify_unknown_crate_404(client):
    assert client.get("/api/crates/999/verify").status_code == 404


def test_each_verification_is_chained(client):
    a, b = verify(client, 1), verify(client, 1)
    assert a["verification_audit_hash"] != b["verification_audit_hash"]
    assert b["chain"]["length"] == a["chain"]["length"] + 1


# ---------------------------------------------------------------- ingest rules
def test_thermal_excursion_locks_out_and_blocks_dispensing(client):
    r = reading(client, 1, temp=9.2).json()
    assert r["is_locked_out"] and r["integrity_status"] == "COMPROMISED_THERMAL"
    assert r["violations"][0]["code"] == "THERMAL_EXCURSION"
    assert verify(client, 1)["verdict"] == "COMPROMISED — DO NOT USE"
    # sticky: a later in-range reading does not clear the lockout
    assert reading(client, 1, temp=4.0).json()["is_locked_out"]


def test_too_cold_also_locks_out(client):
    assert reading(client, 1, temp=1.4).json()["integrity_status"] == "COMPROMISED_THERMAL"


def test_ldr_tamper_locks_out(client):
    r = reading(client, 1, tamper=True).json()
    assert r["integrity_status"] == "COMPROMISED_TAMPER" and r["is_locked_out"]
    v = verify(client, 1)
    assert any(x.startswith("TAMPER_HISTORY") for x in v["reasons"])


def test_device_reported_mkt_breach_locks_out(client):
    body = {"crate_id": 1, "device_id": "E", "recorded_at": ts(), "lat": ALPHA[0], "lng": ALPHA[1],
            "temperature_c": 5.0, "mkt_c": 8.6}
    r = client.post("/api/telemetry/ingest", json=body).json()
    assert r["is_locked_out"] and r["violations"][0]["code"] == "MKT_BREACH"


def test_validation_errors(client):
    assert client.post("/api/telemetry/ingest", json={"device_id": "x", "recorded_at": ts(), "lat": 0, "lng": 0,
                                                      "temperature_c": 4}).status_code == 422  # no crate identity
    assert reading(client, 1, pos=(95, 0)).status_code == 422
    assert reading(client, 1, minutes_ago=-60).status_code == 422  # future timestamp
    assert reading(client, 999).status_code == 404


def test_ingest_by_qr_payload(client):
    body = {"qr_code_payload": "TITAN-CRATE-0001", "device_id": "E", "recorded_at": ts(), "lat": ALPHA[0],
            "lng": ALPHA[1], "temperature_c": 4.4}
    assert client.post("/api/telemetry/ingest", json=body).json()["crate_id"] == 1


# ---------------------------------------------------------------- geofence state machine
def test_full_dispatch_lifecycle(client):
    r1 = reading(client, 1, minutes_ago=60, pos=(-15.20, 28.30)).json()  # ~24 km out
    assert r1["previous_movement_status"] == "AT_BASE" and r1["movement_status"] == "IN_TRANSIT"
    assert r1["distance_to_origin_m"] > 800
    assert r1["audit_records_appended"] == 3  # INGEST + GEOFENCE_TRANSITION + DISPATCH_DEPARTED

    transit = client.get("/api/inventory", params={"movement_status": "IN_TRANSIT"}).json()["items"]
    assert any(i["crate"]["crate_id"] == 1 and i["transit"]["vehicle_callsign"] == "TITAN-07"
               and i["transit"]["dispatched_at"] for i in transit)

    r2 = reading(client, 1, minutes_ago=5, pos=BRAVO).json()
    assert r2["movement_status"] == "AT_BATTLEFIELD" and r2["distance_to_destination_m"] < 10
    assert r2["audit_records_appended"] == 3  # INGEST + GEOFENCE_TRANSITION + BASE_HANDOFF
    arrived = client.get("/api/inventory", params={"movement_status": "AT_BATTLEFIELD"}).json()["items"]
    assert arrived and arrived[0]["storage"]["base_code"] == "BRAVO"
    assert verify(client, 1)["safe"]


def test_return_to_origin_geofence_is_at_base(client):
    reading(client, 1, minutes_ago=60, pos=(-15.20, 28.30))
    assert reading(client, 1, minutes_ago=30, pos=ALPHA).json()["movement_status"] == "AT_BASE"


# ---------------------------------------------------------------- burst sync / offline buffering
def test_duplicate_sequence_is_idempotent(client):
    a = reading(client, 1, seq=100).json()
    b = reading(client, 1, seq=100).json()
    assert a["accepted"] and b["duplicate"] and not b["accepted"]
    chain = client.get("/api/crates/1/audit").json()["chain"]
    reading(client, 1, seq=100)
    assert client.get("/api/crates/1/audit").json()["chain"]["length"] == chain["length"]


def test_out_of_order_reading_counts_for_cold_chain_but_not_position(client):
    r = reading(client, 2, minutes_ago=100, pos=ALPHA, temp=9.9).json()  # older than crate 2's last_seen
    assert r["out_of_order"] and r["is_locked_out"] and r["movement_status"] == "IN_TRANSIT"
    inv = client.get("/api/inventory", params={"movement_status": "IN_TRANSIT"}).json()["items"][0]
    assert abs(inv["transit"]["latitude"] - (-15.6)) < 1e-6  # position untouched


def test_batch_burst_sync(client):
    rs = [{"crate_id": 1, "device_id": "EB", "sequence": s, "recorded_at": ts(m), "lat": la, "lng": ALPHA[1],
           "temperature_c": 4.5} for s, m, la in [(3, 10, -15.30), (1, 50, ALPHA[0]), (2, 30, -15.35)]]  # shuffled
    rs.append({**rs[0], "crate_id": 999, "sequence": 9})
    r = client.post("/api/telemetry/ingest/batch", json={"readings": rs}).json()
    assert r["processed"] == 3 and r["failed"] == 1
    assert [x["index"] for x in r["results"] if x["ok"]] == [1, 2, 0]  # replayed in device-time order
    assert client.post("/api/telemetry/ingest/batch", json={"readings": rs[:3]}).json()["duplicates"] == 3


# ---------------------------------------------------------------- hash chain / non-repudiation
def test_audit_chain_links(client):
    a = client.get("/api/crates/2/audit").json()
    assert a["chain"]["valid"]
    recs = sorted(a["records"], key=lambda r: r["chain_seq"])
    assert recs[0]["previous_hash"] == "0" * 64
    assert all(recs[i]["previous_hash"] == recs[i - 1]["current_hash"] for i in range(1, len(recs)))
    types = [r["event_type"] for r in recs]
    assert {"CRATE_REGISTERED", "DISPATCH_CREATED", "TELEMETRY_INGEST", "GEOFENCE_TRANSITION", "DISPATCH_DEPARTED"} <= set(types)


def test_edited_audit_record_is_detected(client, db):
    db.execute(text("UPDATE audit_trail SET event_json = replace(event_json, :a, :b) "
                    "WHERE crate_id=1 AND event_type='TELEMETRY_INGEST'"),
               {"a": '"temperature_c":4.2', "b": '"temperature_c":3.0'})
    db.commit()
    v = verify(client, 1)
    assert v["verdict"] == "COMPROMISED — DO NOT USE" and v["checks"]["hash_chain"] == "FAIL"
    assert any(r.startswith("AUDIT_CHAIN_BROKEN") for r in v["reasons"])


def test_deleted_audit_record_is_detected(client, db):
    db.execute(text("DELETE FROM audit_trail WHERE crate_id=1 AND chain_seq=3"))
    db.commit()
    assert "sequence gap" in verify(client, 1)["chain"]["reason"]


def test_tampered_head_triggers_hash_mismatch_lockout_on_next_ingest(client, db):
    db.execute(text("UPDATE audit_trail SET event_json = event_json || ' ' WHERE crate_id=1 AND chain_seq = "
                    "(SELECT MAX(chain_seq) FROM audit_trail WHERE crate_id=1)"))
    db.commit()
    r = reading(client, 1).json()
    assert r["integrity_status"] == "HASH_MISMATCH" and r["is_locked_out"]


def test_anchor_roundtrip_and_truncation_detection(client, db):
    a = client.post("/api/crates/3/anchor").json()
    assert a["is_mock"] and a["tx_hash"].startswith("0x") and a["ipfs_cid"].startswith("mock-cid-")
    assert verify(client, 3)["checks"]["anchor"] == "PASS (mock)"
    # attacker rewrites history *and* recomputes the chain to look valid: the anchor still exposes it
    db.execute(text("DELETE FROM audit_trail WHERE crate_id=3 AND chain_seq >= 3"))
    db.commit()
    v = verify(client, 3)
    assert v["checks"]["anchor"] == "FAIL" and any(r.startswith("ANCHOR_MISMATCH") for r in v["reasons"])


def test_cannot_anchor_broken_chain(client, db):
    db.execute(text("UPDATE audit_trail SET event_json = event_json || ' ' WHERE crate_id=3 AND chain_seq=2"))
    db.commit()
    assert client.post("/api/crates/3/anchor").status_code == 409


def test_verify_by_qr(client):
    r = client.get("/api/crates/by-qr/TITAN-CRATE-0001/verify")
    assert r.status_code == 200 and r.json()["crate_id"] == 1
