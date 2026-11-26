"""Project TITAN — Module 1 API.   Run:  uvicorn app.main:app --reload"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Optional

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from . import audit, demo, services
from .anchor import anchor_crate
from .config import settings
from .database import Base_engine_init, get_db
from .models import Crate, Dispatch, MedicalItem, MilitaryBase, MovementStatus, AuditTrail
from .rules import classify_expiry
from .schemas import (AnchorOut, AuditRecordOut, AuditResponse, BatchItemResult, CrateBrief, ExpiryLiteral,
                      BaseOut, CrateOut, DispatchBrief, InventoryItem, InventoryResponse, ItemTypeLiteral, MovementLiteral, StorageInfo,
                      TelemetryBatchIn, TelemetryBatchResult, TelemetryIn, TelemetryResult, TransitInfo,
                      VerifyResponse)
from .util import TitanError, utcnow


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.auto_create_tables:
        Base_engine_init()
    yield
    demo.stop_all()


app = FastAPI(title="Project TITAN — Medical Logistics & Cold-Chain Verification", version="0.1.0", lifespan=lifespan)
app.include_router(demo.router)
app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(TitanError)
async def _titan_error(_, exc: TitanError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok", "time_utc": utcnow().isoformat() + "Z"}


# ------------------------------------------------------------------ telemetry
@app.post("/api/telemetry/ingest", response_model=TelemetryResult)
def ingest(payload: TelemetryIn, db: Session = Depends(get_db)):
    """Validate → evaluate rules + geofence → update crate/dispatch → append SHA-256 chained audit records."""
    result = services.ingest_reading(db, payload)
    db.commit()
    return result


@app.post("/api/telemetry/ingest/batch", response_model=TelemetryBatchResult)
def ingest_batch(batch: TelemetryBatchIn, db: Session = Depends(get_db)):
    """Burst-sync of an offline buffer. Replayed in device-time order, one transaction per reading,
    so one bad reading never discards the rest. Safe to retry (sequence-based idempotency)."""
    order = sorted(range(len(batch.readings)),
                   key=lambda i: (batch.readings[i].recorded_at.timestamp() if batch.readings[i].recorded_at.tzinfo
                                  else batch.readings[i].recorded_at.replace(tzinfo=None).timestamp(), i))
    out: list[BatchItemResult] = []
    dups = failed = 0
    for i in order:
        try:
            r = services.ingest_reading(db, batch.readings[i])
            db.commit()
            dups += r.duplicate
            out.append(BatchItemResult(index=i, ok=True, result=r))
        except TitanError as e:
            db.rollback()
            failed += 1
            out.append(BatchItemResult(index=i, ok=False, error=e.detail))
    return TelemetryBatchResult(processed=len(out) - failed, duplicates=dups, failed=failed, results=out)


# ------------------------------------------------------------------ inventory
@app.get("/api/inventory", response_model=InventoryResponse)
def inventory(
    movement_status: Optional[MovementLiteral] = None,
    item_type: Optional[ItemTypeLiteral] = None,
    base_code: Optional[str] = Query(None, description="Crate is at this base, or IN_TRANSIT to/from it"),
    expiry_status: Optional[ExpiryLiteral] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    today = utcnow().date()
    soon = today + timedelta(days=settings.expiring_soon_days)

    stmt = (select(MedicalItem)
            .join(Crate, MedicalItem.crate_id == Crate.crate_id)
            .outerjoin(Dispatch, Crate.current_dispatch_id == Dispatch.dispatch_id))
    if movement_status:
        stmt = stmt.where(Crate.movement_status == movement_status)
    if item_type:
        stmt = stmt.where(MedicalItem.item_type == item_type)
    if base_code:
        base = db.scalars(select(MilitaryBase).where(MilitaryBase.base_code == base_code.upper())).first()
        if base is None:
            return InventoryResponse(total=0, count=0, limit=limit, offset=offset, items=[])
        stmt = stmt.where(or_(
            Crate.current_base_id == base.base_id,
            and_(Crate.movement_status == MovementStatus.IN_TRANSIT,
                 or_(Dispatch.origin_base_id == base.base_id, Dispatch.destination_base_id == base.base_id))))
    if expiry_status == "EXPIRED":
        stmt = stmt.where(MedicalItem.expiry_date < today)
    elif expiry_status == "EXPIRING_SOON":
        stmt = stmt.where(MedicalItem.expiry_date >= today, MedicalItem.expiry_date <= soon)
    elif expiry_status == "VALID":
        stmt = stmt.where(MedicalItem.expiry_date > soon)

    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(
        stmt.options(
            selectinload(MedicalItem.crate).selectinload(Crate.current_base),
            selectinload(MedicalItem.crate).selectinload(Crate.dispatch).selectinload(Dispatch.origin_base),
            selectinload(MedicalItem.crate).selectinload(Crate.dispatch).selectinload(Dispatch.destination_base))
        .order_by(MedicalItem.expiry_date, MedicalItem.item_id).limit(limit).offset(offset)).all()

    items: list[InventoryItem] = []
    for it in rows:
        c = it.crate
        est, days = classify_expiry(it.expiry_date, today)
        transit = storage = None
        if c.movement_status == MovementStatus.IN_TRANSIT and c.dispatch is not None:
            d = c.dispatch
            transit = TransitInfo(
                dispatch_id=d.dispatch_id, vehicle_callsign=d.vehicle_callsign, driver_name=d.driver_name,
                origin_base_code=d.origin_base.base_code, destination_base_code=d.destination_base.base_code,
                dispatched_at=d.dispatched_at, latitude=c.latitude, longitude=c.longitude, last_seen_at=c.last_seen_at)
        else:
            b = c.current_base
            storage = StorageInfo(base_code=b.base_code if b else None, base_name=b.base_name if b else None,
                                  sector_or_region=b.sector_or_region if b else None,
                                  latitude=b.latitude if b else c.latitude, longitude=b.longitude if b else c.longitude)
        items.append(InventoryItem(
            item_id=it.item_id, item_name=it.item_name, item_type=it.item_type, batch_number=it.batch_number,
            quantity=it.quantity, unit=it.unit, expiry_date=it.expiry_date, item_status=it.item_status,
            expiry_status=est, days_to_expiry=days, integrity_status=c.integrity_status,
            is_locked_out=c.is_locked_out,
            preliminary_safe=(not c.is_locked_out and c.integrity_status == "SAFE"
                              and it.item_status == "AVAILABLE" and est != "EXPIRED"),
            crate=CrateBrief(crate_id=c.crate_id, qr_code_payload=c.qr_code_payload,
                             movement_status=c.movement_status, integrity_status=c.integrity_status,
                             is_locked_out=c.is_locked_out, lockout_reason=c.lockout_reason,
                             last_seen_at=c.last_seen_at),
            transit=transit, storage=storage))
    return InventoryResponse(total=total, count=len(items), limit=limit, offset=offset, items=items)


# ------------------------------------------------------------------ verification
@app.get("/api/crates/{crate_id}/verify", response_model=VerifyResponse)
def verify(crate_id: int, db: Session = Depends(get_db)):
    """Authoritative go/no-go. Each call is itself written to the crate's audit chain (VERIFICATION)."""
    result = services.verify_crate(db, crate_id)
    db.commit()
    return result


