import json
from datetime import date, datetime, timedelta
from pathlib import Path

from wizz_finder import airports
from wizz_finder.availability import FileAvailability, NoAvailability
from wizz_finder.aycf_routes import Network, parse_plans
from wizz_finder.models import AYCF_CARRIER, AycfCheck, Flight
from wizz_finder.planner import Planner, SearchOptions

DAY = date(2026, 9, 8)


def flight(o, d, dep, arr, carrier="FR", price=50.0, day=DAY):
    dep_dt = airports.localize(datetime.combine(day, datetime.strptime(dep, "%H:%M").time()), o)
    arr_dt = airports.localize(datetime.combine(day, datetime.strptime(arr, "%H:%M").time()), d)
    if arr_dt < dep_dt:
        arr_dt += timedelta(days=1)
    return Flight(o, d, dep_dt, arr_dt, carrier, carrier + "1", price, "GBP")


class FakeFares:
    name = "fake"

    def __init__(self, flights):
        self.flights = flights
        self.calls = []

    def search(self, o, d, day_from, day_to):
        self.calls.append((o, d))
        return [f for f in self.flights if f.origin == o and f.dest == d and day_from <= f.dep.date() <= day_to]


class DictAvailability:
    def __init__(self, table):
        self.table = table

    def available_flights(self, check):
        return self.table.get((check.origin, check.dest, check.day))


NET = Network(
    routes={"LTN": {"BUD", "SOF"}, "BUD": {"LTN", "TIA", "SOF"}, "SOF": {"LTN", "BUD", "TIA"}, "TIA": {"BUD", "SOF"}},
    names={},
)


def test_reports_unknown_checks_when_nothing_is_known():
    planner = Planner(NET, [FakeFares([])], NoAvailability(), SearchOptions())
    res = planner.plan(["LTN"], ["TIA"], DAY)
    assert res.itineraries == []
    # via BUD and via SOF, both legs, second leg also next day
    assert AycfCheck("LTN", "BUD", DAY) in res.unknown_checks
    assert AycfCheck("BUD", "TIA", DAY + timedelta(days=1)) in res.unknown_checks
    assert AycfCheck("LTN", "TIA", DAY) not in res.unknown_checks  # no such route


def test_aycf_plus_aycf_respects_connection_window():
    avail = DictAvailability({
        ("LTN", "BUD", DAY): [flight("LTN", "BUD", "06:30", "10:10", AYCF_CARRIER, 9.99)],
        ("BUD", "TIA", DAY): [
            flight("BUD", "TIA", "11:00", "12:45", AYCF_CARRIER, 9.99),  # 50 min: too short
            flight("BUD", "TIA", "14:10", "15:55", AYCF_CARRIER, 9.99),  # 4h: fine
            flight("BUD", "TIA", "22:00", "23:45", AYCF_CARRIER, 9.99),  # 11h50: too long
        ],
    })
    planner = Planner(NET, [FakeFares([])], avail, SearchOptions(min_connect_hours=3, max_layover_hours=8))
    res = planner.plan(["LTN"], ["TIA"], DAY)
    assert len(res.itineraries) == 1
    it = res.itineraries[0]
    assert [l.flight_no for l in it.legs] == ["W6-AYCF1", "W6-AYCF1"]
    assert it.legs[1].dep.hour == 14
    assert round(it.total_price, 2) == 19.98
    assert it.layovers[0] == timedelta(hours=4)


def test_other_plus_aycf_only_checks_aycf_where_a_fare_exists():
    fares = FakeFares([flight("LTN", "BUD", "05:55", "09:20", "FR", 20.0)])
    planner = Planner(NET, [fares], NoAvailability(), SearchOptions())
    res = planner.plan(["LTN"], ["TIA"], DAY)
    # BUD has a Ryanair leg so BUD->TIA needs checking; SOF has none so SOF->TIA is
    # only needed via the AYCF+AYCF skeleton (which it is), but LTN->SOF check is still
    # present from AYCF+AYCF. Make sure the fare-gated AYCF+other path did not add junk.
    assert AycfCheck("BUD", "TIA", DAY) in res.unknown_checks
    assert ("LTN", "BUD") in fares.calls and ("LTN", "SOF") in fares.calls


def test_ranking_prefers_cheaper_then_faster():
    avail = DictAvailability({
        ("LTN", "BUD", DAY): [flight("LTN", "BUD", "06:30", "10:10", AYCF_CARRIER, 9.99)],
        ("BUD", "TIA", DAY): [flight("BUD", "TIA", "14:10", "15:55", AYCF_CARRIER, 9.99)],
    })
    fares = FakeFares([
        flight("LTN", "TIA", "08:00", "11:00", "FR", 120.0),           # direct, expensive
        flight("LTN", "BUD", "05:55", "09:20", "FR", 20.0),            # FR + AYCF = 29.99
    ])
    planner = Planner(NET, [fares], avail, SearchOptions())
    res = planner.plan(["LTN"], ["TIA"], DAY)
    prices = [round(it.total_price, 2) for it in res.itineraries]
    assert prices == sorted(prices)
    assert prices[0] == 19.98  # AYCF + AYCF
    assert 29.99 in prices
    assert 120.0 in prices


