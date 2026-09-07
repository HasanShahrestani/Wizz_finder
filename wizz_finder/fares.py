"""Fare sources for the non-Wizz legs.

Every provider answers: which flights fly origin->dest on days in [day_from, day_to]
and what do they cost. Providers may return only the cheapest flight per day.
"""
from __future__ import annotations

import json
import os
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


def _cached_json(cache: Path, ttl_seconds: float, fetch) -> dict | None:
    """Read a cached JSON answer, or fetch and store one. None when the fetch failed."""
    if cache.exists() and time.time() - cache.stat().st_mtime < ttl_seconds:
        try:
            return json.loads(cache.read_text())
        except json.JSONDecodeError:
            pass
    payload = fetch()
    if payload is None:
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(payload))
    return payload


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
        self.errors: list[str] = []

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
        except requests.RequestException as exc:
            self.errors.append(f"could not reach Ryanair: {exc}")
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


class KiwiFares:
    """Kiwi.com (Tequila) search: nonstop fares across most airlines, low-cost included.

    Needs a Tequila API key in KIWI_API_KEY. One request per route and day.
    """

    name = "kiwi"
    URL = "https://tequila-api.kiwi.com/v2/search"

    def __init__(self, api_key: str, currency: str = "GBP",
                 cache_dir: Path = Path(".cache/kiwi"), ttl_hours: float = 6, limit: int = 20):
        self.api_key, self.currency, self.limit = api_key, currency, limit
        self.cache_dir, self.ttl = cache_dir, ttl_hours * 3600
        self.requests_made = 0
        self.errors: list[str] = []

    def search(self, origin: str, dest: str, day_from: date, day_to: date) -> list[Flight]:
        flights: list[Flight] = []
        for day in _days(day_from, day_to):
            payload = _cached_json(
                self.cache_dir / f"{origin}-{dest}-{day}-{self.currency}.json",
                self.ttl,
                lambda d=day: self._fetch(origin, dest, d),
            )
            if payload:
                flights.extend(parse_kiwi(payload, self.currency))
        return flights

    def _fetch(self, origin: str, dest: str, day: date) -> dict | None:
        params = {
            "fly_from": origin, "fly_to": dest,
            "date_from": day.strftime("%d/%m/%Y"), "date_to": day.strftime("%d/%m/%Y"),
            "adults": 1, "curr": self.currency, "max_stopovers": 0, "limit": self.limit,
            "vehicle_type": "aircraft",
        }
        self.requests_made += 1
        try:
            resp = requests.get(self.URL, params=params, headers={"apikey": self.api_key}, timeout=30)
        except requests.RequestException as exc:
            self.errors.append(f"could not reach Kiwi: {exc}")
            return None
        if resp.status_code in (401, 403):
            raise RuntimeError(f"Kiwi rejected KIWI_API_KEY (HTTP {resp.status_code}). "
                               "Check the key in .env.")
        if resp.status_code != 200:
            self.errors.append(f"Kiwi answered HTTP {resp.status_code} for {origin}-{dest} {day}")
            return {}
        return resp.json()


def parse_kiwi(payload: dict, currency: str) -> list[Flight]:
    flights: list[Flight] = []
    for offer in payload.get("data", []):
        legs = offer.get("route") or []
        if len(legs) != 1:  # we build our own connections, so only nonstop is useful
            continue
        leg = legs[0]
        o, d = leg["flyFrom"], leg["flyTo"]
        flights.append(Flight(
            origin=o, dest=d,
            dep=airports.localize(_iso_naive(leg["local_departure"]), o),
            arr=airports.localize(_iso_naive(leg["local_arrival"]), d),
            carrier=leg.get("airline", "??"),
            flight_no=f"{leg.get('airline', '')}{leg.get('flight_no', '')}",
            price=float(offer["price"]),
            currency=payload.get("currency", currency),
        ))
    return flights


