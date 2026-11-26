"""Pydantic v2 request/response models."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

MovementLiteral = Literal["AT_BASE", "IN_TRANSIT", "AT_BATTLEFIELD", "IN_STORAGE", "DISPENSED"]
ItemTypeLiteral = Literal["VACCINE", "BLOOD_PRODUCT", "DRUG", "MEDICAL_SUPPLY"]
ExpiryLiteral = Literal["VALID", "EXPIRING_SOON", "EXPIRED"]

SAFE_VERDICT = "SAFE TO ADMINISTER"
UNSAFE_VERDICT = "COMPROMISED — DO NOT USE"


# ------------------------------------------------------------------ telemetry
class TelemetryIn(BaseModel):
    crate_id: Optional[int] = Field(None, ge=1)
    qr_code_payload: Optional[str] = Field(None, min_length=1, max_length=255)
    device_id: str = Field(..., min_length=1, max_length=64)
    sequence: Optional[int] = Field(None, ge=0, description="Monotonic per device; enables idempotent burst-sync replay")
    recorded_at: datetime = Field(..., description="Device timestamp (ISO-8601). Naive values are treated as UTC.")
    lat: float = Field(..., ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(..., ge=-180, le=180, allow_inf_nan=False)
    temperature_c: float = Field(..., ge=-90, le=80, allow_inf_nan=False)
    humidity_pct: Optional[float] = Field(None, ge=0, le=100)
    battery_pct: Optional[float] = Field(None, ge=0, le=100)
    ldr_tamper: bool = False
    mkt_c: Optional[float] = Field(None, ge=-90, le=80, allow_inf_nan=False, description="Device-computed MKT, if any")

    @model_validator(mode="after")
    def _need_identity(self):
        if self.crate_id is None and not self.qr_code_payload:
            raise ValueError("Provide crate_id or qr_code_payload")
        return self

    model_config = {
        "json_schema_extra": {
            "example": {
                "qr_code_payload": "TITAN-CRATE-0001", "device_id": "EDGE-TITAN-01", "sequence": 42,
                "recorded_at": "2026-11-24T10:15:00Z", "lat": -15.4166, "lng": 28.2834,
                "temperature_c": 4.4, "humidity_pct": 38.0, "battery_pct": 91.5, "ldr_tamper": False,
            }
        }
    }


class TelemetryBatchIn(BaseModel):
    readings: list[TelemetryIn] = Field(..., min_length=1, max_length=1000)


class ViolationOut(BaseModel):
    code: str
    detail: str


class TelemetryResult(BaseModel):
    accepted: bool
    duplicate: bool = False
    out_of_order: bool = False
    crate_id: int
    dispatch_id: Optional[int]
    previous_movement_status: str
    movement_status: str
    integrity_status: str
    is_locked_out: bool
    lockout_reason: Optional[str]
    violations: list[ViolationOut]
    distance_to_origin_m: Optional[float]
    distance_to_destination_m: Optional[float]
    audit_records_appended: int
    audit_head_hash: Optional[str]


class BatchItemResult(BaseModel):
    index: int
    ok: bool
    result: Optional[TelemetryResult] = None
    error: Optional[str] = None


class TelemetryBatchResult(BaseModel):
    processed: int
    duplicates: int
    failed: int
    results: list[BatchItemResult]


# ------------------------------------------------------------------ inventory
class CrateBrief(BaseModel):
    crate_id: int
    qr_code_payload: str
    movement_status: str
    integrity_status: str
    is_locked_out: bool
    lockout_reason: Optional[str]
    last_seen_at: Optional[datetime]


class TransitInfo(BaseModel):
    dispatch_id: int
    vehicle_callsign: str
    driver_name: str
    origin_base_code: str
    destination_base_code: str
    dispatched_at: Optional[datetime]
    latitude: Optional[float]
    longitude: Optional[float]
    last_seen_at: Optional[datetime]


class StorageInfo(BaseModel):
    base_code: Optional[str]
    base_name: Optional[str]
    sector_or_region: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]


class InventoryItem(BaseModel):
    item_id: int
    item_name: str
    item_type: str
    batch_number: str
    quantity: int
    unit: str
    expiry_date: date
    item_status: str
    expiry_status: ExpiryLiteral
    days_to_expiry: int
    integrity_status: str
    is_locked_out: bool
    preliminary_safe: bool = Field(..., description="Quick flag only — authoritative answer is /crates/{id}/verify")
    crate: CrateBrief
    transit: Optional[TransitInfo] = None
    storage: Optional[StorageInfo] = None


class InventoryResponse(BaseModel):
    total: int
    count: int
    limit: int
    offset: int
    items: list[InventoryItem]


# ------------------------------------------------------------------ verification / audit
class ChainStatus(BaseModel):
    valid: bool
    length: int
    head_hash: Optional[str]
    broken_at_seq: Optional[int] = None
    reason: Optional[str] = None


class ItemVerdict(BaseModel):
    item_id: int
    item_name: str
    item_type: str
    batch_number: str
    expiry_date: date
    expiry_status: ExpiryLiteral
    days_to_expiry: int
    item_status: str
    safe_to_administer: bool
    reasons: list[str]


class VerifyResponse(BaseModel):
    crate_id: int
    qr_code_payload: str
    verdict: Literal["SAFE TO ADMINISTER", "COMPROMISED — DO NOT USE"]
    safe: bool
    reasons: list[str]
    warnings: list[str]
    checks: dict[str, str]
    items: list[ItemVerdict]
    chain: ChainStatus
    verified_at: datetime
    verification_audit_hash: str


class AuditRecordOut(BaseModel):
    audit_id: int
    chain_seq: int
    event_type: str
    dispatch_id: Optional[int]
    previous_hash: str
    current_hash: str
    created_at: datetime
    event: dict


class AuditResponse(BaseModel):
    crate_id: int
    chain: ChainStatus
    records: list[AuditRecordOut]


class AnchorOut(BaseModel):
    anchor_id: int
    crate_id: int
    chain_length: int
    head_hash: str
    provider: str
    network: Optional[str]
    ipfs_cid: Optional[str]
    tx_hash: Optional[str]
    is_mock: bool
    anchored_at: datetime


# ------------------------------------------------------------------ bases / crates (used by the PWA)
class BaseOut(BaseModel):
    base_id: int
    base_code: str
    base_name: str
    sector_or_region: str
    latitude: float
    longitude: float
    geofence_radius_m: int


class DispatchBrief(BaseModel):
    dispatch_id: int
    vehicle_callsign: str
    driver_name: str
    origin_base_code: str
    destination_base_code: str
    status: str
    dispatched_at: Optional[datetime]
    arrived_at: Optional[datetime]


class CrateOut(CrateBrief):
    latitude: Optional[float]
    longitude: Optional[float]
    base_code: Optional[str]
    item_count: int
    dispatch: Optional[DispatchBrief] = None
