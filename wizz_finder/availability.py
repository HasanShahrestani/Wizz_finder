"""Answering 'is the All You Can Fly fare open on this route and day?'.

The subscriber-only Multipass portal is the only source of truth, so the MVP
reads answers from a JSON file you fill in by hand. The planner tells you the
exact (route, day) pairs worth checking, so the manual work is small.

File format (a list of entries):
  {"from": "LTN", "to": "BUD", "date": "2026-09-08", "dep": "06:30", "arr": "10:10",
   "flight_no": "W62201"}                       -> one available flight
  {"from": "LTN", "to": "BUD", "date": "2026-09-08", "none": true}
                                                -> checked, nothing available
A route/day with no entry at all is reported as "unknown, please check".
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from . import airports
from .models import AYCF_CARRIER, AycfCheck, Flight


class AvailabilityProvider(Protocol):
    def available_flights(self, check: AycfCheck) -> list[Flight] | None:
        """Flights with the AYCF fare open, [] if checked and none, None if unknown."""


class NoAvailability:
    """Knows nothing. Useful to get the list of checks to do."""

    def available_flights(self, check: AycfCheck) -> list[Flight] | None:
        return None


class FileAvailability:
    def __init__(self, path: Path, fee: float, currency: str):
        self.fee = fee
        self.currency = currency
        self._known: dict[tuple[str, str, date], list[Flight]] = {}
        for entry in json.loads(Path(path).read_text()):
            key = (entry["from"].upper(), entry["to"].upper(), date.fromisoformat(entry["date"]))
            flights = self._known.setdefault(key, [])
            if entry.get("none"):
                continue
            flights.append(self._to_flight(entry, key))

    def _to_flight(self, entry: dict, key: tuple[str, str, date]) -> Flight:
        origin, dest, day = key
        dep = airports.localize(datetime.combine(day, _hm(entry["dep"])), origin)
        arr_day = day + timedelta(days=1 if entry.get("arr_next_day") else 0)
        arr = airports.localize(datetime.combine(arr_day, _hm(entry["arr"])), dest)
        if arr < dep and not entry.get("arr_next_day"):
            arr += timedelta(days=1)
        return Flight(
            origin=origin,
            dest=dest,
            dep=dep,
            arr=arr,
            carrier=AYCF_CARRIER,
            flight_no=entry.get("flight_no", "W6"),
            price=self.fee,
            currency=self.currency,
        )

    def available_flights(self, check: AycfCheck) -> list[Flight] | None:
        return self._known.get((check.origin, check.dest, check.day))


def _hm(text: str):
    return datetime.strptime(text.strip(), "%H:%M").time()


class CombinedAvailability:
    """Ask providers in order; the first one that knows the answer wins."""

    def __init__(self, providers: list[AvailabilityProvider]):
        self.providers = providers

    def available_flights(self, check: AycfCheck) -> list[Flight] | None:
        for p in self.providers:
            found = p.available_flights(check)
            if found is not None:
                return found
        return None