class AmadeusFares:
    """Amadeus Self-Service flight offers: broad airline coverage on a free tier.

    Needs AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET. Set AMADEUS_ENV=production to
    use the live host instead of the test one, which carries only sample data.
    """

    name = "amadeus"
    HOSTS = {"test": "https://test.api.amadeus.com", "production": "https://api.amadeus.com"}

    def __init__(self, client_id: str, client_secret: str, currency: str = "GBP",
                 environment: str = "test", cache_dir: Path = Path(".cache/amadeus"),
                 ttl_hours: float = 6, limit: int = 20):
        self.client_id, self.client_secret = client_id, client_secret
        self.currency, self.limit = currency, limit
        self.host = self.HOSTS.get(environment, self.HOSTS["test"])
        self.cache_dir, self.ttl = cache_dir, ttl_hours * 3600
        self.requests_made = 0
        self.errors: list[str] = []
        self._token: str | None = None
        self._token_expires = 0.0

    def search(self, origin: str, dest: str, day_from: date, day_to: date) -> list[Flight]:
        flights: list[Flight] = []
        for day in _days(day_from, day_to):
            payload = _cached_json(
                self.cache_dir / f"{origin}-{dest}-{day}-{self.currency}.json",
                self.ttl,
                lambda d=day: self._fetch(origin, dest, d),
            )
            if payload:
                flights.extend(parse_amadeus(payload, self.currency))
        return flights

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        try:
            resp = requests.post(
                f"{self.host}/v1/security/oauth2/token",
                data={"grant_type": "client_credentials",
                      "client_id": self.client_id, "client_secret": self.client_secret},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach Amadeus at {self.host}: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"Amadeus refused the credentials (HTTP {resp.status_code}). "
                "Check AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET in .env."
            )
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + float(data.get("expires_in", 1800)) - 60
        return self._token

    def _fetch(self, origin: str, dest: str, day: date) -> dict | None:
        token = self._access_token()  # outside the try: a bad key must not look like no flights
        params = {
            "originLocationCode": origin, "destinationLocationCode": dest,
            "departureDate": day.isoformat(), "adults": 1,
            "currencyCode": self.currency, "nonStop": "true", "max": self.limit,
        }
        self.requests_made += 1
        try:
            resp = requests.get(
                f"{self.host}/v2/shopping/flight-offers",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        except requests.RequestException as exc:
            self.errors.append(f"could not reach Amadeus: {exc}")
            return None
        if resp.status_code in (401, 403):
            raise RuntimeError(f"Amadeus refused the request (HTTP {resp.status_code}). "
                               "Check the credentials, and whether the app has flight-offers access.")
        if resp.status_code != 200:
            self.errors.append(f"Amadeus answered HTTP {resp.status_code} for {origin}-{dest} {day}")
            return {}
        return resp.json()


def parse_amadeus(payload: dict, currency: str) -> list[Flight]:
    flights: list[Flight] = []
    for offer in payload.get("data", []):
        price = offer.get("price", {})
        total = price.get("grandTotal") or price.get("total")
        for itinerary in offer.get("itineraries", []):
            segments = itinerary.get("segments", [])
            if len(segments) != 1:  # nonstop only; we build connections ourselves
                continue
            seg = segments[0]
            o, d = seg["departure"]["iataCode"], seg["arrival"]["iataCode"]
            flights.append(Flight(
                origin=o, dest=d,
                dep=airports.localize(_iso_naive(seg["departure"]["at"]), o),
                arr=airports.localize(_iso_naive(seg["arrival"]["at"]), d),
                carrier=seg.get("carrierCode", "??"),
                flight_no=f"{seg.get('carrierCode', '')}{seg.get('number', '')}",
                price=float(total),
                currency=price.get("currency", currency),
            ))
    return flights


def _iso_naive(text: str) -> datetime:
    """Parse an ISO timestamp as wall-clock time, ignoring any trailing zone marker.

    Kiwi labels local times with a Z and Amadeus sends none at all; both mean local.
    """
    cleaned = text.strip().replace("Z", "").split("+")[0]
    if "." in cleaned:
        cleaned = cleaned.split(".")[0]
    return datetime.fromisoformat(cleaned)


def _days(day_from: date, day_to: date) -> list[date]:
    span = (day_to - day_from).days
    return [day_from + timedelta(days=i) for i in range(max(span, 0) + 1)]


PROVIDERS = {"ryanair": RyanairFares, "kiwi": KiwiFares, "amadeus": AmadeusFares}


def build_providers(names: list[str], currency: str) -> tuple[list[FareProvider], list[str]]:
    """Make the named providers. Returns them plus notes about any that were skipped."""
    built: list[FareProvider] = []
    notes: list[str] = []
    for name in names:
        if name == "ryanair":
            built.append(RyanairFares(currency=currency))
        elif name == "kiwi":
            key = os.environ.get("KIWI_API_KEY")
            if key:
                built.append(KiwiFares(api_key=key, currency=currency))
            else:
                notes.append("kiwi needs KIWI_API_KEY in .env")
        elif name == "amadeus":
            cid, secret = os.environ.get("AMADEUS_CLIENT_ID"), os.environ.get("AMADEUS_CLIENT_SECRET")
            if cid and secret:
                built.append(AmadeusFares(client_id=cid, client_secret=secret, currency=currency,
                                          environment=os.environ.get("AMADEUS_ENV", "test")))
            else:
                notes.append("amadeus needs AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET in .env")
        else:
            raise ValueError(f"Unknown fare source {name!r}. Known: {', '.join(sorted(PROVIDERS))}, file")
    return built, notes