def test_no_other_plus_other_itineraries():
    fares = FakeFares([
        flight("LTN", "BUD", "05:55", "09:20", "FR", 20.0),
        flight("BUD", "TIA", "13:00", "14:45", "FR", 30.0),
    ])
    planner = Planner(NET, [fares], NoAvailability(), SearchOptions())
    res = planner.plan(["LTN"], ["TIA"], DAY)
    assert all(any(l.aycf for l in it.legs) for it in res.itineraries)


def test_file_availability_and_none_marker(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps([
        {"from": "LTN", "to": "BUD", "date": "2026-09-08", "dep": "06:30", "arr": "10:10", "flight_no": "W62201"},
        {"from": "LTN", "to": "SOF", "date": "2026-09-08", "none": True},
    ]))
    fa = FileAvailability(p, fee=9.99, currency="GBP")
    got = fa.available_flights(AycfCheck("LTN", "BUD", DAY))
    assert len(got) == 1 and got[0].aycf and got[0].price == 9.99
    assert got[0].dep.tzinfo is not None and got[0].arr.utcoffset() != got[0].dep.utcoffset()
    assert fa.available_flights(AycfCheck("LTN", "SOF", DAY)) == []
    assert fa.available_flights(AycfCheck("BUD", "TIA", DAY)) is None


def test_parse_plans_prefers_unlimited_plan():
    payload = {
        "content": {
            "stations": [
                {"departureStation": {"id": "AAA", "name": "A"}, "arrivalStations": [{"id": "BBB", "name": "B"}]},
                {"departureStation": {"id": "LTN", "name": "London Luton"},
                 "arrivalStations": [{"id": "BUD", "name": "Budapest"}]},
            ],
            "mappedPlans": [
                {"planId": "INT-GB", "stations": [{"departureStation": {"id": "XXX", "name": "X"}, "arrivalStations": []}]},
                {"planId": "UNLIMITED", "stations": [
                    {"departureStation": "LTN", "arrivalStations": ["BUD"]}]},
            ],
        }
    }
    net = parse_plans(payload)
    assert net.has("LTN", "BUD") and not net.has("AAA", "BBB")
    assert net.name("BUD") == "Budapest"


def test_nearby_uses_only_network_stations():
    near = airports.nearby("LTN", 100, {"STN", "LGW", "BUD"})
    assert "STN" in near and "LGW" in near and "BUD" not in near


# ---- check and lookup budgets --------------------------------------------------

def test_budget_keeps_the_cheapest_prospects_first():
    fares = FakeFares([
        flight("LTN", "BUD", "05:55", "09:20", "FR", 20.0),   # BUD hand-off: cheap
        flight("LTN", "SOF", "06:00", "10:00", "FR", 250.0),  # SOF hand-off: dear
    ])
    planner = Planner(NET, [fares], NoAvailability(), SearchOptions(max_checks=3))
    res = planner.plan(["LTN"], ["TIA"], DAY)
    assert len(res.unknown_checks) <= 3
    labels = [g.label for g in res.unknown_groups]
    assert labels[0].startswith("Two AYCF legs")  # 19.98 beats anything with a cash leg
    assert res.skipped_groups > 0


def test_budget_never_splits_a_skeleton():
    planner = Planner(NET, [FakeFares([])], NoAvailability(), SearchOptions(max_checks=2))
    res = planner.plan(["LTN"], ["TIA"], DAY)
    # LTN->TIA is not an AYCF route, so every group here needs three checks and none fit
    assert res.unknown_checks == []
    assert res.skipped_groups > 0


def test_a_generous_budget_checks_everything():
    planner = Planner(NET, [FakeFares([])], NoAvailability(), SearchOptions(max_checks=100))
    res = planner.plan(["LTN"], ["TIA"], DAY)
    assert res.skipped_groups == 0 and len(res.unknown_checks) == 6  # via BUD and via SOF


def test_fare_lookup_budget_stops_further_lookups():
    fares = FakeFares([])
    planner = Planner(NET, [fares], NoAvailability(), SearchOptions(max_fare_lookups=2))
    res = planner.plan(["LTN"], ["TIA"], DAY)
    assert res.fare_lookups == 2
    assert res.fare_lookups_skipped > 0
    assert len(fares.calls) == 2


def test_cached_fares_do_not_spend_the_budget_twice():
    fares = FakeFares([flight("LTN", "BUD", "05:55", "09:20", "FR", 20.0)])
    planner = Planner(NET, [fares], NoAvailability(), SearchOptions(max_fare_lookups=50))
    planner.plan(["LTN"], ["TIA"], DAY)
    before = planner.fare_lookups
    planner.plan(["LTN"], ["TIA"], DAY)
    assert planner.fare_lookups == before  # second run answered entirely from the cache
