from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import ExitStack
from datetime import date
from pathlib import Path

from . import airports
from .availability import CombinedAvailability, FileAvailability, NoAvailability
from .envfile import load_env, mask, read_env, set_env_value
from .aycf_routes import load_network
from .fares import FileFares, build_providers
from .models import Itinerary, PlanResult
from .planner import Planner, SearchOptions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wizz-finder", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_routes = sub.add_parser("routes", help="list All You Can Fly destinations from an airport")
    p_routes.add_argument("airport")
    p_routes.add_argument("--refresh", action="store_true", help="re-download the route network")

    p = sub.add_parser("search", help="rank ways to get from A to B mixing AYCF and other airlines")
    p.add_argument("--from", dest="origins", required=True, help="origin airport(s), e.g. LTN,STN")
    p.add_argument("--to", dest="dests", required=True, help="destination airport(s), e.g. BUD")
    p.add_argument("--date", required=True, type=date.fromisoformat, help="departure day YYYY-MM-DD")
    p.add_argument("--near-from", type=float, default=0, help="also use AYCF airports within N km of origin")
    p.add_argument("--near-to", type=float, default=0, help="also use AYCF airports within N km of destination")
    p.add_argument("--availability", type=Path, help="JSON file with AYCF availability you checked")
    p.add_argument("--portal", action="store_true", help="check AYCF availability live on the Multipass portal")
    p.add_argument("--headed", action="store_true", help="show the browser used for --portal")
    p.add_argument("--portal-delay", type=float, default=2.0, help="seconds between portal requests")
    p.add_argument("--fares", type=Path, help="JSON file with fares you looked up by hand")
    p.add_argument("--fare-source", default="auto",
                   help="comma list of fare sources: ryanair, kiwi, amadeus (default: auto, "
                        "which is ryanair plus any whose keys are in .env). 'none' for no lookups")
    p.add_argument("--no-ryanair", action="store_true", help="shorthand for --fare-source none")
    p.add_argument("--currency", default="GBP")
    p.add_argument("--aycf-fee", type=float, default=9.99, help="flat fee per AYCF leg")
    p.add_argument("--min-connect", type=float, default=3.0, help="minimum self-transfer time, hours")
    p.add_argument("--max-layover", type=float, default=8.0, help="maximum layover, hours")
    p.add_argument("--max-handoffs", type=int, default=10, help="hand-off airports to consider per skeleton")
    p.add_argument("--max-checks", type=int, default=20,
                   help="most AYCF availability checks to make, best prospects first")
    p.add_argument("--max-fare-lookups", type=int, default=40,
                   help="most fare lookups to make across all sources")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--refresh", action="store_true", help="re-download the route network")

    sub.add_parser("login", help="log in to the Multipass portal once and save your subscription id")

    p_id = sub.add_parser("subscription-id", help="show, set, or recover your Multipass subscription id")
    p_id.add_argument("--set", dest="value", help="write this id to .env")
    p_id.add_argument("--from-recording", type=Path, help="read it out of a record-portal .jsonl file")
    p_probe = sub.add_parser("probe", help="run one portal check and print the full request and answer")
    p_probe.add_argument("--from", dest="origin", required=True)
    p_probe.add_argument("--to", dest="dest", required=True)
    p_probe.add_argument("--date", required=True, type=date.fromisoformat)
    p_probe.add_argument("--headed", action="store_true", help="show the browser")
    p_probe.add_argument("--aycf-fee", type=float, default=9.99)
    p_probe.add_argument("--currency", default="GBP")

    sub.add_parser("record-portal", help="open the Multipass portal in a browser and record its API traffic")

    load_env()
    args = parser.parse_args(argv)
    if args.cmd == "login":
        from .portal import interactive_login

        return interactive_login()
    if args.cmd == "subscription-id":
        return cmd_subscription_id(args)
    if args.cmd == "probe":
        return cmd_probe(args)
    if args.cmd == "routes":
        return cmd_routes(args)
    if args.cmd == "search":
        return cmd_search(args)
    if args.cmd == "record-portal":
        from .record_portal import record

        return record()
    return 2


def cmd_probe(args) -> int:
    """One check with full diagnostics, for when the portal rejects everything."""
    from .portal import PortalAvailability

    with PortalAvailability(
        fee=args.aycf_fee,
        currency=args.currency,
        subscription_id=os.environ.get("WIZZ_SUBSCRIPTION_ID") or None,
        email=os.environ.get("WIZZ_EMAIL") or None,
        password=os.environ.get("WIZZ_PASSWORD") or None,
        headless=not args.headed,
        delay_seconds=0,
        verbose=False,
    ) as portal:
        report = portal.probe(args.origin.upper(), args.dest.upper(), args.date)

    from .portal import WRONG_ID_HELP, looks_like_unknown_subscription

    print(json.dumps(report, indent=2, default=str))
    print()
    if report["status"] == 200 and "flightsOutbound" in report["response_body"]:
        print("The portal answered normally, so `search --portal` should work.")
        return 0
    if looks_like_unknown_subscription(report["status"], report["response_body"]):
        print(WRONG_ID_HELP)
        return 1
    if not report["page"]["url"].rstrip("/").endswith("private-page"):
        print("The browser was not on the logged-in portal page. Run `wizz-finder login` first.")
    print("The portal did not answer normally. The response_body above says why;")
    print("no credentials or full ids are in this output, so it is safe to share.")
    return 1


