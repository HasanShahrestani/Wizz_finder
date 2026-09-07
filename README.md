# Wizz Finder

Find the cheapest reasonable way from A to B by mixing **Wizz All You Can Fly (AYCF)**
legs (flat fee, about £10 each) with other airlines' flights.

The tool does the two things that are actually hard:

1. **It works out which few AYCF routes are worth checking.** The AYCF fare is only
   visible to subscribers on the Multipass portal, inside the 72-hour booking window,
   and it is often not there. Checking is the bottleneck, so the planner narrows the
   whole network down to a short, ordered list of (route, day) pairs.
2. **It only combines flights you can actually connect.** Self-transfers need time.
   Every itinerary respects a minimum connection time and a maximum layover, at the
   same airport, with real time zones.

## How it works

```
origins, destinations, date
        │
        ▼
AYCF route map  (public Multipass plans endpoint, cached 7 days)
        │  skeletons: AYCF | other | AYCF+AYCF | other+AYCF | AYCF+other, max 2 legs
        ▼
hand-off airports ranked by geographic detour, top N kept
        │
        ▼
fares for the non-AYCF legs  (Ryanair fare finder, cheapest per day; or a hand-made file)
        │  AYCF checks are only listed where a partner fare exists
        ▼
AYCF availability  (your answers from the portal, in a JSON file)
        │
        ▼
combine → connection filter → rank by price, then duration → top options
```

## Install

```bash
pip install -e ".[dev]"
```

## Use

List where AYCF can take you from an airport:

```bash
wizz-finder routes LTN
```

Search. The first run has no availability data, so it returns the exact AYCF checks to do:

```bash
wizz-finder search --from LTN,STN --to TIA --date 2026-09-08 --near-to 150
```

Either let the tool check them on the portal for you (see **Live availability** below):

```bash
wizz-finder search --from LTN,STN --to TIA --date 2026-09-08 --near-to 150 --portal
```

or check them by hand on multipass.wizzair.com and record what you saw in a file
(see `examples/availability.example.json`):

```json
[
  {"from": "LTN", "to": "BUD", "date": "2026-09-08", "dep": "06:30", "arr": "10:10", "flight_no": "W62201"},
  {"from": "LTN", "to": "TIA", "date": "2026-09-08", "none": true}
]
```

Re-run with it:

```bash
wizz-finder search --from LTN,STN --to TIA --date 2026-09-08 --near-to 150 --availability availability.json
```

Output is a ranked list of options; you pick. Useful flags:

| flag | default | meaning |
|---|---|---|
| `--min-connect` | 3.0 | minimum self-transfer time in hours |
| `--max-layover` | 8.0 | maximum layover in hours |
| `--near-from`, `--near-to` | 0 | also consider AYCF airports within N km |
| `--max-handoffs` | 10 | hand-off airports tried per skeleton |
| `--fares fares.json` | | add fares you looked up by hand (easyJet, Jet2, ...) |
| `--no-ryanair` | | skip the live Ryanair lookup |
| `--aycf-fee` | 9.99 | flat fee per AYCF leg |
| `--json` | | machine-readable output |

## Live availability (`--portal`)

The All You Can Fly fare is only visible to a logged-in subscriber, so the tool drives a
real Chromium through Playwright and asks the portal's own search endpoint, one route-day
at a time, with a pause between requests. Answers are cached for 30 minutes.

```bash
pip install playwright && playwright install chromium
cp .env.example .env            # optional: WIZZ_EMAIL / WIZZ_PASSWORD for automatic login
wizz-finder login               # or: log in once by hand; the browser profile keeps the session
wizz-finder search --from LTN --to TIA --date 2026-09-08 --portal
```

- Credentials live only in `.env`, which is git-ignored, and are used only to fill the
  portal's login form. The saved browser profile in `.cache/` also holds your session:
  treat both as private.
- The subscription id in the search URL is picked up from the portal after login. If that
  fails, set `WIZZ_SUBSCRIPTION_ID` in `.env` (`wizz-finder record-portal` prints it).
- `--availability file.json` and `--portal` can be combined; the file is consulted first.
- `--headed` shows the browser, which helps when the login flow changes.

## Limits of this version

- Two legs at most, same-airport connections only.
- Ryanair is the only automatic fare source, and it returns the cheapest flight per day,
  not every flight. Add other airlines through `--fares`.
- The live portal check has been built from a recorded session and unit-tested against
  it, but the automatic login form fill and the "no flights" response have not been
  exercised end to end. If something changes on the portal, `wizz-finder record-portal`
  records its traffic so the parser can be updated.
- Prices are assumed to be in one currency (`--currency`, default GBP).

## Tests

```bash
pytest
```
