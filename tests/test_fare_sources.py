import json
from datetime import date

import pytest

from wizz_finder.fares import _days, build_providers, parse_amadeus, parse_kiwi

KIWI = {
    "currency": "GBP",
    "data": [
        {"price": 41.5, "route": [{
            "flyFrom": "LTN", "flyTo": "MXP", "airline": "W6", "flight_no": 6302,
            "local_departure": "2026-09-08T15:15:00.000Z", "local_arrival": "2026-09-08T18:15:00.000Z"}]},
        {"price": 300.0, "route": [  # a connection: we build our own, so it is ignored
            {"flyFrom": "LTN", "flyTo": "BUD", "airline": "W6", "flight_no": 1,
             "local_departure": "2026-09-08T06:00:00.000Z", "local_arrival": "2026-09-08T09:30:00.000Z"},
            {"flyFrom": "BUD", "flyTo": "MXP", "airline": "W6", "flight_no": 2,
             "local_departure": "2026-09-08T11:00:00.000Z", "local_arrival": "2026-09-08T12:40:00.000Z"}]},
    ],
}

AMADEUS = {
    "data": [
        {"price": {"grandTotal": "62.30", "currency": "GBP"},
         "itineraries": [{"segments": [{
             "departure": {"iataCode": "LTN", "at": "2026-09-08T07:00:00"},
             "arrival": {"iataCode": "MXP", "at": "2026-09-08T10:05:00"},
             "carrierCode": "U2", "number": "8321"}]}]},
        {"price": {"grandTotal": "200.00", "currency": "GBP"},
         "itineraries": [{"segments": [
             {"departure": {"iataCode": "LTN", "at": "2026-09-08T07:00:00"},
              "arrival": {"iataCode": "BUD", "at": "2026-09-08T10:30:00"},
              "carrierCode": "W6", "number": "1"},
             {"departure": {"iataCode": "BUD", "at": "2026-09-08T12:00:00"},
              "arrival": {"iataCode": "MXP", "at": "2026-09-08T13:40:00"},
              "carrierCode": "W6", "number": "2"}]}]},
    ],
}


def test_parse_kiwi_keeps_nonstop_and_localises_times():
    flights = parse_kiwi(KIWI, "GBP")
    assert len(flights) == 1
    f = flights[0]
    assert (f.origin, f.dest, f.carrier, f.flight_no) == ("LTN", "MXP", "W6", "W66302")
    assert f.price == 41.5 and f.currency == "GBP"
    # the Z on Kiwi's local_departure is not UTC: 15:15 is London time, 18:15 Milan time
    assert (f.dep.hour, str(f.dep.tzinfo)) == (15, "Europe/London")
    assert (f.arr.hour, str(f.arr.tzinfo)) == (18, "Europe/Rome")
    assert f.duration.total_seconds() == 2 * 3600


def test_parse_amadeus_keeps_nonstop_and_localises_times():
    flights = parse_amadeus(AMADEUS, "GBP")
    assert len(flights) == 1
    f = flights[0]
    assert (f.origin, f.dest, f.carrier, f.flight_no) == ("LTN", "MXP", "U2", "U28321")
    assert f.price == 62.30
    assert (f.dep.hour, str(f.dep.tzinfo)) == (7, "Europe/London")
    assert (f.arr.hour, str(f.arr.tzinfo)) == (10, "Europe/Rome")


def test_parsers_survive_an_empty_answer():
    assert parse_kiwi({}, "GBP") == [] and parse_kiwi({"data": []}, "GBP") == []
    assert parse_amadeus({}, "GBP") == [] and parse_amadeus({"data": []}, "GBP") == []


def test_days_spans_the_range_inclusively():
    assert _days(date(2026, 9, 8), date(2026, 9, 8)) == [date(2026, 9, 8)]
    assert len(_days(date(2026, 9, 8), date(2026, 9, 10))) == 3


def test_build_providers_reports_missing_keys(monkeypatch):
    monkeypatch.delenv("KIWI_API_KEY", raising=False)
    monkeypatch.delenv("AMADEUS_CLIENT_ID", raising=False)
    built, notes = build_providers(["ryanair", "kiwi", "amadeus"], "GBP")
    assert [p.name for p in built] == ["ryanair"]
    assert any("KIWI_API_KEY" in n for n in notes)
    assert any("AMADEUS_CLIENT_ID" in n for n in notes)


def test_build_providers_uses_keys_when_present(monkeypatch):
    monkeypatch.setenv("KIWI_API_KEY", "k")
    monkeypatch.setenv("AMADEUS_CLIENT_ID", "i")
    monkeypatch.setenv("AMADEUS_CLIENT_SECRET", "s")
    built, notes = build_providers(["kiwi", "amadeus"], "GBP")
    assert [p.name for p in built] == ["kiwi", "amadeus"] and notes == []


def test_build_providers_rejects_an_unknown_name():
    with pytest.raises(ValueError, match="Unknown fare source"):
        build_providers(["easyjet"], "GBP")


# ---- failures must never look like an empty market -----------------------------

import requests

from wizz_finder.fares import AmadeusFares, KiwiFares


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_amadeus_bad_credentials_raise_rather_than_return_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(401))
    provider = AmadeusFares("id", "secret", cache_dir=tmp_path)
    with pytest.raises(RuntimeError, match="refused the credentials"):
        provider.search("LTN", "MXP", date(2026, 9, 8), date(2026, 9, 8))


def test_amadeus_unreachable_raises(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "post", boom)
    provider = AmadeusFares("id", "secret", cache_dir=tmp_path)
    with pytest.raises(RuntimeError, match="Could not reach Amadeus"):
        provider.search("LTN", "MXP", date(2026, 9, 8), date(2026, 9, 8))


def test_amadeus_records_a_network_error_on_the_search(monkeypatch, tmp_path):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(200, {"access_token": "t", "expires_in": 1800}))

    def boom(*a, **k):
        raise requests.ConnectionError("dropped")

    monkeypatch.setattr(requests, "get", boom)
    provider = AmadeusFares("id", "secret", cache_dir=tmp_path)
    assert provider.search("LTN", "MXP", date(2026, 9, 8), date(2026, 9, 8)) == []
    assert provider.errors and "dropped" in provider.errors[0]


def test_kiwi_rejects_a_bad_key_loudly(monkeypatch, tmp_path):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(401))
    provider = KiwiFares("bad-key", cache_dir=tmp_path)
    with pytest.raises(RuntimeError, match="KIWI_API_KEY"):
        provider.search("LTN", "MXP", date(2026, 9, 8), date(2026, 9, 8))


def test_kiwi_records_a_server_error(monkeypatch, tmp_path):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(503))
    provider = KiwiFares("key", cache_dir=tmp_path)
    assert provider.search("LTN", "MXP", date(2026, 9, 8), date(2026, 9, 8)) == []
    assert provider.errors and "503" in provider.errors[0]


def test_a_genuine_empty_answer_records_no_error(monkeypatch, tmp_path):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200, {"data": [], "currency": "GBP"}))
    provider = KiwiFares("key", cache_dir=tmp_path)
    assert provider.search("LTN", "MXP", date(2026, 9, 8), date(2026, 9, 8)) == []
    assert provider.errors == []
