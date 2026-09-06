"""The All You Can Fly route network, read from the public Multipass plans endpoint."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

PLANS_URL = "https://multipass.wizzair.com/w6/subscriptions/catalog/plans"
DEFAULT_CACHE = Path(".cache/aycf_network.json")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


class Network:
    """Directed route map: origin -> set of destinations, plus station names."""

    def __init__(self, routes: dict[str, set[str]], names: dict[str, str]):
        self.routes = routes
        self.names = names

    @property
    def stations(self) -> set[str]:
        return set(self.routes) | {d for ds in self.routes.values() for d in ds}

    def has(self, origin: str, dest: str) -> bool:
        return dest in self.routes.get(origin, ())

    def destinations(self, origin: str) -> set[str]:
        return set(self.routes.get(origin, ()))

    def origins(self, dest: str) -> set[str]:
        return {o for o, ds in self.routes.items() if dest in ds}

    def name(self, iata: str) -> str:
        return self.names.get(iata, iata)


def parse_plans(payload: dict) -> Network:
    """Routes from the UNLIMITED (All You Can Fly) plan, names from the top-level station list.

    Station entries come either as {"id": "BUD", "name": "Budapest"} or as a bare "BUD".
    """
    content = payload.get("content", payload)
    names: dict[str, str] = {}
    for entry in content.get("stations", []):
        dep = entry["departureStation"]
        if isinstance(dep, dict):
            names[dep["id"]] = dep["name"]
        for arr in entry["arrivalStations"]:
            if isinstance(arr, dict):
                names[arr["id"]] = arr["name"]

    stations = None
    for plan in content.get("mappedPlans", []):
        if str(plan.get("planId", "")).upper() == "UNLIMITED" and plan.get("stations"):
            stations = plan["stations"]
            break
    if stations is None:
        stations = content["stations"]

    routes: dict[str, set[str]] = {}
    for entry in stations:
        dep = _code(entry["departureStation"])
        for arr in entry["arrivalStations"]:
            routes.setdefault(dep, set()).add(_code(arr))
    return Network(routes, names)


def _code(station) -> str:
    return station["id"] if isinstance(station, dict) else str(station)


def fetch_plans() -> dict:
    resp = requests.get(
        PLANS_URL,
        headers={"User-Agent": UA, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def load_network(cache: Path = DEFAULT_CACHE, max_age_days: float = 7, refresh: bool = False) -> Network:
    if not refresh and cache.exists() and time.time() - cache.stat().st_mtime < max_age_days * 86400:
        payload = json.loads(cache.read_text())
    else:
        payload = fetch_plans()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload))
    return parse_plans(payload)
