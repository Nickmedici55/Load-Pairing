"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys

from . import db, geocode, report
from .costing import DEFAULT_DWELL_HOURS
from .mileage import ESTIMATED, MileageService, RoutingError, router_from_env
from .pairing import OBJECTIVE_DUTY, OBJECTIVE_WAIT, PairingConfig, build_trips, plan
from .parsing import ParseError, parse_workbook
from .windows import MIDNIGHT_NO_WINDOW, MIDNIGHT_STRICT, WindowPolicy
from .xlsx import XlsxError, sheet_names

DEFAULT_DC_ZIP = "01020"        # Chicopee MA


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loadpairing", description=__doc__)
    parser.add_argument("--db", help="postgresql:// URL or SQLite path (env LOAD_PAIRING_DB)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="pair the loads on a dispatch sheet")
    plan_parser.add_argument("sheet", help="path to the dispatch .xlsx")
    plan_parser.add_argument("--tab", default="3", help="tab number or sheet name (default 3)")
    plan_parser.add_argument("--carrier", default=None, help="only loads for this carrier ID, e.g. PTAG")
    plan_parser.add_argument("--header-row", type=int, default=6, help="zero-based header row (default 6)")
    plan_parser.add_argument("--dc-zip", default=DEFAULT_DC_ZIP, help=f"DC ZIP (default {DEFAULT_DC_ZIP})")
    plan_parser.add_argument(
        "--router",
        default="auto",
        choices=["auto", "pcmiler", "google", "here", ESTIMATED],
        help="mileage source (default auto: whichever API key is set, else estimated)",
    )
    plan_parser.add_argument("--centroids", help="CSV of zip,lat,lon for the offline estimate")
    plan_parser.add_argument("--max-duty", type=float, default=14.0, help="duty limit in hours")
    plan_parser.add_argument("--max-drive", type=float, default=11.0, help="drive limit in hours")
    plan_parser.add_argument(
        "--match-equipment",
        action="store_true",
        help="only pair loads wanting the same trailer type",
    )
    plan_parser.add_argument("--no-windows", action="store_true", help="ignore delivery windows")
    plan_parser.add_argument(
        "--midnight",
        default=MIDNIGHT_NO_WINDOW,
        choices=[MIDNIGHT_NO_WINDOW, MIDNIGHT_STRICT],
        help="how to read a 00:00-00:00 window (default: treat it as no window)",
    )
    plan_parser.add_argument("--earliest-start", type=float, default=0.0, help="earliest dispatch hour")
    plan_parser.add_argument("--latest-start", type=float, default=24.0, help="latest dispatch hour")
    plan_parser.add_argument(
        "--objective",
        default=OBJECTIVE_DUTY,
        choices=[OBJECTIVE_DUTY, OBJECTIVE_WAIT],
        help="tie-break once driver count is minimised (default: pack the fullest shifts)",
    )
    plan_parser.add_argument(
        "--matcher", default="auto", choices=["auto", "networkx", "builtin"], help=argparse.SUPPRESS
    )
    plan_parser.add_argument("--schedule", action="store_true", help="print every stop on the clock")
    plan_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")

    serve_parser = subparsers.add_parser("serve", help="run the web front end for local work")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=None, help="defaults to $PORT, else 8000")

    sheets_parser = subparsers.add_parser("sheets", help="list the tabs in a workbook")
    sheets_parser.add_argument("sheet")

    locations_parser = subparsers.add_parser("locations", help="inspect and edit per-location dwell")
    location_subparsers = locations_parser.add_subparsers(dest="action", required=True)
    location_subparsers.add_parser("list", help="list known locations")
    dwell_parser = location_subparsers.add_parser("dwell", help="set the dwell for one ZIP")
    dwell_parser.add_argument("zip")
    dwell_parser.add_argument("hours", type=float)
    coords_parser = location_subparsers.add_parser("coords", help="fill in missing coordinates")
    coords_parser.add_argument("--csv", help="CSV of zip,lat,lon")

    lanes_parser = subparsers.add_parser("lanes", help="inspect the cached mileage")
    lane_subparsers = lanes_parser.add_subparsers(dest="action", required=True)
    lane_subparsers.add_parser("list", help="list every cached lane")
    lane_subparsers.add_parser("estimated", help="list lanes still on offline mileage")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            return _plan(args)
        if args.command == "serve":
            from .web import serve

            serve(host=args.host, port=args.port)
            return 0
        if args.command == "sheets":
            for index, name in enumerate(sheet_names(args.sheet), start=1):
                print(f"{index}. {name}")
            return 0
        if args.command == "locations":
            return _locations(args)
        if args.command == "lanes":
            return _lanes(args)
    except (ParseError, XlsxError, RoutingError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _plan(args) -> int:
    tab: int | str = int(args.tab) if str(args.tab).isdigit() else args.tab
    parsed = parse_workbook(args.sheet, tab=tab, carrier_id=args.carrier, header_row=args.header_row)
    if not parsed.loads:
        print("error: no loads matched", file=sys.stderr)
        return 1

    store = db.connect(args.db)
    store.ensure_locations(
        [
            db.Location(zip=stop.zip, city=stop.city, state=stop.state)
            for load in parsed.loads
            for stop in load.stops
        ]
        + [db.Location(zip=args.dc_zip)]
    )

    router = router_from_env(args.router)
    if router.name == ESTIMATED:
        centroids = geocode.read_centroids(args.centroids) if args.centroids else None
        geocode.fill_coordinates(store, centroids)

    mileage = MileageService(store, router)
    mileage.refresh()
    dwell = store.dwell_hours()

    config = PairingConfig(
        dc_zip=args.dc_zip,
        max_duty_hours=args.max_duty,
        max_drive_hours=args.max_drive,
        match_equipment=args.match_equipment,
        objective=args.objective,
        matcher=args.matcher,
        windows=WindowPolicy(
            enforce=not args.no_windows,
            midnight=args.midnight,
            earliest_start=args.earliest_start,
            latest_start=args.latest_start,
        ),
    )

    trips = build_trips(
        parsed.loads,
        miles_for=mileage.miles,
        dwell_for=lambda zip_code: dwell.get(zip_code, DEFAULT_DWELL_HOURS),
        config=config,
    )
    result = plan(trips, config)

    if args.json:
        print(json.dumps(report.as_dict(result), indent=2))
    else:
        print(f"{parsed.sheet_name}: {len(parsed.loads)} loads, {parsed.stop_count} stops")
        if mileage.fetched:
            print(f"fetched {mileage.fetched} new lanes from {router.name}")
        for lane, reason in mileage.failures.items():
            print(f"warning: {lane[0]}->{lane[1]} fell back to an estimate ({reason})", file=sys.stderr)
        print()
        print(report.render(result, show_schedule=args.schedule))
    store.close()
    return 0


def _locations(args) -> int:
    store = db.connect(args.db)
    if args.action == "list":
        for location in store.locations():
            coords = (
                f"{location.lat:.4f},{location.lon:.4f}"
                if location.lat is not None and location.lon is not None
                else "no coords"
            )
            where = ", ".join(p for p in (location.city, location.state) if p) or "-"
            print(f"{location.zip}  {location.dwell_hours:>5.2f} h  {where:<28} {coords}")
    elif args.action == "dwell":
        if store.set_dwell(args.zip, args.hours):
            print(f"{args.zip} dwell set to {args.hours:g} h")
        else:
            print(f"error: {args.zip} is not a known location", file=sys.stderr)
            return 1
    elif args.action == "coords":
        centroids = geocode.read_centroids(args.csv) if args.csv else None
        filled = geocode.fill_coordinates(store, centroids)
        print(f"filled coordinates for {filled} locations")
    store.close()
    return 0


def _lanes(args) -> int:
    store = db.connect(args.db)
    lanes = store.estimated_lanes() if args.action == "estimated" else store.lanes()
    for lane in sorted(lanes, key=lambda l: (l.from_zip, l.to_zip)):
        print(f"{lane.from_zip} -> {lane.to_zip}  {lane.miles:>7.1f} mi  {lane.source}")
    if args.action == "estimated" and lanes:
        print(f"\n{len(lanes)} lanes are still estimated; backfill them with a routing source")
    store.close()
    return 0
