import json
from datetime import timedelta
from pathlib import Path

from wizz_finder.availability import CombinedAvailability, NoAvailability
from wizz_finder.models import AycfCheck
from wizz_finder.portal import parse_availability

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "portal_availability.json").read_text())


def test_parse_portal_flight_uses_local_times_from_key():
    flights = parse_availability(FIXTURE, "LTN", "AYT", 9.99, "GBP")
    assert len(flights) == 1
    f = flights[0]
    assert f.aycf and f.flight_no == "W95331" and f.price == 9.99
    assert (f.dep.hour, f.dep.minute, str(f.dep.tzinfo)) == (15, 0, "Europe/London")
    assert (f.arr.hour, f.arr.minute, str(f.arr.tzinfo)) == (21, 35, "Europe/Istanbul")
    # The portal shows "06h 35m" (naive clock difference); the real elapsed time is 4h35.
    assert f.duration == timedelta(hours=4, minutes=35)


def test_parse_portal_without_key_falls_back_to_text():
    entry = {k: v for k, v in FIXTURE["flightsOutbound"][0].items() if k != "key"}
    f = parse_availability({"flightsOutbound": [entry]}, "LTN", "AYT", 9.99, "GBP")[0]
    assert (f.dep.hour, f.arr.hour, f.arr.minute) == (15, 21, 35)


def test_parse_portal_empty_means_checked_and_none_available():
    assert parse_availability({"flightsOutbound": [], "flightsInbound": []}, "LTN", "AYT", 9.99, "GBP") == []


def test_combined_asks_next_provider_only_when_unknown():
    class Known:
        def available_flights(self, check):
            return []

    combined = CombinedAvailability([NoAvailability(), Known()])
    assert combined.available_flights(AycfCheck("LTN", "AYT", __import__("datetime").date(2026, 9, 9))) == []
