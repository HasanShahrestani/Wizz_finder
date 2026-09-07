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
from .envfile import mask, set_env_value
from .models import AYCF_CARRIER, AycfCheck, Flight

PORTAL_URL = "https://multipass.wizzair.com/w6/subscriptions"
PROFILE_DIR = Path(".cache/portal_profile")
CACHE_PATH = Path(".cache/aycf_availability.json")
KEY_RE = re.compile(r"^(?P<code>\S+)\s+(?P<o>[A-Z]{3})#(?P<dep>\d{8}T\d{4})~(?P<d>[A-Z]{3})#(?P<arr>\d{8}T\d{4})$")
SUB_ID_RE = re.compile(r"json/availability/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


AUTH_STATUSES = {401, 403, 419, 440}


def looks_like_auth_failure(status: int, text: str) -> bool:
    """Did the portal reject this because we are not logged in?

    A logged-out portal answers with an auth status, or with the login page's HTML, or
    with JSON carrying a redirectUri pointing at the login flow.
    """
    if status in AUTH_STATUSES:
        return True
    if text.lstrip().startswith("<"):
        return True
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload, dict) and bool(payload.get("redirectUri"))


def looks_like_unknown_subscription(status: int, text: str) -> bool:
    """Does the portal say the id in the URL is not a subscription of yours?

    A wrong id answers 400 with {"code": "notFound"}. This is the usual outcome of
    copying some other UUID off the portal's pages: the cookie-consent id and the
    profile id both look exactly like a subscription id.
    """
    if status not in (400, 404):
        return False
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    return "notfound" in str(payload.get("code", "")).lower()


