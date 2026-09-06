"""Build and rank itineraries of up to two legs mixing AYCF and other airlines.

Skeletons considered (O = origin airports, D = destination airports, H = hand-off):
  1. O -> D            AYCF
  2. O -> D            other airline
  3. O -> H -> D       AYCF + AYCF
  4. O -> H -> D       other + AYCF
  5. O -> H -> D       AYCF + other
Expensive lookups (AYCF availability, fares) are only made for hand-offs that
survive a geographic detour ranking and, for AYCF checks, that already have a
partner fare on the other leg.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import product

from . import airports
from .availability import AvailabilityProvider
from .aycf_routes import Network
from .fares import FareProvider
from .models import AycfCheck, CheckGroup, Flight, Itinerary, PlanResult


@dataclass
class SearchOptions:
    min_connect_hours: float = 3.0
    max_layover_hours: float = 8.0
    max_handoffs: int = 10
    max_detour_ratio: float = 2.5
    top: int = 10
    include_direct_other: bool = True


@dataclass
class Planner:
    network: Network
    fares: list[FareProvider]
    availability: AvailabilityProvider
    options: SearchOptions = field(default_factory=SearchOptions)

    def __post_init__(self) -> None:
        self._fare_cache: dict[tuple[str, str, date, date], list[Flight]] = {}
        self._avail_cache: dict[AycfCheck, list[Flight] | None] = {}
        self.fare_lookups = 0

    # ---- public -----------------------------------------------------------

    def plan(self, origins: list[str], dests: list[str], day: date) -> PlanResult:
        next_day = day + timedelta(days=1)
        itins: list[Itinerary] = []
        groups: list[CheckGroup] = []

        for o, d in product(origins, dests):
            if o == d:
                continue
            # 1. direct AYCF
            if self.network.has(o, d):
                groups.append(CheckGroup(f"Direct AYCF {o} -> {d}", [AycfCheck(o, d, day)]))
            # 2. direct other airline
            if self.options.include_direct_other:
                itins += [Itinerary([f]) for f in self._fares(o, d, day, day)]
            # 3. AYCF + AYCF
            for h in self._rank(o, d, self.network.destinations(o) & self.network.origins(d)):
                groups.append(CheckGroup(
                    f"Two AYCF legs {o} -> {h} -> {d}",
                    [AycfCheck(o, h, day), AycfCheck(h, d, day), AycfCheck(h, d, next_day)],
                ))
            # 4. other + AYCF: fares first, check AYCF only where a fare exists
            for h in self._rank(o, d, self.network.origins(d) - {o}):
                fares = self._fares(o, h, day, day)
                if fares:
                    groups.append(CheckGroup(
                        f"After {_cheapest(fares)} then AYCF",
                        [AycfCheck(h, d, day), AycfCheck(h, d, next_day)],
                    ))
            # 5. AYCF + other: fares first as well
            for h in self._rank(o, d, self.network.destinations(o) - {d}):
                fares = self._fares(h, d, day, next_day)
                if fares:
                    groups.append(CheckGroup(f"AYCF then {_cheapest(fares)}", [AycfCheck(o, h, day)]))

        needed = list(dict.fromkeys(c for g in groups for c in g.checks))
        unknown = [c for c in needed if self._avail(c) is None]
        unknown_set = set(unknown)
        unknown_groups = []
        for g in groups:
            open_checks = [c for c in g.checks if c in unknown_set]
            if open_checks:
                unknown_groups.append(CheckGroup(g.label, open_checks))

        for o, d in product(origins, dests):
            if o == d:
                continue
            itins += [Itinerary([f]) for f in self._avail_flights(o, d, day)]
            for h in self.network.stations - {o, d}:
                first = self._avail_flights(o, h, day) + self._fares_if_looked_up(o, h, day, day)
                second = (
                    self._avail_flights(h, d, day) + self._avail_flights(h, d, next_day)
                    + self._fares_if_looked_up(h, d, day, next_day)
                )
                if not first or not second:
                    continue
                for a, b in product(first, second):
                    if a.aycf or b.aycf:  # pure other+other is Kiwi's job, not ours
                        if self._connects(a, b):
                            itins.append(Itinerary([a, b]))

        itins.sort(key=Itinerary.sort_key)
        return PlanResult(
            itineraries=itins[: self.options.top],
            unknown_checks=unknown,
            unknown_groups=unknown_groups,
            checks_done=len(needed) - len(unknown),
            fare_lookups=self.fare_lookups,
        )

    # ---- helpers ----------------------------------------------------------

    def _rank(self, o: str, d: str, candidates: set[str]) -> list[str]:
        scored = sorted(
            (airports.detour_ratio(o, h, d), h) for h in candidates if h not in (o, d)
        )
        return [h for r, h in scored if r <= self.options.max_detour_ratio][: self.options.max_handoffs]

    def _fares(self, o: str, d: str, day_from: date, day_to: date) -> list[Flight]:
        key = (o, d, day_from, day_to)
        if key not in self._fare_cache:
            found: list[Flight] = []
            for provider in self.fares:
                self.fare_lookups += 1
                found += provider.search(o, d, day_from, day_to)
            self._fare_cache[key] = found
        return self._fare_cache[key]

    def _fares_if_looked_up(self, o: str, d: str, day_from: date, day_to: date) -> list[Flight]:
        return self._fare_cache.get((o, d, day_from, day_to), [])

    def _avail(self, check: AycfCheck) -> list[Flight] | None:
        if check not in self._avail_cache:
            self._avail_cache[check] = self.availability.available_flights(check)
        return self._avail_cache[check]

    def _avail_flights(self, o: str, d: str, day: date) -> list[Flight]:
        return self._avail_cache.get(AycfCheck(o, d, day)) or []

    def _connects(self, a: Flight, b: Flight) -> bool:
        if a.dest != b.origin:  # same airport only; no cross-town transfers in the MVP
            return False
        gap = b.dep - a.arr
        return timedelta(hours=self.options.min_connect_hours) <= gap <= timedelta(hours=self.options.max_layover_hours)


def _cheapest(fares: list[Flight]) -> str:
    f = min(fares, key=lambda x: x.price)
    return f"{f.carrier} {f.origin} -> {f.dest} from {f.price:.2f} {f.currency}"
