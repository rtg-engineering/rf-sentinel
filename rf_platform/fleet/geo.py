"""Geometry for the fleet: distances, bearings, and weighted-centroid localization (WCL).

WCL is ported from the Cellular Survey module (rm520n-survey, estimate_cell_location), keeping its uncertainty model:
- each observation pulls the estimate toward the observer in proportion to linear received power (10**(dBm/10));
- uncertainty_radius_m is the signal-weighted RMS distance of the observers from the estimate, inflated when the
  observers cover few bearings around it relative to how many they could (one-sided geometry is a systematic bias,
  not noise, so more observations from the same side do not shrink it).
With fixed nodes, a weighted centroid can only land inside the area the nodes surround: an emitter outside that area
is pulled inward. The radius is "how big a circle to draw before trusting the pin", not a statistical bound.
"""
from __future__ import annotations

import math
from typing import Any, Sequence

EARTH_M = 6_371_000.0
SECTORS = 8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(min(1.0, a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def offset(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    return (lat + math.degrees(north_m / EARTH_M),
            lon + math.degrees(east_m / (EARTH_M * math.cos(math.radians(lat)))))


def compass(deg: float) -> str:
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[int(((deg % 360) + 22.5) // 45) % 8]


def wcl(observations: Sequence[tuple[float, float, float]], min_observations: int = 3) -> dict[str, Any] | None:
    """(lat, lon, rssi_dB) per observer -> {"lat", "lon", "uncertainty_m", "observers", "sector_coverage"} or None."""
    usable = [(la, lo, r) for la, lo, r in observations if r is not None]
    if len(usable) < min_observations:
        return None
    strongest = max(r for _, _, r in usable)
    weights = [10 ** ((r - strongest) / 10.0) for _, _, r in usable]  # relative to the strongest: no underflow
    total = sum(weights)
    lat = sum(w * la for (la, _, _), w in zip(usable, weights)) / total
    lon = sum(w * lo for (_, lo, _), w in zip(usable, weights)) / total
    sq, hits = 0.0, [False] * SECTORS
    for (la, lo, _), w in zip(usable, weights):
        sq += w * haversine_m(lat, lon, la, lo) ** 2
        hits[int(bearing_deg(lat, lon, la, lo) // (360.0 / SECTORS))] = True
    rms = math.sqrt(sq / total)
    coverage = sum(hits) / SECTORS
    ceiling = min(1.0, len(usable) / SECTORS)
    return {"lat": lat, "lon": lon, "uncertainty_m": rms / max(0.15, coverage / ceiling),
            "observers": len(usable), "sector_coverage": round(coverage, 2)}
