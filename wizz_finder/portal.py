"""Live All You Can Fly availability from the Multipass portal, via a real browser.

The portal is a logged-in web app. Its search calls
    POST /w6/subscriptions/json/availability/<subscription-id>
    {"flightType": "OW", "origin": "LTN", "destination": "AYT", "departure": "2026-09-09"}
and answers {"flightsOutbound": [...], "flightsInbound": [...]}. Each outbound entry
carries a key like "W95331 LTN#20260909T1500~AYT#20260909T2135" with local times.

We drive Chromium with a persistent profile so the login survives between runs, and
issue the search from inside the page so cookies and CSRF handling are the browser's.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from pathlib import Path

from . import airports
from .models import AYCF_CARRIER, AycfCheck, Flight

PORTAL_URL = "https://multipass.wizzair.com/w6/subscriptions"
PROFILE_DIR = Path(".cache/portal_profile")
CACHE_PATH = Path(".cache/aycf_availability.json")
KEY_RE = re.compile(r"^(?P<code>\S+)\s+(?P<o>[A-Z]{3})#(?P<dep>\d{8}T\d{4})~(?P<d>[A-Z]{3})#(?P<arr>\d{8}T\d{4})$")
SUB_ID_RE = re.compile(r"json/availability/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse_availability(payload: dict, origin: str, dest: str, fee: float, currency: str) -> list[Flight]:
    flights: list[Flight] = []
    for entry in payload.get("flightsOutbound") or []:
        m = KEY_RE.match(entry.get("key", ""))
        if m:
            dep = datetime.strptime(m["dep"], "%Y%m%dT%H%M")
            arr = datetime.strptime(m["arr"], "%Y%m%dT%H%M")
            code = m["code"]
        else:
            dep = _from_text(entry["departureDateIso"], entry["departure"])
            arr = _from_text(entry.get("arrivalDateIso") or entry["departureDateIso"], entry["arrival"])
            code = entry.get("flightCode", "W6")
        o = entry.get("departureStationCode") or origin
        d = entry.get("arrivalStationCode") or dest
        dep_local, arr_local = airports.localize(dep, o), airports.localize(arr, d)
        flights.append(Flight(o, d, dep_local, arr_local, AYCF_CARRIER, code, fee, currency))
    return flights


def _from_text(day_iso: str, clock: str) -> datetime:
    return datetime.strptime(f"{day_iso} {clock.strip().upper()}", "%Y-%m-%d %I:%M %p")


class PortalAvailability:
    """AvailabilityProvider backed by the live portal. Use as a context manager."""

    def __init__(
        self,
        fee: float,
        currency: str,
        subscription_id: str | None = None,
        email: str | None = None,
        password: str | None = None,
        headless: bool = True,
        delay_seconds: float = 2.0,
        profile_dir: Path = PROFILE_DIR,
        cache_path: Path = CACHE_PATH,
        cache_ttl_minutes: float = 30,
    ):
        self.fee, self.currency = fee, currency
        self.subscription_id = subscription_id
        self.email, self.password = email, password
        self.headless, self.delay = headless, delay_seconds
        self.profile_dir, self.cache_path, self.ttl = profile_dir, cache_path, cache_ttl_minutes * 60
        self.requests_made = 0
        self._cache = self._load_cache()
        self._pw = self._browser = self._page = None

    # ---- lifecycle --------------------------------------------------------

    def __enter__(self) -> "PortalAvailability":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        self._pw = self._browser = self._page = None

    def _open(self):
        if self._page:
            return self._page
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch_persistent_context(str(self.profile_dir), headless=self.headless)
        self._page = self._browser.pages[0] if self._browser.pages else self._browser.new_page()
        self._page.goto(PORTAL_URL, wait_until="domcontentloaded")
        self._ensure_logged_in()
        if not self.subscription_id:
            self.subscription_id = self._discover_subscription_id()
        return self._page

    # ---- login ------------------------------------------------------------

    def _logged_in(self) -> bool:
        page = self._page
        if "openid-connect" in page.url or "login-actions" in page.url:
            return False
        return bool(page.evaluate("() => (window.CVO && window.CVO.hasUserInfo) || false"))

    def _ensure_logged_in(self) -> None:
        page = self._page
        if self._logged_in():
            return
        if "openid-connect" not in page.url and "login-actions" not in page.url:
            page.goto(PORTAL_URL + "/auth/login", wait_until="domcontentloaded")
        if self.email and self.password:
            self._fill_login_form()
        elif self.headless:
            raise RuntimeError(
                "Not logged in to the Multipass portal. Either run `wizz-finder login` once "
                "(interactive browser) or set WIZZ_EMAIL and WIZZ_PASSWORD in .env."
            )
        else:
            print("Log in to the Multipass portal in the browser window...")
        self._wait_until_logged_in()

    def _fill_login_form(self) -> None:
        page = self._page
        user = page.locator('input[name="username"], input[type="email"], #username').first
        pwd = page.locator('input[name="password"], input[type="password"], #password').first
        user.wait_for(timeout=20000)
        user.fill(self.email)
        pwd.fill(self.password)
        page.locator('#kc-login, button[type="submit"], input[type="submit"]').first.click()

    def _wait_until_logged_in(self, timeout_s: float = 300) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self._page.wait_for_timeout(1000)
            if self._logged_in():
                return
        raise RuntimeError("Timed out waiting for the Multipass login to complete.")

    # ---- subscription id --------------------------------------------------

    def _discover_subscription_id(self) -> str:
        page = self._page
        m = SUB_ID_RE.search(page.content())
        if m:
            return m.group(1)
        found = page.evaluate(
            """() => {
                const c = window.CVO || {};
                const cands = [c.currentPlan, c.subscriptionId, c.subscription, c.userInfo];
                return JSON.stringify(cands);
            }"""
        )
        for token in re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", found or ""):
            return token
        raise RuntimeError(
            "Could not find your subscription id on the portal page. Copy it from the portal's "
            "availability URL (…/json/availability/<id>) into WIZZ_SUBSCRIPTION_ID in .env."
        )

    # ---- the check ----------------------------------------------------------

    def available_flights(self, check: AycfCheck) -> list[Flight] | None:
        key = f"{check.origin}-{check.dest}-{check.day.isoformat()}"
        hit = self._cache.get(key)
        if hit and time.time() - hit["at"] < self.ttl:
            return parse_availability(hit["payload"], check.origin, check.dest, self.fee, self.currency)
        payload = self._search(check.origin, check.dest, check.day)
        if payload is None:
            return None
        self._cache[key] = {"at": time.time(), "payload": payload}
        self._save_cache()
        return parse_availability(payload, check.origin, check.dest, self.fee, self.currency)

    def _search(self, origin: str, dest: str, day: date) -> dict | None:
        page = self._open()
        if not UUID_RE.match(self.subscription_id or ""):
            raise RuntimeError(f"Subscription id does not look right: {self.subscription_id!r}")
        body = {"flightType": "OW", "origin": origin, "destination": dest,
                "departure": day.isoformat(), "arrival": "", "intervalSubtype": None}
        if self.requests_made:
            page.wait_for_timeout(int(self.delay * 1000))
        self.requests_made += 1
        result = page.evaluate(
            """async ([url, body]) => {
                const meta = document.querySelector('meta[name="csrf-token"]');
                const csrf = (meta && meta.content) || (window.Laravel && window.Laravel.csrfToken) || '';
                const xsrf = decodeURIComponent((document.cookie.match(/XSRF-TOKEN=([^;]+)/) || [,''])[1]);
                const headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                                 'X-Requested-With': 'XMLHttpRequest'};
                if (csrf) headers['X-CSRF-TOKEN'] = csrf;
                if (xsrf) headers['X-XSRF-TOKEN'] = xsrf;
                const r = await fetch(url, {method: 'POST', headers, credentials: 'same-origin',
                                            body: JSON.stringify(body)});
                const text = await r.text();
                return {status: r.status, text};
            }""",
            [f"{PORTAL_URL}/json/availability/{self.subscription_id}", body],
        )
        if result["status"] != 200:
            print(f"  portal answered HTTP {result['status']} for {origin}->{dest} {day}")
            if result["status"] in (401, 403, 419):
                self._page = None  # force re-login next time
            return None
        try:
            payload = json.loads(result["text"])
        except json.JSONDecodeError:
            return None
        if "flightsOutbound" not in payload:
            return None
        return payload

    # ---- cache --------------------------------------------------------------

    def _load_cache(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache))


def interactive_login(profile_dir: Path = PROFILE_DIR) -> int:
    """Open a visible browser on the portal so you can log in once; the profile keeps the session."""
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(str(profile_dir), headless=False)
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(PORTAL_URL + "/auth/login", wait_until="domcontentloaded")
        print("Log in in the browser window. This tool waits until the portal shows you as logged in.")
        deadline = time.time() + 600
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            if "openid-connect" in page.url or "login-actions" in page.url:
                continue
            try:
                if page.evaluate("() => (window.CVO && window.CVO.hasUserInfo) || false"):
                    print("Logged in. Session saved in", profile_dir)
                    browser.close()
                    return 0
            except Exception:
                pass
        print("Gave up waiting for login.")
        browser.close()
        return 1
