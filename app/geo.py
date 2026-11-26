"""Haversine distance and geofence classification (pure functions, no DB)."""
from __future__ import annotations

import math
from typing import Iterable, Optional, TypeVar

EARTH_RADIUS_M = 6_371_008.8  # IUGG mean Earth radius

AT_BASE, IN_TRANSIT, AT_BATTLEFIELD = "AT_BASE", "IN_TRANSIT", "AT_BATTLEFIELD"


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def classify_movement(
    d_origin_m: float, origin_radius_m: float, d_dest_m: float, dest_radius_m: float
) -> str:
    """Destination geofence wins if the two overlap."""
    if d_dest_m <= dest_radius_m:
        return AT_BATTLEFIELD
    if d_origin_m <= origin_radius_m:
        return AT_BASE
    return IN_TRANSIT


B = TypeVar("B")


def containing_base(bases: Iterable[B], lat: float, lng: float) -> Optional[B]:
    """Closest base whose geofence contains the point (bases need latitude/longitude/geofence_radius_m)."""
    best, best_d = None, math.inf
    for b in bases:
        d = haversine_m(lat, lng, b.latitude, b.longitude)  # type: ignore[attr-defined]
        if d <= b.geofence_radius_m and d < best_d:  # type: ignore[attr-defined]
            best, best_d = b, d
    return best
