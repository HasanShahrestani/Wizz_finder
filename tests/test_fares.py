import json
from datetime import date
from pathlib import Path

from wizz_finder.fares import _months, parse_ryanair_month

FIXTURE = Path(__file__).parent / "fixtures" / "ryanair_cheapest_per_day.json"


def test_parse_ryanair_month_skips_unavailable_days():
    flights = parse_ryanair_month(json.loads(FIXTURE.read_text()), "STN", "BUD")
    assert flights and all(f.carrier == "FR" and f.price > 0 for f in flights)
    f = flights[0]
    assert f.dep.tzinfo is not None and str(f.dep.tzinfo) == "Europe/London"
    assert str(f.arr.tzinfo) == "Europe/Budapest"
    assert f.duration.total_seconds() > 0


def test_months_span():
    assert _months(date(2026, 9, 28), date(2026, 10, 2)) == [date(2026, 9, 1), date(2026, 10, 1)]
    assert _months(date(2026, 9, 8), date(2026, 9, 9)) == [date(2026, 9, 1)]
