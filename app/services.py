"""Business logic for telemetry ingest and crate verification.

Callers own the transaction: these functions flush but never commit.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from . import audit, rules
from .config import settings
from .geo import classify_movement, containing_base, haversine_m
from .models import (AnchorReceipt, AuditTrail, Crate, DispatchStatus, IntegrityStatus, ItemStatus,
                     MedicalItem, MilitaryBase, MovementStatus, TelemetryReading)
from .rules import Violation, classify_expiry, evaluate_telemetry, integrity_for, mean_kinetic_temperature
from .schemas import (SAFE_VERDICT, UNSAFE_VERDICT, ItemVerdict, TelemetryIn, TelemetryResult,
                      VerifyResponse, ViolationOut)
from .util import TitanError, iso, to_utc_naive, utcnow

MAX_REASON_LEN = 512


# --------------------------------------------------------------------------- helpers
def _lock_crate(db: Session, crate_id: Optional[int] = None, qr: Optional[str] = None) -> Optional[Crate]:
    stmt = select(Crate)
    if crate_id is not None:
        stmt = stmt.where(Crate.crate_id == crate_id)
    if qr is not None:
        stmt = stmt.where(Crate.qr_code_payload == qr)
    return db.scalars(stmt.with_for_update()).first()  # row lock serialises the crate's hash chain


def _server_mkt(db: Session, crate_id: int, recorded_at, current_temp: float) -> Optional[float]:
    start = recorded_at - timedelta(hours=settings.mkt_window_hours)
    prior = db.scalars(
        select(TelemetryReading.temperature_c)
        .where(TelemetryReading.crate_id == crate_id,
               TelemetryReading.recorded_at >= start, TelemetryReading.recorded_at <= recorded_at)
        .limit(5000)
    ).all()
    m = mean_kinetic_temperature([*prior, current_temp])
    return None if m is None else round(m, 3)


def _result(crate: Crate, *, accepted: bool, duplicate=False, out_of_order=False, prev_movement: str,
            violations: list[Violation], d_o=None, d_d=None, appended=0, head: Optional[str] = None) -> TelemetryResult:
    return TelemetryResult(
        accepted=accepted, duplicate=duplicate, out_of_order=out_of_order, crate_id=crate.crate_id,
        dispatch_id=crate.current_dispatch_id, previous_movement_status=prev_movement,
        movement_status=crate.movement_status, integrity_status=crate.integrity_status,
        is_locked_out=crate.is_locked_out, lockout_reason=crate.lockout_reason,
        violations=[ViolationOut(code=v.code, detail=v.detail) for v in violations],
        distance_to_origin_m=None if d_o is None else round(d_o, 1),
        distance_to_destination_m=None if d_d is None else round(d_d, 1),
        audit_records_appended=appended, audit_head_hash=head)


# --------------------------------------------------------------------------- ingest
def ingest_reading(db: Session, p: TelemetryIn) -> TelemetryResult:
    now = utcnow()
    rec = to_utc_naive(p.recorded_at)
    if rec > now + timedelta(seconds=settings.max_future_skew_s):
        raise TitanError(422, f"recorded_at {iso(rec)} is in the future (check edge-device clock)")

    crate = _lock_crate(db, p.crate_id, p.qr_code_payload)
    if crate is None:
        raise TitanError(404, "No crate matches the supplied crate_id / qr_code_payload")

    # Idempotent burst-sync: the same (device, sequence) is accepted exactly once.
    if p.sequence is not None:
        dup = db.scalar(select(TelemetryReading.reading_id).where(
            TelemetryReading.crate_id == crate.crate_id, TelemetryReading.device_id == p.device_id,
            TelemetryReading.sequence == p.sequence))
        if dup is not None:
            return _result(crate, accepted=False, duplicate=True, prev_movement=crate.movement_status, violations=[])

    dispatch = crate.dispatch
    origin = dispatch.origin_base if dispatch else None
    dest = dispatch.destination_base if dispatch else None
    prev_movement = crate.movement_status
    # Buffered readings may arrive after newer ones. They still count for cold-chain/tamper rules,
    # but must never drag the crate's position/state backwards.
    out_of_order = crate.last_seen_at is not None and rec < crate.last_seen_at

    # ---- 1. rules --------------------------------------------------------
    mkt_srv = _server_mkt(db, crate.crate_id, rec, p.temperature_c)
    violations = evaluate_telemetry(p.temperature_c, p.ldr_tamper, [mkt_srv, p.mkt_c])

    head = audit.get_head(db, crate.crate_id)
    if head is not None and audit.compute_hash(head.previous_hash, head.event_json) != head.current_hash:
        violations.append(Violation(rules.CHAIN, f"AUDIT_CHAIN_HEAD_MISMATCH: record #{head.chain_seq} fails SHA-256 re-computation"))

    # ---- 2. geofence / movement -----------------------------------------
    d_o = d_d = None
    new_movement, new_base_id = prev_movement, crate.current_base_id
    if not out_of_order:
        if dispatch is not None and origin is not None and dest is not None:
            d_o = haversine_m(p.lat, p.lng, origin.latitude, origin.longitude)
            d_d = haversine_m(p.lat, p.lng, dest.latitude, dest.longitude)
            new_movement = classify_movement(d_o, origin.geofence_radius_m, d_d, dest.geofence_radius_m)
            new_base_id = {MovementStatus.AT_BASE: origin.base_id,
                           MovementStatus.AT_BATTLEFIELD: dest.base_id}.get(new_movement)
        elif prev_movement not in (MovementStatus.IN_STORAGE, MovementStatus.DISPENSED):
            base = containing_base(db.scalars(select(MilitaryBase)).all(), p.lat, p.lng)
            if base is not None:
                new_movement, new_base_id = MovementStatus.AT_BASE, base.base_id

    # ---- 3. persist the raw reading (single INSERT; table is append-only) ----
    db.add(TelemetryReading(
        crate_id=crate.crate_id, dispatch_id=crate.current_dispatch_id, device_id=p.device_id,
        sequence=p.sequence, recorded_at=rec, received_at=now, latitude=p.lat, longitude=p.lng,
        temperature_c=p.temperature_c, humidity_pct=p.humidity_pct, battery_pct=p.battery_pct,
        ldr_tamper=p.ldr_tamper, mkt_device_c=p.mkt_c, mkt_server_c=mkt_srv,
        violations=",".join(v.code for v in violations) or None))

    # ---- 4. build the audit events (written in causal order) -------------
    events: list[tuple[str, dict]] = [(audit.TELEMETRY_INGEST, {
        "device_id": p.device_id, "sequence": p.sequence, "recorded_at": rec,
        "lat": round(p.lat, 6), "lng": round(p.lng, 6), "temperature_c": round(p.temperature_c, 2),
        "humidity_pct": p.humidity_pct, "ldr_tamper": p.ldr_tamper,
        "mkt_server_c": mkt_srv, "mkt_device_c": p.mkt_c, "violations": [v.code for v in violations],
        "movement_status": new_movement, "out_of_order": out_of_order})]

    if new_movement != prev_movement:
        events.append((audit.GEOFENCE_TRANSITION, {
            "from": prev_movement, "to": new_movement, "lat": round(p.lat, 6), "lng": round(p.lng, 6),
            "distance_to_origin_m": None if d_o is None else round(d_o, 1),
            "distance_to_destination_m": None if d_d is None else round(d_d, 1)}))

    if dispatch is not None and not out_of_order and dispatch.status != DispatchStatus.CANCELLED:
        if dispatch.status == DispatchStatus.PLANNED and new_movement != MovementStatus.AT_BASE:
            dispatch.status = DispatchStatus.EN_ROUTE
            dispatch.dispatched_at = dispatch.dispatched_at or rec
            events.append((audit.DISPATCH_DEPARTED, {
                "vehicle_callsign": dispatch.vehicle_callsign, "driver_name": dispatch.driver_name,
                "origin": origin.base_code, "destination": dest.base_code, "departed_at": dispatch.dispatched_at}))
        if new_movement == MovementStatus.AT_BATTLEFIELD and dispatch.status != DispatchStatus.ARRIVED:
            dispatch.status = DispatchStatus.ARRIVED
            dispatch.arrived_at = rec
            dispatch.dispatched_at = dispatch.dispatched_at or rec
            events.append((audit.BASE_HANDOFF, {
                "from_base": origin.base_code, "to_base": dest.base_code, "arrived_at": rec,
                "vehicle_callsign": dispatch.vehicle_callsign, "driver_name": dispatch.driver_name}))

    # ---- 5. apply crate state --------------------------------------------
    if not out_of_order:
        crate.latitude, crate.longitude, crate.last_seen_at = p.lat, p.lng, rec
        crate.movement_status, crate.current_base_id = new_movement, new_base_id

    # Persist expiry first, so an expired item is reported EXPIRED rather than QUARANTINED.
    db.execute(update(MedicalItem).where(
        MedicalItem.crate_id == crate.crate_id,
        MedicalItem.item_status.in_([ItemStatus.AVAILABLE, ItemStatus.QUARANTINED]),
        MedicalItem.expiry_date < now.date()).values(item_status=ItemStatus.EXPIRED))

    if violations:
        if not crate.is_locked_out:
            crate.is_locked_out, crate.locked_out_at = True, rec
            crate.integrity_status = integrity_for(violations)
            crate.lockout_reason = "; ".join(v.detail for v in violations)[:MAX_REASON_LEN]
            db.execute(update(MedicalItem).where(
                MedicalItem.crate_id == crate.crate_id, MedicalItem.item_status == ItemStatus.AVAILABLE
            ).values(item_status=ItemStatus.QUARANTINED))
            events.append((audit.LOCKOUT, {
                "integrity_status": crate.integrity_status, "reasons": [v.detail for v in violations],
                "triggered_by_reading_at": rec, "device_id": p.device_id}))
        else:  # lockout is sticky; record any *new* failure modes
            extra = [v.detail for v in violations if v.code not in (crate.lockout_reason or "")]
            if extra:
                crate.lockout_reason = f"{crate.lockout_reason} | {'; '.join(extra)}"[:MAX_REASON_LEN]

    # ---- 6. append to the hash chain -------------------------------------
    last = None
    for etype, payload in events:
        last = audit.append_event(db, crate_id=crate.crate_id, event_type=etype, payload=payload,
                                  dispatch_id=crate.current_dispatch_id, occurred_at=rec)
    return _result(crate, accepted=True, out_of_order=out_of_order, prev_movement=prev_movement,
                   violations=violations, d_o=d_o, d_d=d_d, appended=len(events),
                   head=last.current_hash if last else None)


# --------------------------------------------------------------------------- verify
def verify_crate(db: Session, crate_id: int) -> VerifyResponse:
    crate = _lock_crate(db, crate_id)
    if crate is None:
        raise TitanError(404, f"Crate {crate_id} not found")
    now = utcnow()
    today = now.date()
    lo, hi = settings.temp_min_c, settings.temp_max_c
    reasons: list[str] = []
    warnings: list[str] = []
    checks: dict[str, str] = {}
    R = TelemetryReading

    # 1. lockout / integrity flag
    if crate.is_locked_out:
        reasons.append(f"LOCKOUT: {crate.lockout_reason or 'crate is locked out'}")
        checks["lockout"] = "FAIL"
    elif crate.integrity_status != IntegrityStatus.SAFE:
        reasons.append(f"INTEGRITY: crate integrity_status is {crate.integrity_status}")
        checks["lockout"] = "FAIL"
    else:
        checks["lockout"] = "PASS"

    # 2. cold-chain + tamper history (re-derived from raw readings, not just the cached flag)
    total, tmin, tmax = db.execute(select(func.count(R.reading_id), func.min(R.temperature_c),
                                          func.max(R.temperature_c)).where(R.crate_id == crate_id)).one()
    exc_n, exc_first = db.execute(select(func.count(R.reading_id), func.min(R.recorded_at)).where(
        R.crate_id == crate_id, or_(R.temperature_c < lo, R.temperature_c > hi))).one()
    tam_n, tam_first = db.execute(select(func.count(R.reading_id), func.min(R.recorded_at)).where(
        R.crate_id == crate_id, R.ldr_tamper.is_(True))).one()
    mkt = mean_kinetic_temperature(db.scalars(select(R.temperature_c).where(R.crate_id == crate_id)).all())

    if total == 0:
        reasons.append("COLD_CHAIN_UNVERIFIED: no telemetry has ever been recorded for this crate")
        checks["cold_chain"] = "FAIL"
    elif exc_n or (mkt is not None and rules.out_of_range(mkt)):
        if exc_n:
            reasons.append(f"COLD_CHAIN_BREACH: {exc_n} reading(s) outside {lo:g}–{hi:g}°C "
                           f"(range seen {tmin:.1f}–{tmax:.1f}°C, first at {iso(exc_first)})")
        if mkt is not None and rules.out_of_range(mkt):
            reasons.append(f"MKT_BREACH: lifetime MKT {mkt:.2f}°C outside {lo:g}–{hi:g}°C")
        checks["cold_chain"] = "FAIL"
    else:
        checks["cold_chain"] = "PASS"
    if tam_n:
        reasons.append(f"TAMPER_HISTORY: {tam_n} tamper-flagged reading(s), first at {iso(tam_first)}")
        checks["tamper"] = "FAIL"
    else:
        checks["tamper"] = "PASS" if total else "FAIL"

    # 3. SHA-256 hash chain
    chain = audit.verify_chain(db, crate_id)
    if chain.length == 0:
        reasons.append("AUDIT_TRAIL_MISSING: crate has no audit records")
        checks["hash_chain"] = "FAIL"
    elif not chain.valid:
        reasons.append(f"AUDIT_CHAIN_BROKEN: record #{chain.broken_at_seq}: {chain.reason}")
        checks["hash_chain"] = "FAIL"
    else:
        checks["hash_chain"] = "PASS"

    # 4. external anchor consistency (also catches tail truncation)
    anchor = db.scalars(select(AnchorReceipt).where(AnchorReceipt.crate_id == crate_id)
                        .order_by(AnchorReceipt.chain_length.desc(), AnchorReceipt.anchor_id.desc()).limit(1)).first()
    if anchor is None:
        checks["anchor"] = "NOT_ANCHORED"
    else:
        at = db.scalars(select(AuditTrail).where(AuditTrail.crate_id == crate_id,
                                                 AuditTrail.chain_seq == anchor.chain_length)).first()
        if at is None:
            reasons.append(f"ANCHOR_MISMATCH: record #{anchor.chain_length} anchored via {anchor.provider} no longer exists (chain truncated)")
            checks["anchor"] = "FAIL"
        elif at.current_hash != anchor.head_hash:
            reasons.append(f"ANCHOR_MISMATCH: record #{anchor.chain_length} differs from the externally anchored hash")
            checks["anchor"] = "FAIL"
        else:
            checks["anchor"] = "PASS" + (" (mock)" if anchor.is_mock else "")

    crate_level_ok = not reasons

    # 5. item expiry / status
    item_rows = db.scalars(select(MedicalItem).where(MedicalItem.crate_id == crate_id)
                           .order_by(MedicalItem.item_id)).all()
    verdicts: list[ItemVerdict] = []
    for it in item_rows:
        est, days = classify_expiry(it.expiry_date, today)
        ir: list[str] = []
        if it.item_status == ItemStatus.DISPENSED:
            ir.append("ALREADY_DISPENSED")  # informational — excluded from the crate verdict
        elif est == "EXPIRED":
            msg = f"EXPIRED_ITEM: {it.item_name} (batch {it.batch_number}) expired {it.expiry_date.isoformat()}"
            ir.append(msg); reasons.append(msg)
        elif it.item_status == ItemStatus.QUARANTINED:
            msg = f"QUARANTINED_ITEM: {it.item_name} (batch {it.batch_number}) is quarantined"
            ir.append(msg); reasons.append(msg)
        elif est == "EXPIRING_SOON":
            warnings.append(f"EXPIRING_SOON: {it.item_name} (batch {it.batch_number}) expires in {days} day(s)")
        verdicts.append(ItemVerdict(
            item_id=it.item_id, item_name=it.item_name, item_type=it.item_type, batch_number=it.batch_number,
            expiry_date=it.expiry_date, expiry_status=est, days_to_expiry=days, item_status=it.item_status,
            safe_to_administer=crate_level_ok and not ir, reasons=ir))
    checks["expiry"] = "FAIL" if any(v.expiry_status == "EXPIRED" and v.item_status != ItemStatus.DISPENSED
                                     for v in verdicts) else ("WARN" if warnings else "PASS")
    if not item_rows:
        warnings.append("NO_ITEMS: no medical items are registered to this crate")

    safe = not reasons
    row = audit.append_event(db, crate_id=crate_id, event_type=audit.VERIFICATION,
                             dispatch_id=crate.current_dispatch_id, occurred_at=now, payload={
                                 "verdict": SAFE_VERDICT if safe else UNSAFE_VERDICT,
                                 "reason_codes": [r.split(":", 1)[0] for r in reasons],
                                 "checks": checks, "chain_length_before": chain.length})
    return VerifyResponse(
        crate_id=crate_id, qr_code_payload=crate.qr_code_payload,
        verdict=SAFE_VERDICT if safe else UNSAFE_VERDICT, safe=safe, reasons=reasons, warnings=warnings,
        checks=checks, items=verdicts, chain=chain, verified_at=now, verification_audit_hash=row.current_hash)
