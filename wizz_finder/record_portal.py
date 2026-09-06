"""Open the Multipass portal in a real browser and save every JSON response it receives.

This is groundwork for automating the availability check: log in, search a route
by hand, and the recording shows which endpoints and payloads the portal uses.
Requires the optional dependency: pip install "wizz-finder[portal]" && playwright install chromium
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

PORTAL_URL = "https://multipass.wizzair.com/w6/subscriptions"
PROFILE_DIR = Path(".cache/portal_profile")
OUT_DIR = Path("portal_recordings")


def record() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Run: pip install playwright && playwright install chromium")
        return 1

    OUT_DIR.mkdir(exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = OUT_DIR / f"{stamp}.jsonl"
    print(f"Recording JSON responses to {log_path}. Log in, search a route, then close the browser.")

    with sync_playwright() as pw, log_path.open("a") as log:
        browser = pw.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False)
        page = browser.pages[0] if browser.pages else browser.new_page()

        def on_response(resp):
            if "json" not in (resp.headers.get("content-type") or ""):
                return
            try:
                body = resp.json()
            except Exception:
                return
            log.write(json.dumps({"url": resp.url, "status": resp.status, "method": resp.request.method,
                                  "post": _safe_post(resp.request), "body": body}) + "\n")
            log.flush()
            print(f"  {resp.status} {resp.request.method} {re.sub(r'\?.*', '?...', resp.url)}")

        browser.on("response", on_response)
        page.goto(PORTAL_URL)
        try:
            while browser.pages:
                page.wait_for_timeout(1000)
        except Exception:
            pass
    print(f"Saved {log_path}")
    return 0


def _safe_post(request):
    try:
        return request.post_data
    except Exception:
        return None
