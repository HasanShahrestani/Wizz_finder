"""Fare sources for the non-Wizz legs.

Every provider answers: which flights fly origin->dest on days in [day_from, day_to]
and what do they cost. Providers may return only the cheapest flight per day.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import requests

from . import airports
from .models import Flight

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


class FareProvider(Protocol):
    name: str

    def search(self, origin: str, dest: str, day_from: date, day_to: date) -> list[Flight]: ...


class RyanairFares:
    """Ryanair's public fare finder: the cheapest flight per day for a whole month.

    One request per (route, month). Not every flight of the day, just the cheapest.
    """

    name = "ryanair"
    URL = "https://www.ryanair.com/api/farfnd/v4/oneWayFares/{o}/{d}/cheapestPerDay"

    def __init__(self, currency: str = "GBP", cache_dir: Path = Path(".cache/ryanair"), ttl_hours: float = 6):
        self.currency = currency
        self.cache_dir = cache_dir
        self.ttl = ttl_hours * 3600
        self.requests_made = 0

    def search(self, origin: str, dest: str, day_from: date, day_to: date) -> list[Flight]:
        flights: list[Flight] = []
        for month in _months(day_from, day_to):
            payload = self._month(origin, dest, month)
            if payload is None:
                continue
            flights.extend(parse_ryanair_month(payload, origin, dest))
        return [f for f in flights if day_from <= f.dep.date() <= day_to]

    def _month(self, origin: str, dest: str, month: date) -> dict | None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache = self.cache_dir / f"{origin}-{dest}-{month:%Y-%m}-{self.currency}.json"
        if cache.exists() and time.time() - cache.stat().st_mtime < self.ttl:
            return json.loads(cache.read_text())
        url = self.URL.format(o=origin, d=dest)
        params = {"outboundMonthOfDate": month.isoformat(), "currency": self.currency}
        self.requests_made += 1
        try:
            resp = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=30)
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            # Ryanair answers 404 for routes it does not fly.
            cache.write_text("{}")
            return None
        cache.write_text(resp.text)
        return resp.json()


def parse_ryanair_month(payload: dict, origin: str, dest: str) -> list[Flight]:
    out: list[Flight] = []
    for fare in payload.get("outbound", {}).get("fares", []):
        if not fare.get("price") or fare.get("unavailable") or fare.get("soldOut"):
            continue
        dep = airports.localize(datetime.fromisoformat(fare["departureDate"]), origin)
        arr = airports.localize(datetime.fromisoformat(fare["arrivalDate"]), dest)
        out.append(
            Flight(
                origin=origin,
                dest=dest,
                dep=dep,
                arr=arr,
                carrier="FR",
                flight_no="FR",
                price=float(fare["price"]["value"]),
                currency=fare["price"]["currencyCode"],
            )
        )
    return out


class FileFares:
    """Fares you looked up by hand (easyJet, Jet2, a Wizz cash fare...).

    Entries: {"from": "LGW", "to": "BUD", "date": "2026-09-08", "dep": "07:00",
              "arr": "10:35", "carrier": "U2", "flight_no": "U28321", "price": 62.5}
    """

    name = "file"

    def __init__(self, path: Path, currency: str):
        self.flights: list[Flight] = []
        for e in json.loads(Path(path).read_text()):
            o, d = e["from"].upper(), e["to"].upper()
            day = date.fromisoformat(e["date"])
            dep = airports.localize(datetime.combine(day, datetime.strptime(e["dep"], "%H:%M").time()), o)
            arr = airports.localize(datetime.combine(day, datetime.strptime(e["arr"], "%H:%M").time()), d)
            if arr < dep or e.get("arr_next_day"):
                arr += timedelta(days=1)
            self.flights.append(
                Flight(o, d, dep, arr, e.get("carrier", "XX"), e.get("flight_no", ""), float(e["price"]),
                       e.get("currency", currency))
            )

    def search(self, origin: str, dest: str, day_from: date, day_to: date) -> list[Flight]:
        return [
            f for f in self.flights
            if f.origin == origin and f.dest == dest and day_from <= f.dep.date() <= day_to
        ]


def _months(day_from: date, day_to: date) -> list[date]:
    months = []
    cur = day_from.replace(day=1)
    while cur <= day_to:
        months.append(cur)
        cur = (cur + timedelta(days=32)).replace(day=1)
    return months
