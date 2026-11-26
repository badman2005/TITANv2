"""Cold-chain, tamper and expiry rules (pure functions)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

from .config import settings

THERMAL = "THERMAL_EXCURSION"
MKT = "MKT_BREACH"
TAMPER = "TAMPER_DETECTED"
CHAIN = "AUDIT_CHAIN_HEAD_MISMATCH"


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str


def mean_kinetic_temperature(temps_c: Iterable[float]) -> Optional[float]:
    """WHO/USP MKT with ΔH = 83.144 kJ/mol  (ΔH/R = 10 000 K). Returns °C."""
    temps = list(temps_c)
    if not temps:
        return None
    k = 10_000.0
    mean_exp = sum(math.exp(-k / (t + 273.15)) for t in temps) / len(temps)
    return k / -math.log(mean_exp) - 273.15


def out_of_range(t: float) -> bool:
    return t < settings.temp_min_c or t > settings.temp_max_c


def evaluate_telemetry(
    temperature_c: float, ldr_tamper: bool, mkt_values: Iterable[Optional[float]]
) -> list[Violation]:
    """Zero-tolerance: a single out-of-range reading, MKT breach or tamper flag is a violation.

    (With zero tolerance the server-side MKT can only breach when a raw reading already did;
    the MKT rule still matters for device-reported MKT that covers history this server never saw.)
    """
    lo, hi = settings.temp_min_c, settings.temp_max_c
    v: list[Violation] = []
    if out_of_range(temperature_c):
        v.append(Violation(THERMAL, f"THERMAL_EXCURSION: {temperature_c:.2f}°C outside {lo:g}–{hi:g}°C"))
    for m in mkt_values:
        if m is not None and out_of_range(m):
            v.append(Violation(MKT, f"MKT_BREACH: MKT {m:.2f}°C outside {lo:g}–{hi:g}°C"))
            break
    if ldr_tamper:
        v.append(Violation(TAMPER, "TAMPER_DETECTED: LDR light-ingress sensor tripped"))
    return v


def integrity_for(violations: list[Violation]) -> str:
    codes = {v.code for v in violations}
    if CHAIN in codes:
        return "HASH_MISMATCH"
    if TAMPER in codes:
        return "COMPROMISED_TAMPER"
    return "COMPROMISED_THERMAL"


def classify_expiry(expiry: date, today: date) -> tuple[str, int]:
    days = (expiry - today).days
    if expiry < today:
        return "EXPIRED", days
    if expiry <= today + timedelta(days=settings.expiring_soon_days):
        return "EXPIRING_SOON", days
    return "VALID", days
