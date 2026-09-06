from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

AYCF_CARRIER = "W6-AYCF"


@dataclass(frozen=True)
class Flight:
    """One flight leg. Times are timezone-aware local times."""

    origin: str
    dest: str
    dep: datetime
    arr: datetime
    carrier: str
    flight_no: str
    price: float
    currency: str

    @property
    def aycf(self) -> bool:
        return self.carrier == AYCF_CARRIER

    @property
    def duration(self) -> timedelta:
        return self.arr - self.dep

    def describe(self) -> str:
        tag = "AYCF" if self.aycf else self.carrier
        return (
            f"{self.origin} {self.dep:%a %d %b %H:%M} -> {self.dest} {self.arr:%H:%M} "
            f"[{tag} {self.flight_no}] {self.price:.2f} {self.currency}"
        )


@dataclass
class Itinerary:
    legs: list[Flight]

    @property
    def total_price(self) -> float:
        return sum(leg.price for leg in self.legs)

    @property
    def total_duration(self) -> timedelta:
        return self.legs[-1].arr - self.legs[0].dep

    @property
    def layovers(self) -> list[timedelta]:
        return [b.dep - a.arr for a, b in zip(self.legs, self.legs[1:])]

    @property
    def currency(self) -> str:
        return self.legs[0].currency

    def sort_key(self) -> tuple[float, timedelta]:
        return (round(self.total_price, 2), self.total_duration)


@dataclass(frozen=True)
class AycfCheck:
    """A single 'is the All You Can Fly fare open on this route and day' question."""

    origin: str
    dest: str
    day: date

    def __str__(self) -> str:
        return f"{self.origin} -> {self.dest} on {self.day:%a %d %b %Y}"


@dataclass
class CheckGroup:
    """The AYCF checks one skeleton needs, e.g. 'Two AYCF legs via BUD'."""

    label: str
    checks: list[AycfCheck]


@dataclass
class PlanResult:
    itineraries: list[Itinerary]
    unknown_checks: list[AycfCheck] = field(default_factory=list)
    unknown_groups: list[CheckGroup] = field(default_factory=list)
    checks_done: int = 0
    fare_lookups: int = 0
