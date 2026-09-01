"""A small web front end for dispatchers.

The spec's core feature is that a location's dwell is inserted at the 1.0 h
default the first time its ZIP appears in an uploaded sheet and is owned by the
dispatcher from then on. That needs somewhere to upload a sheet and somewhere
to edit the dwell, which is what this is.

It is a plain WSGI application with no framework, so it runs under gunicorn in
production and under ``python -m loadpairing.web`` for local work.
"""

from __future__ import annotations

import base64
import hmac
import html
import os
import shutil
import tempfile
import traceback

from . import db, geocode, report
from .costing import DEFAULT_DWELL_HOURS
from .formdata import Form, FormError, read_form
from .mileage import ESTIMATED, MileageService, RoutingError, router_from_env
from .pairing import OBJECTIVE_DUTY, OBJECTIVE_WAIT, PairingConfig, build_trips, plan
from .parsing import ParseError, parse_workbook
from .windows import MIDNIGHT_NO_WINDOW, MIDNIGHT_STRICT, WindowPolicy
from .xlsx import XlsxError, sheet_names

DEFAULT_DC_ZIP = "01020"        # Chicopee MA

STYLE = """
:root { color-scheme: light dark; --ink:#16191d; --dim:#5c6570; --line:#dce0e6;
        --bg:#f6f7f9; --card:#fff; --accent:#1c5d99; --warn:#8a5300; --bad:#9a2d2d; }
@media (prefers-color-scheme: dark) {
  :root { --ink:#e8eaed; --dim:#9aa3ad; --line:#333a42; --bg:#15181b; --card:#1d2126;
          --accent:#7fb3e0; --warn:#d9a441; --bad:#e08585; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; }
header { background:var(--card); border-bottom:1px solid var(--line); padding:14px 24px; }
header h1 { margin:0; font-size:17px; letter-spacing:.2px; }
header nav { margin-top:6px; display:flex; gap:16px; font-size:14px; }
a { color:var(--accent); }
main { max-width:1100px; margin:0 auto; padding:24px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:18px 20px; margin-bottom:18px; }
h2 { font-size:15px; margin:0 0 14px; letter-spacing:.3px; text-transform:uppercase; color:var(--dim); }
label { display:block; font-size:13px; color:var(--dim); margin-bottom:4px; }
input[type=text], input[type=number], select, input[type=file] {
  width:100%; padding:7px 9px; border:1px solid var(--line); border-radius:5px;
  background:var(--bg); color:var(--ink); font:inherit; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:14px; }
.checks { display:flex; flex-wrap:wrap; gap:18px; margin:16px 0 4px; font-size:14px; }
.checks label { display:flex; align-items:center; gap:7px; color:var(--ink); margin:0; }
.checks input { width:auto; }
button { margin-top:16px; padding:9px 20px; border:0; border-radius:5px; background:var(--accent);
         color:#fff; font:inherit; font-weight:600; cursor:pointer; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th { text-align:left; font-weight:600; color:var(--dim); font-size:12px; text-transform:uppercase;
     letter-spacing:.4px; padding:0 10px 6px 0; border-bottom:1px solid var(--line); }
td { padding:7px 10px 7px 0; border-bottom:1px solid var(--line); vertical-align:top; }
tr:last-child td { border-bottom:0; }
.num { font-variant-numeric:tabular-nums; white-space:nowrap; }
.stats { display:flex; flex-wrap:wrap; gap:28px; }
.stat b { display:block; font-size:26px; font-weight:600; font-variant-numeric:tabular-nums; }
.stat span { font-size:12px; color:var(--dim); text-transform:uppercase; letter-spacing:.4px; }
.driver { border:1px solid var(--line); border-radius:6px; margin-bottom:12px; overflow:hidden; }
.driver > .head { display:flex; flex-wrap:wrap; gap:6px 14px; align-items:baseline;
                  padding:10px 14px; background:var(--bg); border-bottom:1px solid var(--line); }
.driver .who { font-weight:600; }
.driver .meta { font-size:13px; color:var(--dim); font-variant-numeric:tabular-nums; }
.driver table { padding:0 14px; }
.driver td, .driver th { padding-left:14px; }
.note { color:var(--warn); font-size:14px; }
.error { color:var(--bad); }
.tag { font-size:11px; padding:1px 6px; border-radius:3px; border:1px solid var(--line); color:var(--dim); }
p.hint { color:var(--dim); font-size:13px; margin:6px 0 0; }
"""