def _suggest_recordings(missing: Path, limit: int = 5) -> None:
    """Point at recordings that do exist, since a doubled or wrong path is the usual slip."""
    seen: list[Path] = []
    for candidate in [Path(missing.name), Path("portal_recordings") / missing.name]:
        if candidate.is_file() and candidate != missing:
            seen.append(candidate)
    for directory in [Path("."), Path("portal_recordings"), Path("..")]:
        for found in sorted(directory.glob("*.jsonl")):
            if found.is_file() and found not in seen and found != missing:
                seen.append(found)
    if not seen:
        print("No .jsonl recordings found nearby either. Make one with `wizz-finder record-portal`,",
              file=sys.stderr)
        print("logging in and searching one route before you close the browser.", file=sys.stderr)
        return
    print("\nDid you mean one of these?", file=sys.stderr)
    for found in seen[:limit]:
        print(f"  wizz-finder subscription-id --from-recording {found}", file=sys.stderr)


def cmd_subscription_id(args) -> int:
    from .portal import UUID_RE, subscription_id_from_recording

    value = args.value
    if args.from_recording:
        path = args.from_recording
        if not path.is_file():
            print(f"No such file: {path}", file=sys.stderr)
            _suggest_recordings(path)
            return 1
        value = subscription_id_from_recording(path)
        if not value:
            print(f"{path} holds no .../json/availability/<id> request.", file=sys.stderr)
            print("A recording only contains the id if you ran a search while recording.", file=sys.stderr)
            return 1
    if value:
        if not UUID_RE.match(value):
            print(f"That does not look like a subscription id: {value!r}", file=sys.stderr)
            print("It should look like 1a2b3c4d-1234-5678-9abc-1a2b3c4d5e6f.", file=sys.stderr)
            return 2
        set_env_value("WIZZ_SUBSCRIPTION_ID", value)
        print(f"Wrote WIZZ_SUBSCRIPTION_ID={mask(value)} to .env")
        return 0

    current = read_env().get("WIZZ_SUBSCRIPTION_ID") or os.environ.get("WIZZ_SUBSCRIPTION_ID")
    if current:
        print(f"WIZZ_SUBSCRIPTION_ID is set ({mask(current)}).")
        return 0
    print("No subscription id yet. Three ways to get one:")
    print("  wizz-finder login                                  log in and search once; it is saved for you")
    print("  wizz-finder subscription-id --from-recording f.jsonl   read it from a record-portal recording")
    print("  wizz-finder subscription-id --set <id>             paste it yourself")
    print()
    print("To find it by hand: open the portal, press F12, open the Network tab, search any")
    print("route, and look for a request under .../json/availability/<id>. The id is that last part.")
    return 1


def cmd_routes(args) -> int:
    net = load_network(refresh=args.refresh)
    code = args.airport.upper()
    dests = sorted(net.destinations(code))
    if not dests:
        print(f"{code} is not in the All You Can Fly network.")
        return 1
    print(f"{code} ({net.name(code)}): {len(dests)} AYCF destinations")
    for d in dests:
        print(f"  {d}  {net.name(d)}")
    return 0


def cmd_search(args) -> int:
    net = load_network(refresh=args.refresh)
    origins = _expand([c.strip().upper() for c in args.origins.split(",")], args.near_from, net)
    dests = _expand([c.strip().upper() for c in args.dests.split(",")], args.near_to, net)
    for code in origins + dests:
        if not airports.known(code):
            print(f"Unknown airport code: {code}", file=sys.stderr)
            return 2

    try:
        fares, notes = build_providers(_fare_sources(args), args.currency)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.fares:
        fares.append(FileFares(args.fares, args.currency))
    for note in notes:
        print(f"Skipping fare source: {note}", file=sys.stderr)
    if fares:
        print(f"Fare sources: {', '.join(f.name for f in fares)}")
    providers = []
    if args.availability:
        providers.append(FileAvailability(args.availability, args.aycf_fee, args.currency))
    with ExitStack() as stack:
        if args.portal:
            from .portal import PortalAvailability

            providers.append(stack.enter_context(PortalAvailability(
                fee=args.aycf_fee,
                currency=args.currency,
                subscription_id=os.environ.get("WIZZ_SUBSCRIPTION_ID") or None,
                email=os.environ.get("WIZZ_EMAIL") or None,
                password=os.environ.get("WIZZ_PASSWORD") or None,
                headless=not args.headed,
                delay_seconds=args.portal_delay,
            )))
        availability = CombinedAvailability(providers) if providers else NoAvailability()
        return _run_search(args, net, origins, dests, fares, availability)