@app.get("/api/crates/by-qr/{qr_payload:path}/verify", response_model=VerifyResponse)
def verify_by_qr(qr_payload: str, db: Session = Depends(get_db)):
    """Convenience for the PWA scanner: verify straight from the decoded QR payload."""
    crate_id = db.scalar(select(Crate.crate_id).where(Crate.qr_code_payload == qr_payload))
    if crate_id is None:
        raise HTTPException(404, "Unknown QR payload")
    result = services.verify_crate(db, crate_id)
    db.commit()
    return result


@app.get("/api/crates/{crate_id}/audit", response_model=AuditResponse)
def audit_trail(crate_id: int, limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    if db.get(Crate, crate_id) is None:
        raise HTTPException(404, f"Crate {crate_id} not found")
    chain = audit.verify_chain(db, crate_id)
    rows = db.scalars(select(AuditTrail).where(AuditTrail.crate_id == crate_id)
                      .order_by(AuditTrail.chain_seq.desc()).limit(limit)).all()
    recs = []
    for r in rows:
        try:
            ev = json.loads(r.event_json)
        except ValueError:
            ev = {"_error": "event_json is not valid JSON"}
        recs.append(AuditRecordOut(audit_id=r.audit_id, chain_seq=r.chain_seq, event_type=r.event_type,
                                   dispatch_id=r.dispatch_id, previous_hash=r.previous_hash,
                                   current_hash=r.current_hash, created_at=r.created_at, event=ev))
    return AuditResponse(crate_id=crate_id, chain=chain, records=recs)


@app.post("/api/crates/{crate_id}/anchor", response_model=AnchorOut)
def anchor(crate_id: int, db: Session = Depends(get_db)):
    """Anchor the crate's chain head via the configured provider (ANCHOR_BACKEND = mock | ipfs | polygon)."""
    row = anchor_crate(db, crate_id)
    db.commit()
    return AnchorOut(anchor_id=row.anchor_id, crate_id=row.crate_id, chain_length=row.chain_length,
                     head_hash=row.head_hash, provider=row.provider, network=row.network, ipfs_cid=row.ipfs_cid,
                     tx_hash=row.tx_hash, is_mock=row.is_mock, anchored_at=row.anchored_at)


# ------------------------------------------------------------------ bases & crates (map, labels, filters)
@app.get("/api/bases", response_model=list[BaseOut])
def bases(db: Session = Depends(get_db)):
    return [BaseOut(base_id=b.base_id, base_code=b.base_code, base_name=b.base_name,
                    sector_or_region=b.sector_or_region, latitude=b.latitude, longitude=b.longitude,
                    geofence_radius_m=b.geofence_radius_m)
            for b in db.scalars(select(MilitaryBase).order_by(MilitaryBase.base_code))]


def _crate_out(c: Crate, item_count: int) -> CrateOut:
    d = c.dispatch
    return CrateOut(
        crate_id=c.crate_id, qr_code_payload=c.qr_code_payload, movement_status=c.movement_status,
        integrity_status=c.integrity_status, is_locked_out=c.is_locked_out, lockout_reason=c.lockout_reason,
        last_seen_at=c.last_seen_at, latitude=c.latitude, longitude=c.longitude,
        base_code=c.current_base.base_code if c.current_base else None, item_count=item_count,
        dispatch=None if d is None else DispatchBrief(
            dispatch_id=d.dispatch_id, vehicle_callsign=d.vehicle_callsign, driver_name=d.driver_name,
            origin_base_code=d.origin_base.base_code, destination_base_code=d.destination_base.base_code,
            status=d.status, dispatched_at=d.dispatched_at, arrived_at=d.arrived_at))


@app.get("/api/crates", response_model=list[CrateOut])
def crates(movement_status: Optional[MovementLiteral] = None, db: Session = Depends(get_db)):
    stmt = (select(Crate).options(selectinload(Crate.current_base),
                                  selectinload(Crate.dispatch).selectinload(Dispatch.origin_base),
                                  selectinload(Crate.dispatch).selectinload(Dispatch.destination_base))
            .order_by(Crate.crate_id))
    if movement_status:
        stmt = stmt.where(Crate.movement_status == movement_status)
    counts = dict(db.execute(select(MedicalItem.crate_id, func.count()).group_by(MedicalItem.crate_id)).all())
    return [_crate_out(c, counts.get(c.crate_id, 0)) for c in db.scalars(stmt)]


@app.get("/api/crates/{crate_id}", response_model=CrateOut)
def crate_detail(crate_id: int, db: Session = Depends(get_db)):
    c = db.get(Crate, crate_id)
    if c is None:
        raise HTTPException(404, f"Crate {crate_id} not found")
    n = db.scalar(select(func.count()).select_from(MedicalItem).where(MedicalItem.crate_id == crate_id)) or 0
    return _crate_out(c, n)


# ------------------------------------------------------------------ serve the built PWA (frontend/dist)
DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if DIST.is_dir() and (DIST / "index.html").is_file():
    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(404, "Not found")
        target = (DIST / full_path).resolve()
        if full_path and target.is_file() and DIST.resolve() in target.parents:
            headers = {"Cache-Control": "no-cache"} if target.name in ("sw.js", "index.html", "manifest.webmanifest") \
                else ({"Cache-Control": "public, max-age=31536000, immutable"} if "assets" in target.parts else {})
            return FileResponse(target, headers=headers)
        return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})  # client-side routing