# -- rendering ------------------------------------------------------------


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} - Load Pairing</title><style>{STYLE}</style></head>
<body><header><h1>Load Pairing</h1><nav>
<a href="/">Plan a sheet</a><a href="/locations">Locations</a><a href="/lanes">Lanes</a>
</nav></header><main>{body}</main></body></html>""".encode("utf-8")


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def plan_form(defaults: dict, message: str = "") -> str:
    def value(name, fallback=""):
        return esc(defaults.get(name, fallback))

    def checked(name, fallback=False):
        return " checked" if defaults.get(name, fallback) else ""

    def options(name, choices, fallback):
        chosen = defaults.get(name, fallback)
        return "".join(
            f'<option value="{esc(key)}"{" selected" if key == chosen else ""}>{esc(label)}</option>'
            for key, label in choices
        )

    return f"""{message}
<form class="card" method="post" action="/plan" enctype="multipart/form-data">
<h2>Plan a dispatch sheet</h2>
<div class="grid">
  <div><label for="sheet">Dispatch workbook (.xlsx)</label>
       <input id="sheet" type="file" name="sheet" accept=".xlsx" required></div>
  <div><label for="tab">Tab</label>
       <input id="tab" type="text" name="tab" value="{value('tab', '3')}"></div>
  <div><label for="carrier">Carrier ID</label>
       <input id="carrier" type="text" name="carrier" value="{value('carrier')}" placeholder="all carriers"></div>
  <div><label for="dc_zip">DC ZIP</label>
       <input id="dc_zip" type="text" name="dc_zip" value="{value('dc_zip', DEFAULT_DC_ZIP)}"></div>
  <div><label for="earliest_start">Earliest dispatch hour</label>
       <input id="earliest_start" type="number" step="0.25" min="0" max="24" name="earliest_start"
              value="{value('earliest_start', '4')}"></div>
  <div><label for="max_duty">Duty limit (h)</label>
       <input id="max_duty" type="number" step="0.5" name="max_duty" value="{value('max_duty', '14')}"></div>
  <div><label for="max_drive">Drive limit (h)</label>
       <input id="max_drive" type="number" step="0.5" name="max_drive" value="{value('max_drive', '11')}"></div>
  <div><label for="midnight">A 00:00-00:00 window means</label>
       <select id="midnight" name="midnight">{options('midnight',
         [(MIDNIGHT_NO_WINDOW, 'no window'), (MIDNIGHT_STRICT, 'exactly midnight')], MIDNIGHT_NO_WINDOW)}</select></div>
  <div><label for="objective">Tie-break</label>
       <select id="objective" name="objective">{options('objective',
         [(OBJECTIVE_DUTY, 'pack the fullest shifts'), (OBJECTIVE_WAIT, 'least waiting')], OBJECTIVE_DUTY)}</select></div>
</div>
<div class="checks">
  <label><input type="checkbox" name="enforce_windows"{checked('enforce_windows', True)}> Enforce delivery windows</label>
  <label><input type="checkbox" name="match_equipment"{checked('match_equipment')}> Pair only matching trailer types</label>
</div>
<button type="submit">Build the plan</button>
<p class="hint">Header row 6, stop rows carry a blank Carrier ID, tab 3 is the delivery order.
Every ZIP in the sheet is recorded at a 1.0 h dwell the first time it is seen; adjust it under
<a href="/locations">Locations</a> and it holds from then on.</p>
</form>"""


def render_plan(result, sheet_name: str, load_count: int, stop_count: int, fetched: int, router_name: str) -> str:
    config = result.config
    warnings = []
    if result.estimated_mileage:
        warnings.append(
            "Some lanes use estimated mileage (great-circle x 1.20) rather than a routing source. "
            "Good for shaping the plan, not for dispatching."
        )
    if not config.windows.enforce:
        warnings.append("Delivery windows were ignored for this run.")

    rows = "".join(
        f"<tr><td>{esc(', '.join(rejection.load_ids))}</td><td>{esc(rejection.reason)}</td></tr>"
        for rejection in result.unschedulable
    )
    unschedulable = (
        f'<div class="card"><h2>Unschedulable</h2><table><tr><th>Load</th><th>Why</th></tr>{rows}</table></div>'
        if rows
        else ""
    )

    return f"""<div class="card">
