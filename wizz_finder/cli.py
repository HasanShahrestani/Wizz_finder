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
from .aycf_routes import load_network
from .fares import FileFares, RyanairFares
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
    p.add_argument("--no-ryanair", action="store_true", help="skip the Ryanair fare lookup")
    p.add_argument("--currency", default="GBP")
    p.add_argument("--aycf-fee", type=float, default=9.99, help="flat fee per AYCF leg")
    p.add_argument("--min-connect", type=float, default=3.0, help="minimum self-transfer time, hours")
    p.add_argument("--max-layover", type=float, default=8.0, help="maximum layover, hours")
    p.add_argument("--max-handoffs", type=int, default=10, help="hand-off airports to consider per skeleton")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--refresh", action="store_true", help="re-download the route network")

    sub.add_parser("login", help="log in to the Multipass portal once in a visible browser")
    sub.add_parser("record-portal", help="open the Multipass portal in a browser and record its API traffic")

    load_env()
    args = parser.parse_args(argv)
    if args.cmd == "login":
        from .portal import interactive_login

        return interactive_login()
    if args.cmd == "routes":
        return cmd_routes(args)
    if args.cmd == "search":
        return cmd_search(args)
    if args.cmd == "record-portal":
        from .record_portal import record

        return record()
    return 2


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

    fares = []
    if not args.no_ryanair:
        fares.append(RyanairFares(currency=args.currency))
    if args.fares:
        fares.append(FileFares(args.fares, args.currency))
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
        top=args.top,
    )
    planner = Planner(net, fares, availability, options)
    result = planner.plan(origins, dests, args.date)

    if args.json:
        print(json.dumps(_to_json(result), indent=2, default=str))
    else:
        _print(result, origins, dests, args.date, availability_given=args.availability is not None or args.portal)
    return 0


def load_env(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines from .env into the environment (existing variables win)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


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
          f"unknown: {len(result.unknown_checks)}\n")

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
