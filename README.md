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
fares for the non-AYCF legs  (Ryanair, Kiwi, Amadeus, or a hand-made file)
        │  AYCF checks are only listed where a partner fare exists
        ▼
skeletons ranked cheapest-first, checks capped by a budget
        │
        ▼
AYCF availability  (live from the portal, or your answers in a JSON file)
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
| `--max-checks` | 20 | most AYCF checks per run, best prospects first |
| `--max-fare-lookups` | 40 | most fare lookups per run |
| `--fare-source` | auto | which fare sources to use (see below) |
| `--fares fares.json` | | add fares you looked up by hand |
| `--aycf-fee` | 9.99 | flat fee per AYCF leg |
| `--json` | | machine-readable output |

## Fare sources

Ryanair needs no key and is always available. The others widen the search to the rest of
the market; put a key in `.env` and it is picked up automatically.

| source | key needed | coverage |
|---|---|---|
| `ryanair` | none | Ryanair only, cheapest flight per day |
| `kiwi` | `KIWI_API_KEY` | most airlines including low-cost, per day |
| `amadeus` | `AMADEUS_CLIENT_ID` / `_SECRET` | broad airline coverage, free tier |

```bash
wizz-finder search ... --fare-source ryanair,kiwi   # pick explicitly
wizz-finder search ... --fare-source none           # AYCF legs only
```

`--fare-source auto`, the default, uses Ryanair plus whichever of the others have keys.
The run prints which sources are active and says when a key is missing. Only nonstop
fares are used, because the tool builds the connections itself.

### Getting the keys

**Amadeus** (easiest to obtain). Register at
[developers.amadeus.com](https://developers.amadeus.com), open **My Self-Service
Workspace**, create an app, and copy its **API Key** and **API Secret** into
`AMADEUS_CLIENT_ID` and `AMADEUS_CLIENT_SECRET`. New apps start on the **test** host,
whose flight data is a limited sample rather than the live market, so treat its prices as
a wiring check, not as fares. Moving the app to production in the same workspace gives
real data on a free monthly quota; set `AMADEUS_ENV=production` once you have.

**Kiwi** (better coverage of low-cost airlines). Sign up at
[tequila.kiwi.com](https://tequila.kiwi.com), create a solution, and copy its API key into
`KIWI_API_KEY`. Kiwi vets applicants, so access is not guaranteed and approval can take
a while.

Then check both work:

```bash
wizz-finder fare-check
```

It runs one real lookup per configured source and reports what came back. A missing key,
a rejected key, and an unreachable service each say so distinctly — an empty result is
only ever reported as "no flights" when the source genuinely answered with none.

## Budgets

An AYCF check takes a couple of seconds and a fare lookup may cost an API call, so both
are capped. Skeletons are tried cheapest-first: a direct AYCF leg (£10), then two AYCF
legs (£20), then combinations with a cash leg ordered by that fare, with the shortest
detour winning ties. A skeleton is checked whole or not at all, since half its checks
answer nothing. When a budget binds, the run says how much it left out and which flag
raises it.

## Live availability (`--portal`)

The All You Can Fly fare is only visible to a logged-in subscriber, so the tool drives a
real Chromium through Playwright and asks the portal's own search endpoint, one route-day
at a time, with a pause between requests. Answers are cached for 30 minutes.

### One-time setup

```bash
pip install playwright && playwright install chromium
wizz-finder login
```

`login` opens a browser window. Log in, then **run one search for any route on any date**.
That search is what reveals your subscription id: the portal only sends the id when it
actually searches, so logging in alone is not enough. Close the window and the id is
written to `.env` for you. Then:

```bash
wizz-finder search --from LTN --to TIA --date 2026-09-08 --portal
```

### Finding the subscription id another way

`wizz-finder subscription-id` shows whether one is set and lists the options:

```bash
wizz-finder subscription-id --from-recording portal_recordings/<file>.jsonl
wizz-finder subscription-id --set 1a2b3c4d-1234-5678-9abc-1a2b3c4d5e6f
```

To read it off the portal by hand: open the portal, press F12 for developer tools, go to
the Network tab, search any route, and find the request under
`.../w6/subscriptions/json/availability/<id>`. The id is that last part of the URL.

**Only that URL carries it.** The portal's pages are full of other UUIDs that look
identical — the cookie-consent id and your profile id among them — and a wrong one makes
every check fail with `notFound`. The tool recognises that answer and says so.

### Notes

- Credentials live only in `.env`, which is git-ignored, and are used only to fill the
  portal's login form. The saved browser profile in `.cache/` also holds your session:
  treat both as private.
- `--availability file.json` and `--portal` can be combined; the file is consulted first.
- `--headed` shows the browser, which helps when the login flow changes.
- A search can need dozens of checks at roughly two seconds each, so expect it to take a
  few minutes the first time. Each check prints a line as it goes, and answers are cached
  for 30 minutes, so a re-run is fast.

### If the portal rejects the search

The tool never guesses whether you are logged in: it makes the search and reads the
answer. If the portal rejects it, the tool logs in and retries once. When that still
fails it says so rather than hanging. Things to try, in order:

```bash
wizz-finder login                                        # refresh the saved session
wizz-finder search ... --portal --headed                 # watch it, and log in by hand
wizz-finder subscription-id                              # confirm the id that is set
```

If you have more than one subscription, an id belonging to a different one produces a
rejection that no amount of logging in will fix.

When the portal answers but rejects the request itself (an HTTP 400, say), the tool
prints what the portal said, and stops after five rejections in a row rather than making
dozens more. To see one exchange in full:

```bash
wizz-finder probe --from LTN --to BUD --date 2026-09-08 --headed
```

That prints the request, the response status, headers and body, and whether the page
carried a login and a CSRF token. The subscription id is masked and no credentials
appear, so the output is safe to paste into a bug report.

## Limits of this version

- Two legs at most, same-airport connections only.
- Ryanair returns only the cheapest flight per day, not every flight.
- The Kiwi and Amadeus providers are written to their documented APIs and their parsers
  are unit-tested, but neither has been run against a live account yet.
- The live portal check has been built from a recorded session and unit-tested against
  it, but the automatic login form fill and the "no flights" response have not been
  exercised end to end. If something changes on the portal, `wizz-finder record-portal`
  records its traffic so the parser can be updated.
- Prices are assumed to be in one currency (`--currency`, default GBP).

## Tests

```bash
pytest
```