<h2>{esc(sheet_name)} - {load_count} loads, {stop_count} stops</h2>
<div class="stats">
  <div class="stat"><b>{result.drivers}</b><span>drivers</span></div>
  <div class="stat"><b>{len(result.pairs)}</b><span>pairs</span></div>
  <div class="stat"><b>{len(result.solos)}</b><span>solo</span></div>
  <div class="stat"><b>{len(result.unschedulable)}</b><span>unschedulable</span></div>
  <div class="stat"><b>{result.solo_hours:.1f}</b><span>driver hours if unpaired</span></div>
  <div class="stat"><b>{len(result.candidates)}</b><span>feasible pairs</span></div>
</div>
<p class="hint">Limits {config.max_duty_hours:g} h duty and {config.max_drive_hours:g} h drive;
windows {'enforced' if config.windows.enforce else 'ignored'};
equipment {'must match' if config.match_equipment else 'may differ'};
mileage from {esc(router_name)}{f', {fetched} new lanes cached' if fetched else ''};
matched by {esc(result.matcher)}.</p>
{''.join(f'<p class="note">{esc(text)}</p>' for text in warnings)}
</div>
<div class="card"><h2>Drivers</h2>{''.join(_driver(i, a) for i, a in enumerate(result.assignments, 1))}</div>
{unschedulable}
<p><a href="/">Plan another sheet</a></p>"""


def _driver(index: int, assignment) -> str:
    loads = " + ".join(
        f"{esc(trip.load.load_id)} <span class=\"tag\">{esc(trip.load.equipment)}</span> "
        f"{trip.miles:.0f} mi"
        for trip in assignment.trips
    )
    waiting = f", {assignment.wait_hours:.1f} h waiting" if assignment.wait_hours > 1e-6 else ""
    stops = "".join(
        f"<tr><td>{esc(s.load_id)}</td>"
        f'<td class="num">{report.clock(s.arrive)} - {report.clock(s.depart)}</td>'
        f"<td>{esc(s.stop)}</td>"
        f'<td class="num">{esc(report.window(s.stop))}</td>'
        f'<td class="num">{f"{s.wait * 60:.0f}m" if s.wait > 1e-6 else ""}</td></tr>'
        for s in assignment.schedule
    )
    return f"""<div class="driver"><div class="head">
<span class="who">Driver {index}</span><span>{loads}</span>
<span class="meta">{report.clock(assignment.start_hour)} - {report.clock(assignment.finish_hour)},
{assignment.duty_hours:.1f} h duty, {assignment.drive_hours:.1f} h drive{waiting}</span>
</div><table><tr><th>Load</th><th>On site</th><th>Stop</th><th>Window</th><th>Wait</th></tr>
{stops}</table></div>"""


def render_locations(store, message: str = "") -> str:
    rows = []
    for location in store.locations():
        where = ", ".join(part for part in (location.city, location.state) if part) or "-"
        coordinates = (
            f"{location.lat:.4f}, {location.lon:.4f}"
            if location.lat is not None and location.lon is not None
            else '<span class="note">missing</span>'
        )
        rows.append(
            f"<tr><td class=\"num\">{esc(location.zip)}</td><td>{esc(where)}</td>"
            f'<td><input class="num" type="number" step="0.25" min="0" max="12" '
            f'name="dwell:{esc(location.zip)}" value="{location.dwell_hours:.2f}"></td>'
            f'<td class="num">{coordinates}</td></tr>'
        )
    body = "".join(rows) or '<tr><td colspan="4">No sheet has been uploaded yet.</td></tr>'

    return f"""{message}
<form class="card" method="post" action="/locations">
<h2>Locations</h2>
<p class="hint">Dwell is what the tool bills for time on the dock at each stop. A ZIP arrives here at
1.0 h the first time it appears in an uploaded sheet; what you set below is kept and never
overwritten by a later upload.</p>
<table><tr><th>ZIP</th><th>Where</th><th>Dwell (h)</th><th>Coordinates</th></tr>{body}</table>
<button type="submit">Save dwell</button>
</form>
<form class="card" method="post" action="/coordinates" enctype="multipart/form-data">
<h2>Coordinates</h2>
<p class="hint">Only the offline mileage estimate needs these. A routing API key
(PC*Miler, Google or HERE) makes them unnecessary. Upload a CSV of
<code>zip,lat,lon</code>, or submit with no file to try pgeocode if it is installed.</p>
<input type="file" name="centroids" accept=".csv">
<button type="submit">Fill in coordinates</button>
</form>"""


def render_lanes(store) -> str:
    lanes = sorted(store.lanes(), key=lambda lane: (lane.from_zip, lane.to_zip))
    estimated = [lane for lane in lanes if lane.source == ESTIMATED]
    rows = "".join(
        f'<tr><td class="num">{esc(lane.from_zip)} &rarr; {esc(lane.to_zip)}</td>'
        f'<td class="num">{lane.miles:.1f}</td><td>{esc(lane.source)}</td></tr>'
        for lane in lanes
    ) or '<tr><td colspan="3">Nothing cached yet.</td></tr>'

    warning = (
        f'<p class="note">{len(estimated)} of {len(lanes)} lanes are still estimated. '
        "Set a routing API key and they will be replaced as they are re-fetched.</p>"
        if estimated
        else ""
    )
    return f"""<div class="card"><h2>Cached lanes</h2>
