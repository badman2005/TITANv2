"""Seed realistic demo data through the real ingest path (so audit chains are genuine).

    python seed.py            # idempotent: does nothing if bases already exist
Sample data only — fictional bases, crews and batch numbers.
"""
from __future__ import annotations

from datetime import timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.config import settings
from app.database import Base_engine_init, SessionLocal
from app.models import Crate, Dispatch, MedicalItem, MilitaryBase
from app.schemas import TelemetryIn
from app.services import ingest_reading
from app.util import utcnow


def _items(db, crate, today, spec):
    for name, typ, batch, qty, unit, days in spec:
        exp = today + timedelta(days=days)
        db.add(MedicalItem(crate_id=crate.crate_id, item_name=name, item_type=typ, batch_number=batch, quantity=qty,
                           unit=unit, expiry_date=exp, item_status="EXPIRED" if days < 0 else "AVAILABLE"))


def _crate(db, today, qr, base, movement, items, dispatch=None):
    c = Crate(qr_code_payload=qr, movement_status=movement, current_base_id=base.base_id,
              current_dispatch_id=dispatch.dispatch_id if dispatch else None,
              latitude=base.latitude, longitude=base.longitude)
    db.add(c); db.flush()
    _items(db, c, today, items)
    audit.append_event(db, crate_id=c.crate_id, event_type=audit.CRATE_REGISTERED,
                       dispatch_id=c.current_dispatch_id, payload={"qr_code_payload": qr, "base": base.base_code,
                                                                   "item_count": len(items)})
    if dispatch:
        audit.append_event(db, crate_id=c.crate_id, event_type=audit.DISPATCH_CREATED, dispatch_id=dispatch.dispatch_id,
                           payload={"vehicle_callsign": dispatch.vehicle_callsign, "driver_name": dispatch.driver_name})
    return c


def _feed(db, now, crate, device, pts):
    """pts = [(minutes_ago, lat, lng, temp_c, tamper)] oldest first."""
    for seq, (mins, lat, lng, t, tamper) in enumerate(pts, start=1):
        ingest_reading(db, TelemetryIn(
            crate_id=crate.crate_id, device_id=device, sequence=seq,
            recorded_at=(now - timedelta(minutes=mins)).replace(tzinfo=timezone.utc),
            lat=lat, lng=lng, temperature_c=t, humidity_pct=38.0, battery_pct=90.0, ldr_tamper=tamper))


def run(db: Session) -> dict:
    if db.scalar(select(func.count()).select_from(MilitaryBase)):
        return {"seeded": False, "reason": "military_bases not empty"}
    now, today = utcnow(), utcnow().date()

    A = MilitaryBase(base_code="ALPHA", base_name="Main Medical Depot Alpha", sector_or_region="Central Command",
                     latitude=-15.4167, longitude=28.2833, geofence_radius_m=800)
    B = MilitaryBase(base_code="BRAVO", base_name="Forward Operating Base Bravo", sector_or_region="Northern Sector",
                     latitude=-14.4469, longitude=28.4464, geofence_radius_m=500)
    C = MilitaryBase(base_code="CHARLIE", base_name="Field Hospital Charlie", sector_or_region="Southern Sector",
                     latitude=-15.7667, longitude=28.1833, geofence_radius_m=500)
    db.add_all([A, B, C]); db.flush()

    d1 = Dispatch(vehicle_callsign="TITAN-07", driver_name="Sgt. M. Banda", origin_base_id=A.base_id,
                  destination_base_id=B.base_id)
    d2 = Dispatch(vehicle_callsign="TITAN-12", driver_name="Cpl. T. Mwansa", origin_base_id=A.base_id,
                  destination_base_id=C.base_id)
    db.add_all([d1, d2]); db.flush()

    # Crate 1 — loaded at ALPHA, dispatch PLANNED (use for live demo / simulator)
    c1 = _crate(db, today, "TITAN-CRATE-0001", A, "AT_BASE", [
        ("mRNA COVID-19 Vaccine", "VACCINE", "MRN-24K19", 40, "vials", 120),
        ("Influenza Vaccine (Quadrivalent)", "VACCINE", "FLU-8831", 60, "doses", 90)], d1)
    _feed(db, now, c1, "EDGE-TITAN-01", [(360, -15.4167, 28.2833, 4.1, False), (350, -15.4166, 28.2834, 4.3, False),
                                         (340, -15.4167, 28.2832, 4.2, False)])
    # Crate 2 — already en route ALPHA→CHARLIE (departure + transit derived by the real geofence logic)
    c2 = _crate(db, today, "TITAN-CRATE-0002", A, "AT_BASE", [
        ("O-Negative Packed Red Cells", "BLOOD_PRODUCT", "RBC-ON-7702", 12, "units", 21),
        ("Albumin 5%", "DRUG", "ALB-5521", 20, "bottles", 120)], d2)
    _feed(db, now, c2, "EDGE-TITAN-02", [(180, -15.4167, 28.2833, 4.8, False), (170, -15.4166, 28.2832, 4.9, False),
                                         (150, -15.4500, 28.2700, 5.2, False), (90, -15.5500, 28.2400, 5.5, False),
                                         (20, -15.6000, 28.2250, 5.9, False)])
    # Crate 3 — in storage at BRAVO, healthy; one item expiring soon (warning, still safe)
    c3 = _crate(db, today, "TITAN-CRATE-0003", B, "IN_STORAGE", [
        ("Rabies Immunoglobulin", "DRUG", "RIG-3310", 15, "vials", 9),
        ("Sterile Saline 0.9%", "MEDICAL_SUPPLY", "SAL-9001", 200, "bags", 400)])
    _feed(db, now, c3, "EDGE-TITAN-03", [(300, -14.4469, 28.4464, 3.5, False), (200, -14.4470, 28.4463, 3.6, False),
                                         (100, -14.4469, 28.4465, 3.4, False)])
    # Crate 4 — in storage at ALPHA, thermal excursion → real lockout through ingest
    c4 = _crate(db, today, "TITAN-CRATE-0004", A, "IN_STORAGE", [
        ("Hepatitis B Vaccine", "VACCINE", "HEPB-6644", 30, "vials", 200),
        ("Insulin (Regular)", "DRUG", "INS-1198", 25, "vials", 150)])
    _feed(db, now, c4, "EDGE-TITAN-04", [(240, -15.4167, 28.2833, 4.0, False), (120, -15.4167, 28.2833, 5.1, False),
                                         (60, -15.4167, 28.2833, 9.7, False)])
    # Crate 5 — in storage at ALPHA, healthy cold chain but contains an expired item
    c5 = _crate(db, today, "TITAN-CRATE-0005", A, "IN_STORAGE", [
        ("Tetanus Toxoid Vaccine", "VACCINE", "TT-2207", 50, "doses", -5),
        ("Sterile Gauze Pack", "MEDICAL_SUPPLY", "GZ-5000", 500, "packs", 700)])
    _feed(db, now, c5, "EDGE-TITAN-05", [(200, -15.4167, 28.2833, 4.4, False), (100, -15.4167, 28.2833, 4.6, False)])
    db.commit()
    return {"seeded": True, "bases": 3, "dispatches": 2, "crates": [c.qr_code_payload for c in (c1, c2, c3, c4, c5)]}


if __name__ == "__main__":
    if settings.auto_create_tables:
        Base_engine_init()
    with SessionLocal() as s:
        print(run(s))