WRONG_ID_HELP = (
    "The portal does not recognise that subscription id (it answered notFound).\n"
    "It is almost certainly a different UUID copied off the portal: the cookie-consent\n"
    "id and the profile id look just like a subscription id. Get the real one with:\n"
    "  wizz-finder login                                     log in, then search once\n"
    "  wizz-finder subscription-id --from-recording f.jsonl  from a record-portal file\n"
    "The subscription id is the last part of a .../json/availability/<id> request URL,\n"
    "and nothing else."
)


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
        login_timeout: float = 300,
        verbose: bool = True,
        max_failures: int = 5,
    ):
        self.fee, self.currency = fee, currency
        self.subscription_id = subscription_id
        self.email, self.password = email, password
        self.headless, self.delay = headless, delay_seconds
        self.profile_dir, self.cache_path, self.ttl = profile_dir, cache_path, cache_ttl_minutes * 60
        self.login_timeout = login_timeout
        self.verbose = verbose
        self.max_failures = max_failures
        self.requests_made = 0
        self._failures = 0
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
        """Launch the browser on the portal. Does not verify the login: the search does that."""
        if self._page:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch_persistent_context(str(self.profile_dir), headless=self.headless)
        self._page = self._browser.pages[0] if self._browser.pages else self._browser.new_page()
        self._page.goto(PORTAL_URL, wait_until="domcontentloaded")
        return self._page

    # ---- subscription id --------------------------------------------------

    def _ensure_subscription_id(self) -> str:
        if self.subscription_id:
            if not UUID_RE.match(self.subscription_id):
                raise RuntimeError(
                    f"WIZZ_SUBSCRIPTION_ID does not look like an id: {self.subscription_id!r}\n"
                    "It should look like 1a2b3c4d-1234-5678-9abc-1a2b3c4d5e6f."
                )
            return self.subscription_id
        found = _scrape_subscription_id(self._page)
        if found:
            self.subscription_id = found
            return found
        raise RuntimeError(
            "No subscription id. The portal only reveals it when it runs a search, so:\n"
            "  wizz-finder login          then log in and search any route once\n"
            "  wizz-finder subscription-id   shows the other ways to set it"
        )

    # ---- login ------------------------------------------------------------

    def _log_in(self) -> None:
        """Called only after the portal has actually rejected a search."""
        page = self._page
        page.goto(PORTAL_URL + "/auth/login", wait_until="domcontentloaded")
        if self.email and self.password:
            self._fill_login_form()
            page.wait_for_url(lambda url: "openid-connect" not in url and "login-actions" not in url,
                              timeout=60000)
            page.wait_for_load_state("domcontentloaded")
            return
        if self.headless:
            raise RuntimeError(
                "Not logged in to the Multipass portal (it rejected the search).\n"
                "Fix it in one of these ways:\n"
                "  wizz-finder login                       log in once in a visible browser\n"
                "  wizz-finder search ... --portal --headed  log in as part of this run\n"
                "  put WIZZ_EMAIL and WIZZ_PASSWORD in .env for an automatic login"
            )
        print("\nThe portal is not logged in. Please log in in the browser window; "
              "the search continues by itself once you are through.")
        deadline = time.time() + self.login_timeout
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            url = page.url
            if "openid-connect" not in url and "login-actions" not in url and "multipass.wizzair.com" in url:
                page.wait_for_timeout(2000)
                return
        raise RuntimeError("Gave up waiting for the login in the browser window.")

    def _fill_login_form(self) -> None:
        page = self._page
        user = page.locator('input[name="username"], input[type="email"], #username').first
        pwd = page.locator('input[name="password"], input[type="password"], #password').first
        user.wait_for(timeout=30000)
        user.fill(self.email)
        pwd.fill(self.password)
        page.locator('#kc-login, button[type="submit"], input[type="submit"]').first.click()

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
        """Ask the portal. The answer itself tells us whether we are logged in."""
        self._open()
        self._ensure_subscription_id()

        if self.verbose:
            print(f"  [{self.requests_made + 1}] checking {origin} -> {dest} on {day}", flush=True)
        result = self._post(origin, dest, day)
        if looks_like_auth_failure(result["status"], result["text"]):
            self._log_in()
            result = self._post(origin, dest, day)
            if looks_like_auth_failure(result["status"], result["text"]):
                raise RuntimeError(
                    "The portal still rejects the search after logging in. If you have more than "
                    "one subscription, the id in .env may belong to a different one: check it with "
                    "`wizz-finder subscription-id`."
                )

        if looks_like_unknown_subscription(result["status"], result["text"]):
            raise RuntimeError(WRONG_ID_HELP)

        payload = None
        problem = None
        if result["status"] != 200:
            problem = f"HTTP {result['status']}"
        else:
            try:
                payload = json.loads(result["text"])
            except json.JSONDecodeError:
                problem = "the answer was not JSON"
                payload = None
            else:
                if "flightsOutbound" not in payload:
                    problem = f"no flightsOutbound in the answer (keys: {sorted(payload)[:6]})"
                    payload = None

        if problem is None:
            self._failures = 0
            return payload

        self._failures += 1
        self._report_failure(origin, dest, day, problem, result)
        if self._failures >= self.max_failures:
            raise RuntimeError(
                f"The portal rejected {self._failures} checks in a row ({problem}).\n"
                "Stopping rather than making dozens more failing requests.\n"
                "Run this to see the full request and answer for a single route:\n"
                f"  wizz-finder probe --from {origin} --to {dest} --date {day} --headed"
            )
        return None

    def _report_failure(self, origin: str, dest: str, day: date, problem: str, result: dict) -> None:
        print(f"  portal rejected {origin} -> {dest} on {day}: {problem}")
        if self._failures == 1:
            body = (result.get("text") or "").strip()
            if body:
                print(f"    it said: {body[:400]}")
            print(f"    request was made from {result.get('page_url')}")
        elif self._failures == 2:
            print("    (further failure details suppressed)")

    def probe(self, origin: str, dest: str, day: date) -> dict:
        """One check, with everything needed to work out why the portal is unhappy."""
        self._open()
        sub_id = self._ensure_subscription_id()
        state = self._page.evaluate(
            """() => ({
                url: window.location.href,
                hasUserInfo: !!(window.CVO && window.CVO.hasUserInfo),
                cvoKeys: Object.keys(window.CVO || {}),
                hasCsrfMeta: !!document.querySelector('meta[name="csrf-token"]'),
                hasLaravelToken: !!(window.Laravel && window.Laravel.csrfToken),
                cookieNames: document.cookie.split(';').map(c => c.split('=')[0].trim()).filter(Boolean),
            })"""
        )
        result = self._post(origin, dest, day)
        return {
            "subscription_id": mask(sub_id),
            "url": f"{PORTAL_URL}/json/availability/{mask(sub_id)}",
            "request_body": {"flightType": "OW", "origin": origin, "destination": dest,
                             "departure": day.isoformat(), "arrival": "", "intervalSubtype": None},
            "page": state,
            "status": result["status"],
            "response_headers": result.get("headers", {}),
            "response_body": result.get("text", ""),
        }

    def _post(self, origin: str, dest: str, day: date) -> dict:
        page = self._page
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
                return {status: r.status, text, headers: Object.fromEntries(r.headers.entries())};
            }""",
            [f"{PORTAL_URL}/json/availability/{self.subscription_id}", body],
        )
        result["page_url"] = page.url
        return result

    # ---- cache --------------------------------------------------------------

    def _load_cache(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache))


def subscription_id_from_recording(path: Path) -> str | None:
    """Pull the subscription id out of a `wizz-finder record-portal` recording."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    m = SUB_ID_RE.search(text)
    return m.group(1) if m else None