def _run_search(args, net, origins, dests, fares, availability) -> int:
    options = SearchOptions(
        min_connect_hours=args.min_connect,
        max_layover_hours=args.max_layover,
        max_handoffs=args.max_handoffs,
        max_checks=args.max_checks,
        max_fare_lookups=args.max_fare_lookups,
        aycf_fee=args.aycf_fee,
        top=args.top,
    )
    planner = Planner(net, fares, availability, options)
    result = planner.plan(origins, dests, args.date)

    if args.json:
        print(json.dumps(_to_json(result), indent=2, default=str))
    else:
        _print(result, origins, dests, args.date, availability_given=args.availability is not None or args.portal)
    return 0


def _fare_sources(args) -> list[str]:
    """Work out which fare sources to use from --fare-source and what .env holds."""
    if args.no_ryanair or args.fare_source.strip().lower() in {"none", ""}:
        return []
    if args.fare_source.strip().lower() != "auto":
        return [n.strip().lower() for n in args.fare_source.split(",") if n.strip()]
    sources = ["ryanair"]
    if os.environ.get("KIWI_API_KEY"):
        sources.append("kiwi")
    if os.environ.get("AMADEUS_CLIENT_ID") and os.environ.get("AMADEUS_CLIENT_SECRET"):
        sources.append("amadeus")
    return sources


def _expand(codes: list[str], radius_km: float, net) -> list[str]:
    out = list(codes)
    if radius_km > 0:
        for c in codes:
            out += [n for n in airports.nearby(c, radius_km, net.stations) if n not in out]
    return out


def _fmt_td(td) -> str:
    minutes = int(td.total_seconds() // 60)
    return f"{minutes // 60}h{minutes % 60:02d}"


def _print(result: PlanResult, origins, dests, day, availability_given: bool) -> None:
    print(f"Search {','.join(origins)} -> {','.join(dests)} on {day:%a %d %b %Y}")
    print(f"  fare lookups: {result.fare_lookups}, AYCF checks known: {result.checks_done}, "
          f"unknown: {len(result.unknown_checks)}")
    if result.fare_lookups_skipped:
        print(f"  {result.fare_lookups_skipped} fare lookups skipped by the lookup budget; "
              f"raise it with --max-fare-lookups")
    if result.skipped_groups:
        print(f"  {result.skipped_groups} further route combinations left unchecked by the check "
              f"budget; raise it with --max-checks")
    print()

    if result.itineraries:
        print("Options (cheapest first):")
        for i, it in enumerate(result.itineraries, 1):
            _print_itinerary(i, it)
    else:
        print("No complete itineraries yet.")

    if result.unknown_groups:
        print(f"\nAYCF availability still to check on multipass.wizzair.com "
              f"({len(result.unknown_checks)} route-days, best options first):")
        for g in result.unknown_groups:
            print(f"  {g.label}")
            for route, days in _by_route(g.checks).items():
                print(f"      {route} on {', '.join(days)}")
        if not availability_given:
            print("\nRecord what you find in a JSON file (see examples/availability.example.json) "
                  "and re-run with --availability that_file.json.")


def _by_route(checks) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for c in checks:
        out.setdefault(f"{c.origin} -> {c.dest}", []).append(f"{c.day:%a %d %b}")
    return out


def _print_itinerary(n: int, it: Itinerary) -> None:
    legs = " AYCF" * sum(l.aycf for l in it.legs)
    print(f"{n:>2}. {it.total_price:>8.2f} {it.currency}  total {_fmt_td(it.total_duration)}  "
          f"{len(it.legs)} leg{'s' if len(it.legs) > 1 else ''}{legs}")
    for leg, layover in zip(it.legs, it.layovers + [None]):
        print(f"       {leg.describe()}")
        if layover is not None:
            print(f"       ... {_fmt_td(layover)} at {leg.dest}")


def _to_json(result: PlanResult) -> dict:
    return {
        "itineraries": [
            {
                "total_price": round(it.total_price, 2),
                "currency": it.currency,
                "total_duration_minutes": int(it.total_duration.total_seconds() // 60),
                "legs": [
                    {
                        "from": l.origin, "to": l.dest, "dep": l.dep.isoformat(), "arr": l.arr.isoformat(),
                        "carrier": l.carrier, "flight_no": l.flight_no, "price": l.price, "aycf": l.aycf,
                    }
                    for l in it.legs
                ],
            }
            for it in result.itineraries
        ],
        "unknown_checks": [
            {"from": c.origin, "to": c.dest, "date": c.day.isoformat()} for c in result.unknown_checks
        ],
        "fare_lookups": result.fare_lookups,
        "checks_done": result.checks_done,
    }


def run() -> int:
    try:
        return main()
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(run())
