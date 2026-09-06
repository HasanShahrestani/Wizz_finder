"""Airport coordinates and time zones (from the airportsdata package)."""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from math import asin, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo

import airportsdata


@lru_cache(maxsize=1)
def _db() -> dict:
    return airportsdata.load("IATA")


def known(iata: str) -> bool:
    return iata in _db()


def tz(iata: str) -> ZoneInfo:
    return ZoneInfo(_db()[iata]["tz"])


def localize(naive: datetime, iata: str) -> datetime:
    """Attach the airport's local time zone to a naive datetime."""
    return naive.replace(tzinfo=tz(iata))


def coords(iata: str) -> tuple[float, float] | None:
    rec = _db().get(iata)
    if not rec:
        return None
    return rec["lat"], rec["lon"]


def distance_km(a: str, b: str) -> float | None:
    ca, cb = coords(a), coords(b)
    if ca is None or cb is None:
        return None
    lat1, lon1 = map(radians, ca)
    lat2, lon2 = map(radians, cb)
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(h))


def nearby(iata: str, radius_km: float, candidates: set[str]) -> list[str]:
    """Candidates within radius_km of iata, nearest first (iata itself excluded)."""
    found = []
    for c in candidates:
        if c == iata:
            continue
        d = distance_km(iata, c)
        if d is not None and d <= radius_km:
            found.append((d, c))
    return [c for _, c in sorted(found)]


def detour_ratio(origin: str, via: str, dest: str) -> float:
    """How much longer origin->via->dest is than origin->dest. 1.0 = no detour."""
    direct = distance_km(origin, dest)
    a, b = distance_km(origin, via), distance_km(via, dest)
    if direct is None or a is None or b is None:
        return float("inf")
    return (a + b) / max(direct, 1.0)