<p class="hint">Every lane is fetched once and kept for good.</p>{warning}
<table><tr><th>Lane</th><th>Miles</th><th>Source</th></tr>{rows}</table></div>"""


# -- request handling -----------------------------------------------------


def authorized(environ) -> bool:
    """True unless ``LOAD_PAIRING_PASSWORD`` is set and the request lacks it.

    A deployment reachable from the open internet should set that variable;
    with it unset the app is open, which is fine behind a private network.
    """
    password = os.environ.get("LOAD_PAIRING_PASSWORD")
    if not password:
        return True

    header = environ.get("HTTP_AUTHORIZATION", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, supplied = base64.b64decode(encoded).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False

    expected_user = os.environ.get("LOAD_PAIRING_USER", "dispatch")
    return hmac.compare_digest(user, expected_user) and hmac.compare_digest(supplied, password)


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/") or "/"
    method = environ.get("REQUEST_METHOD", "GET").upper()

    if path == "/healthz":
        return _respond(start_response, "200 OK", b"ok", "text/plain; charset=utf-8")

    if not authorized(environ):
        body = page("Sign in", _message("This deployment needs a password.", "error"))
        start_response(
            "401 Unauthorized",
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("WWW-Authenticate", 'Basic realm="Load Pairing", charset="UTF-8"'),
            ],
        )
        return [body]

    try:
        status, body = _route(path, method, environ)
    except (FormError, ParseError, XlsxError, RoutingError) as exc:
        status, body = "400 Bad Request", page("Problem", _message(str(exc), "error"))
    except Exception:                      # noqa: BLE001 - the last line of defence
        traceback.print_exc()
        status = "500 Internal Server Error"
        body = page("Error", _message("Something went wrong handling that request.", "error"))

    return _respond(start_response, status, body)


def _route(path: str, method: str, environ):
    if path == "/" and method == "GET":
        return "200 OK", page("Plan", plan_form({}))
    if path == "/plan" and method == "POST":
        return _handle_plan(read_form(environ))
    if path == "/locations":
        if method == "GET":
            return _with_store(lambda store: page("Locations", render_locations(store)))
        if method == "POST":
            return _handle_dwell(read_form(environ))
    if path == "/coordinates" and method == "POST":
        return _handle_coordinates(read_form(environ))
    if path == "/lanes" and method == "GET":
        return _with_store(lambda store: page("Lanes", render_lanes(store)))
    if path in ("/", "/plan", "/locations", "/lanes", "/coordinates"):
        return "405 Method Not Allowed", page("Not allowed", _message("That method is not allowed here.", "error"))
    return "404 Not Found", page("Not found", _message("No such page.", "error"))


def _with_store(render):
    store = db.connect()
    try:
        return "200 OK", render(store)
    finally:
        store.close()


def _respond(start_response, status: str, body: bytes, content_type: str = "text/html; charset=utf-8"):
    start_response(status, [("Content-Type", content_type), ("Content-Length", str(len(body)))])
    return [body]


def _message(text: str, kind: str = "note") -> str:
    return f'<div class="card"><p class="{kind}">{esc(text)}</p></div>'


def _settings(form: Form) -> dict:
    return {
        "tab": form.get("tab", "3") or "3",
        "carrier": form.get("carrier"),
        "dc_zip": form.get("dc_zip", DEFAULT_DC_ZIP) or DEFAULT_DC_ZIP,
        "earliest_start": form.get("earliest_start", "4"),
        "max_duty": form.get("max_duty", "14"),
        "max_drive": form.get("max_drive", "11"),
        "midnight": form.get("midnight", MIDNIGHT_NO_WINDOW),
        "objective": form.get("objective", OBJECTIVE_DUTY),
        "enforce_windows": form.checked("enforce_windows"),
        "match_equipment": form.checked("match_equipment"),
    }


def _handle_plan(form: Form):
    settings = _settings(form)
    upload = form.file("sheet")
    if upload is None:
        return "400 Bad Request", page(
            "Plan", plan_form(settings, _message("Choose a dispatch workbook to upload.", "error"))
        )

    workspace = tempfile.mkdtemp(prefix="loadpairing-")
    path = os.path.join(workspace, "upload.xlsx")
    with open(path, "wb") as handle:
        handle.write(upload.value)

    try:
        tab = int(settings["tab"]) if settings["tab"].isdigit() else settings["tab"]
        parsed = parse_workbook(path, tab=tab, carrier_id=settings["carrier"] or None)
        if not parsed.loads:
            tabs = ", ".join(sheet_names(path))
            return "400 Bad Request", page(
                "Plan",
                plan_form(
                    settings,
                    _message(
                        f"No loads matched on tab {settings['tab']}"
                        + (f" for carrier {settings['carrier']}." if settings["carrier"] else ".")
                        + f" Tabs in this workbook: {tabs}.",
                        "error",
                    ),
                ),
            )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    store = db.connect()
    try:
        store.ensure_locations(
            [db.Location(zip=stop.zip, city=stop.city, state=stop.state)
             for load in parsed.loads for stop in load.stops]
            + [db.Location(zip=settings["dc_zip"])]
        )

        router = router_from_env(os.environ.get("LOAD_PAIRING_ROUTER", "auto"))
        if router.name == ESTIMATED:
            geocode.fill_coordinates(store)

        mileage = MileageService(store, router)
        mileage.refresh()
        dwell = store.dwell_hours()

        config = PairingConfig(
            dc_zip=settings["dc_zip"],
            max_duty_hours=form.number("max_duty", 14.0),
            max_drive_hours=form.number("max_drive", 11.0),
            match_equipment=settings["match_equipment"],
            objective=settings["objective"],
            windows=WindowPolicy(
                enforce=settings["enforce_windows"],
                midnight=settings["midnight"],
                earliest_start=form.number("earliest_start", 0.0),
            ),
        )
        trips = build_trips(
            parsed.loads,
            miles_for=mileage.miles,
            dwell_for=lambda zip_code: dwell.get(zip_code, DEFAULT_DWELL_HOURS),
            config=config,
        )
        result = plan(trips, config)
        body = render_plan(
            result, parsed.sheet_name, len(parsed.loads), parsed.stop_count, mileage.fetched, router.name
        )
    except RoutingError as exc:
        return "400 Bad Request", page(
            "Plan",
            plan_form(
                settings,
                _message(f"{exc}. Upload a zip,lat,lon CSV on the Locations page.", "error"),
            ),
        )
    finally:
        store.close()

    return "200 OK", page("Plan", body)


def _handle_dwell(form: Form):
    store = db.connect()
    try:
        changed = 0
        for name, field in form.items():
            if not name.startswith("dwell:"):
                continue
            zip_code = name.split(":", 1)[1]
            try:
                hours = float(field.text)
            except ValueError:
                continue
            existing = store.location(zip_code)
            if existing is not None and abs(existing.dwell_hours - hours) > 1e-9:
                store.set_dwell(zip_code, hours)
                changed += 1
        note = _message(
            f"Saved {changed} dwell change{'' if changed == 1 else 's'}."
            if changed
            else "No dwell values changed."
        )
        return "200 OK", page("Locations", render_locations(store, note))
    finally:
        store.close()


def _handle_coordinates(form: Form):
    store = db.connect()
    try:
        centroids = None
        upload = form.file("centroids")
        if upload is not None:
            workspace = tempfile.mkdtemp(prefix="loadpairing-")
            path = os.path.join(workspace, "centroids.csv")
            with open(path, "wb") as handle:
                handle.write(upload.value)
            try:
                centroids = geocode.read_centroids(path)
            finally:
                shutil.rmtree(workspace, ignore_errors=True)

        filled = geocode.fill_coordinates(store, centroids)
        note = _message(
            f"Filled in coordinates for {filled} location{'' if filled == 1 else 's'}."
            if filled
            else "No coordinates could be filled in. Upload a zip,lat,lon CSV, or set a routing API key."
        )
        return "200 OK", page("Locations", render_locations(store, note))
    finally:
        store.close()


def serve(host: str = "0.0.0.0", port: int | None = None) -> None:
    """Run the development server. Production uses gunicorn."""
    from wsgiref.simple_server import make_server

    port = port or int(os.environ.get("PORT", "8000"))
    with make_server(host, port, application) as server:
        print(f"load pairing on http://{host}:{port}")
        server.serve_forever()


if __name__ == "__main__":       # pragma: no cover
    serve()
