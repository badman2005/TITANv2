"""SQLAlchemy 2.0 ORM models. Mirrors schema.sql (use schema.sql for MariaDB)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, Double, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

DT = DateTime().with_variant(mysql.DATETIME(fsp=3), "mysql")
BIGPK = BigInteger().with_variant(Integer(), "sqlite")


class MovementStatus:
    AT_BASE, IN_TRANSIT, AT_BATTLEFIELD = "AT_BASE", "IN_TRANSIT", "AT_BATTLEFIELD"
    IN_STORAGE, DISPENSED = "IN_STORAGE", "DISPENSED"


class IntegrityStatus:
    SAFE = "SAFE"
    THERMAL, TAMPER, HASH = "COMPROMISED_THERMAL", "COMPROMISED_TAMPER", "HASH_MISMATCH"


class ItemStatus:
    AVAILABLE, QUARANTINED, EXPIRED, DISPENSED = "AVAILABLE", "QUARANTINED", "EXPIRED", "DISPENSED"


class DispatchStatus:
    PLANNED, EN_ROUTE, ARRIVED, CANCELLED = "PLANNED", "EN_ROUTE", "ARRIVED", "CANCELLED"


class Base(DeclarativeBase):
    pass


class MilitaryBase(Base):
    __tablename__ = "military_bases"
    base_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    base_code: Mapped[str] = mapped_column(String(16), unique=True)
    base_name: Mapped[str] = mapped_column(String(128))
    sector_or_region: Mapped[str] = mapped_column(String(64))
    latitude: Mapped[float] = mapped_column(Double)
    longitude: Mapped[float] = mapped_column(Double)
    geofence_radius_m: Mapped[int] = mapped_column(Integer, default=500)


class Dispatch(Base):
    __tablename__ = "dispatches"
    dispatch_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_callsign: Mapped[str] = mapped_column(String(32))
    driver_name: Mapped[str] = mapped_column(String(96))
    origin_base_id: Mapped[int] = mapped_column(ForeignKey("military_bases.base_id"))
    destination_base_id: Mapped[int] = mapped_column(ForeignKey("military_bases.base_id"))
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DT, nullable=True)
    arrived_at: Mapped[Optional[datetime]] = mapped_column(DT, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=DispatchStatus.PLANNED)

    origin_base: Mapped[MilitaryBase] = relationship(foreign_keys=[origin_base_id])
    destination_base: Mapped[MilitaryBase] = relationship(foreign_keys=[destination_base_id])


class Crate(Base):
    __tablename__ = "crates"
    crate_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    qr_code_payload: Mapped[str] = mapped_column(String(255), unique=True)
    current_dispatch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("dispatches.dispatch_id"), nullable=True)
    current_base_id: Mapped[Optional[int]] = mapped_column(ForeignKey("military_bases.base_id"), nullable=True)
    movement_status: Mapped[str] = mapped_column(String(16), default=MovementStatus.AT_BASE, index=True)
    integrity_status: Mapped[str] = mapped_column(String(24), default=IntegrityStatus.SAFE)
    is_locked_out: Mapped[bool] = mapped_column(Boolean, default=False)
    lockout_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    locked_out_at: Mapped[Optional[datetime]] = mapped_column(DT, nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DT, nullable=True)

    dispatch: Mapped[Optional[Dispatch]] = relationship(foreign_keys=[current_dispatch_id])
    current_base: Mapped[Optional[MilitaryBase]] = relationship(foreign_keys=[current_base_id])
    items: Mapped[list["MedicalItem"]] = relationship(back_populates="crate")


class MedicalItem(Base):
    __tablename__ = "medical_items"
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    crate_id: Mapped[int] = mapped_column(ForeignKey("crates.crate_id"), index=True)
    item_name: Mapped[str] = mapped_column(String(128))
    item_type: Mapped[str] = mapped_column(String(16), index=True)
    batch_number: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer)
    unit: Mapped[str] = mapped_column(String(16))
    expiry_date: Mapped[date] = mapped_column(Date, index=True)
    item_status: Mapped[str] = mapped_column(String(16), default=ItemStatus.AVAILABLE)

    crate: Mapped[Crate] = relationship(back_populates="items")


class TelemetryReading(Base):
    __tablename__ = "telemetry_readings"
    __table_args__ = (
        UniqueConstraint("crate_id", "device_id", "sequence", name="uq_reading_seq"),
        Index("ix_reading_crate_time", "crate_id", "recorded_at"),
    )
    reading_id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    crate_id: Mapped[int] = mapped_column(ForeignKey("crates.crate_id"))
    dispatch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    device_id: Mapped[str] = mapped_column(String(64))
    sequence: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DT)
    received_at: Mapped[datetime] = mapped_column(DT)
    latitude: Mapped[float] = mapped_column(Double)
    longitude: Mapped[float] = mapped_column(Double)
    temperature_c: Mapped[float] = mapped_column(Double)
    humidity_pct: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    battery_pct: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    ldr_tamper: Mapped[bool] = mapped_column(Boolean, default=False)
    mkt_device_c: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    mkt_server_c: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    violations: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class AuditTrail(Base):
    """Append-only. Never UPDATE or DELETE (enforced by triggers in schema.sql)."""
    __tablename__ = "audit_trail"
    __table_args__ = (UniqueConstraint("crate_id", "chain_seq", name="uq_audit_crate_seq"),)
    audit_id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    crate_id: Mapped[int] = mapped_column(ForeignKey("crates.crate_id"))
    chain_seq: Mapped[int] = mapped_column(Integer)
    dispatch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    event_json: Mapped[str] = mapped_column(Text)
    previous_hash: Mapped[str] = mapped_column(String(64))
    current_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DT)


class AnchorReceipt(Base):
    __tablename__ = "anchor_receipts"
    anchor_id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    crate_id: Mapped[int] = mapped_column(ForeignKey("crates.crate_id"), index=True)
    chain_length: Mapped[int] = mapped_column(Integer)
    head_hash: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    network: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ipfs_cid: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tx_hash: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    anchored_at: Mapped[datetime] = mapped_column(DT)