def interactive_login(profile_dir: Path = PROFILE_DIR, env_path: Path = Path(".env")) -> int:
    """Log in once in a visible browser and capture the subscription id.

    The id only appears when the portal actually searches, so this asks you to run one
    search in the window. The browser profile keeps you logged in for later runs, and the
    id is written to .env.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Run: pip install playwright && playwright install chromium")
        return 1

    profile_dir.mkdir(parents=True, exist_ok=True)
    found: dict[str, str] = {}

    print("A browser window is opening on the Wizz Multipass portal.")
    print("  1. Log in.")
    print("  2. Search any route on any date (the search itself is what reveals your")
    print("     subscription id; you do not have to book anything).")
    print("  3. Close the browser window when the results appear.")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(str(profile_dir), headless=False)
        page = browser.pages[0] if browser.pages else browser.new_page()

        def on_request(request):
            if "id" in found:
                return
            m = SUB_ID_RE.search(request.url)
            if m:
                found["id"] = m.group(1)
                print(f"Captured subscription id {mask(m.group(1))}")

        browser.on("request", on_request)
        page.goto(PORTAL_URL, wait_until="domcontentloaded")

        while browser.pages:
            try:
                page.wait_for_timeout(1000)
            except Exception:
                break
            if "id" not in found:
                sub_id = _scrape_subscription_id(page)
                if sub_id:
                    found["id"] = sub_id
                    print(f"Found subscription id {mask(sub_id)} on the page")
        try:
            browser.close()
        except Exception:
            pass

    print(f"\nBrowser session saved in {profile_dir} (you stay logged in for later runs).")
    if "id" in found:
        set_env_value("WIZZ_SUBSCRIPTION_ID", found["id"], env_path)
        print(f"Wrote WIZZ_SUBSCRIPTION_ID to {env_path}.")
        print("You can now run: wizz-finder search --from LTN --to TIA --date YYYY-MM-DD --portal")
        return 0

    print("\nNo subscription id seen. That happens if no search was run in the window.")
    print("Run `wizz-finder login` again and do one search, or find the id by hand:")
    print("  open the portal, press F12 for developer tools, go to the Network tab, search a")
    print("  route, and click the request named like a long id under .../json/availability/.")
    print("  The id is the last part of that URL. Put it in .env as WIZZ_SUBSCRIPTION_ID=<id>.")
    return 1


def _scrape_subscription_id(page) -> str | None:
    """Best-effort look for the id in the page HTML and the portal's JS state."""
    try:
        m = SUB_ID_RE.search(page.content())
        if m:
            return m.group(1)
        blob = page.evaluate("() => JSON.stringify(window.CVO || {})")
    except Exception:
        return None
    m = SUB_ID_RE.search(blob or "")
    return m.group(1) if m else None
