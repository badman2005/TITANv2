"""Per-crate append-only SHA-256 hash chain.

    current_hash = SHA-256( previous_hash + canonical_event_json )

`canonical_event_json` is stored verbatim in audit_trail.event_json, so verification re-hashes the
exact bytes that were originally hashed (no re-serialisation drift).  The genesis record uses a
previous_hash of 64 zeros.  One chain per crate keeps verification O(crate history) and lets
different crates ingest in parallel (the crate row is locked FOR UPDATE during appends).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditTrail
from .schemas import ChainStatus
from .util import iso, utcnow

GENESIS_HASH = "0" * 64

# Event types
CRATE_REGISTERED = "CRATE_REGISTERED"
DISPATCH_CREATED = "DISPATCH_CREATED"
DISPATCH_DEPARTED = "DISPATCH_DEPARTED"
TELEMETRY_INGEST = "TELEMETRY_INGEST"
GEOFENCE_TRANSITION = "GEOFENCE_TRANSITION"
BASE_HANDOFF = "BASE_HANDOFF"
LOCKOUT = "LOCKOUT"
VERIFICATION = "VERIFICATION"
ANCHORED = "ANCHORED"


def _default(o: Any):
    if isinstance(o, datetime):
        return iso(o)
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(f"Not JSON serialisable: {type(o)}")


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8, NaN/Inf rejected."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=_default)


def compute_hash(previous_hash: str, event_json: str) -> str:
    return hashlib.sha256((previous_hash + event_json).encode("utf-8")).hexdigest()


def get_head(db: Session, crate_id: int) -> Optional[AuditTrail]:
    return db.scalars(
        select(AuditTrail).where(AuditTrail.crate_id == crate_id)
        .order_by(AuditTrail.chain_seq.desc()).limit(1)
    ).first()


def append_event(db: Session, *, crate_id: int, event_type: str, payload: dict,
                 dispatch_id: Optional[int] = None, occurred_at: Optional[datetime] = None) -> AuditTrail:
    """Append one record to the crate's chain. Caller must hold the crate row lock and commit."""
    head = get_head(db, crate_id)
    seq = (head.chain_seq + 1) if head else 1
    prev = head.current_hash if head else GENESIS_HASH
    event = {
        "crate_id": crate_id,
        "dispatch_id": dispatch_id,
        "event_type": event_type,
        "chain_seq": seq,
        "occurred_at": occurred_at or utcnow(),
        "payload": payload,
    }
    event_json = canonical_json(event)
    row = AuditTrail(crate_id=crate_id, chain_seq=seq, dispatch_id=dispatch_id, event_type=event_type,
                     event_json=event_json, previous_hash=prev,
                     current_hash=compute_hash(prev, event_json), created_at=utcnow())
    db.add(row)
    db.flush()
    return row


def verify_chain(db: Session, crate_id: int) -> ChainStatus:
    """Re-walk the whole chain from genesis. Detects edits, reordering, and mid-chain deletions.
    (Tail truncation is only detectable against an external anchor — see anchor.py.)"""
    rows = db.scalars(select(AuditTrail).where(AuditTrail.crate_id == crate_id)
                      .order_by(AuditTrail.chain_seq)).all()
    prev = GENESIS_HASH

    def broken(seq: int, why: str) -> ChainStatus:
        return ChainStatus(valid=False, length=len(rows), head_hash=rows[-1].current_hash if rows else None,
                           broken_at_seq=seq, reason=why)

    for expected, row in enumerate(rows, start=1):
        if row.chain_seq != expected:
            return broken(expected, f"sequence gap: expected #{expected}, found #{row.chain_seq} (record deleted or reordered)")
        if row.previous_hash != prev:
            return broken(row.chain_seq, "previous_hash does not link to preceding record")
        if compute_hash(row.previous_hash, row.event_json) != row.current_hash:
            return broken(row.chain_seq, "recomputed SHA-256 differs from stored hash (record content altered)")
        try:
            ev = json.loads(row.event_json)
        except ValueError:
            return broken(row.chain_seq, "event_json is not valid JSON")
        if ev.get("crate_id") != crate_id or ev.get("chain_seq") != row.chain_seq:
            return broken(row.chain_seq, "event body does not match its row (record moved between crates/positions)")
        prev = row.current_hash
    return ChainStatus(valid=True, length=len(rows), head_hash=prev if rows else None)
