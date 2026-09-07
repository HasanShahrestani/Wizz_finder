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


# ---- authentication detection -------------------------------------------------

from datetime import date

import pytest

from wizz_finder.portal import PortalAvailability, looks_like_auth_failure


def test_auth_failure_on_auth_statuses():
    assert looks_like_auth_failure(401, "")
    assert looks_like_auth_failure(403, "")
    assert looks_like_auth_failure(419, "")


def test_auth_failure_on_login_html():
    assert looks_like_auth_failure(200, "\n  <!DOCTYPE html><html>login</html>")


def test_auth_failure_on_redirect_uri():
    body = json.dumps({"content": None, "redirectUri": "https://multipass.wizzair.com/w6/subscriptions/auth/login"})
    assert looks_like_auth_failure(200, body)


def test_good_answer_is_not_an_auth_failure():
    assert not looks_like_auth_failure(200, json.dumps(FIXTURE))
    assert not looks_like_auth_failure(200, json.dumps({"flightsOutbound": [], "flightsInbound": []}))
    assert not looks_like_auth_failure(200, json.dumps({"redirectUri": None, "flightsOutbound": []}))
    assert not looks_like_auth_failure(500, "boom")  # a server error is not a login problem


SUB_ID = "9da7635e-870f-45ff-8105-3c66635b08ea"


class FakePage:
    """Stands in for the Playwright page: records posts and replays canned answers."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.posts = 0

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script, args=None):
        self.posts += 1
        return self.answers.pop(0)


def _provider(answers, **kw):
    p = PortalAvailability(fee=9.99, currency="GBP", subscription_id=SUB_ID,
                           cache_path=Path("/nonexistent/cache.json"), delay_seconds=0, **kw)
    p._page = FakePage(answers)
    return p


OK = {"status": 200, "text": json.dumps(FIXTURE)}
EMPTY = {"status": 200, "text": json.dumps({"flightsOutbound": [], "flightsInbound": []})}
DENIED = {"status": 403, "text": ""}


def test_search_does_not_touch_login_when_the_portal_answers(monkeypatch):
    p = _provider([OK])
    monkeypatch.setattr(p, "_log_in", lambda: pytest.fail("should not try to log in"))
    monkeypatch.setattr(p, "_save_cache", lambda: None)
    flights = p.available_flights(AycfCheck("LTN", "AYT", date(2026, 9, 9)))
    assert len(flights) == 1 and p._page.posts == 1


def test_search_logs_in_once_then_retries(monkeypatch):
    p = _provider([DENIED, OK])
    calls = []
    monkeypatch.setattr(p, "_log_in", lambda: calls.append(1))
    monkeypatch.setattr(p, "_save_cache", lambda: None)
    flights = p.available_flights(AycfCheck("LTN", "AYT", date(2026, 9, 9)))
    assert calls == [1] and len(flights) == 1 and p._page.posts == 2


def test_still_denied_after_login_raises_a_useful_error(monkeypatch):
    p = _provider([DENIED, DENIED])
    monkeypatch.setattr(p, "_log_in", lambda: None)
    with pytest.raises(RuntimeError, match="subscription-id"):
        p.available_flights(AycfCheck("LTN", "AYT", date(2026, 9, 9)))


def test_empty_result_means_checked_and_nothing_available(monkeypatch):
    p = _provider([EMPTY])
    monkeypatch.setattr(p, "_log_in", lambda: pytest.fail("should not try to log in"))
    monkeypatch.setattr(p, "_save_cache", lambda: None)
    assert p.available_flights(AycfCheck("LTN", "AYT", date(2026, 9, 9))) == []


def test_bad_subscription_id_is_rejected_before_any_request():
    p = _provider([OK])
    p.subscription_id = "not-a-uuid"
    with pytest.raises(RuntimeError, match="does not look like an id"):
        p.available_flights(AycfCheck("LTN", "AYT", date(2026, 9, 9)))
    assert p._page.posts == 0


def test_missing_subscription_id_points_at_the_login_command(monkeypatch):
    p = _provider([OK])
    p.subscription_id = None
    monkeypatch.setattr("wizz_finder.portal._scrape_subscription_id", lambda page: None)
    with pytest.raises(RuntimeError, match="wizz-finder login"):
        p.available_flights(AycfCheck("LTN", "AYT", date(2026, 9, 9)))


def test_headless_without_credentials_explains_the_options(monkeypatch):
    p = _provider([DENIED], headless=True)
    monkeypatch.setattr(p._page, "goto", lambda *a, **k: None, raising=False)
    with pytest.raises(RuntimeError, match="wizz-finder login"):
        p.available_flights(AycfCheck("LTN", "AYT", date(2026, 9, 9)))
