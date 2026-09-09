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
from urllib.parse import parse_qs

import json

from . import arrange as arrange_loads
from . import db, geocode, report, snapshot
from .costing import DEFAULT_DWELL_HOURS
from .formdata import Form, FormError, read_form
from .mileage import ESTIMATED, MileageService, RoutingError, router_from_env
from .pairing import OBJECTIVE_DUTY, OBJECTIVE_WAIT, PairingConfig, build_trips, plan
from .parsing import DEFAULT_CARRIER_ID, ParseError, normalize_zip, parse_workbook
from .windows import WindowPolicy
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
.move { display:inline-flex; align-items:center; gap:6px; margin:0 10px 0 0; font-size:13px;
        color:var(--ink); font-variant-numeric:tabular-nums; }
.move input { width:58px; padding:3px 6px; text-align:center; }
.driver .pad { padding:8px 14px 0; }
.driver .pad .note { margin:0 0 8px; }
button.quiet { margin:0; padding:2px 8px; font-size:12px; font-weight:500;
               background:transparent; color:var(--dim); border:1px solid var(--line); }
button.quiet.spaced { margin-left:10px; }
.driver.empty { border-style:dashed; }
.orders { padding:10px 14px 2px; }
.orders table { font-size:13px; }
.orders td, .orders th { padding-left:0; padding-right:16px; }
.orders .note { font-size:13px; }
.driver.empty .head { border-bottom:0; }
"""


# -- rendering ------------------------------------------------------------


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} - Load Pairing</title><style>{STYLE}</style></head>
<body><header><h1>Load Pairing</h1><nav>
<a href="/">Plan a sheet</a><a href="/plans">Saved plans</a>
<a href="/service-centers">Service centers</a>
<a href="/locations">Locations</a><a href="/lanes">Lanes</a>
</nav></header><main>{body}</main></body></html>""".encode("utf-8")


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def plan_form(defaults: dict, centers: list, message: str = "") -> str:
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
       <input id="carrier" type="text" name="carrier" value="{value('carrier', DEFAULT_CARRIER_ID)}"
              placeholder="blank for every carrier"></div>
  <div><label for="dc_zip">Service center</label>
       <select id="dc_zip" name="dc_zip">{options('dc_zip',
         [(c.zip, c.label) for c in centers], defaults.get('dc_zip', DEFAULT_DC_ZIP))}</select></div>
  <div><label for="earliest_start">No dispatch before</label>
       <input id="earliest_start" type="number" step="0.25" min="0" max="24" name="earliest_start"
              value="{value('earliest_start', '0')}" title="0 lets the delivery times decide"></div>
  <div><label for="max_duty">Duty limit (h)</label>
       <input id="max_duty" type="number" step="0.5" name="max_duty" value="{value('max_duty', '14')}"></div>
  <div><label for="max_drive">Drive limit (h)</label>
       <input id="max_drive" type="number" step="0.5" name="max_drive" value="{value('max_drive', '11')}"></div>
  <div><label for="objective">Tie-break</label>
       <select id="objective" name="objective">{options('objective',
         [(OBJECTIVE_DUTY, 'pack the fullest shifts'), (OBJECTIVE_WAIT, 'least waiting')], OBJECTIVE_DUTY)}</select></div>
</div>
<div class="checks">
  <label><input type="checkbox" name="enforce_windows"{checked('enforce_windows', True)}> Enforce delivery windows</label>
  <label><input type="checkbox" name="match_equipment"{checked('match_equipment')}> Pair only matching trailer types</label>
</div>
<button type="submit">Build the plan</button>
<p class="hint">The driver leaves the DC at whatever hour lands them at the first stop of a turn
exactly as it opens, so leave <em>No dispatch before</em> at 0 unless there is a real hour before
which nobody rolls -- raising it can only make loads unschedulable.
Header row 6, stop rows carry a blank Carrier ID, tab 3 is the delivery order.
Window Close is the delivery time; a 00:00 close means due by 23:59 that night and is treated as a
drop and hook, 30 minutes on the ground. Every other ZIP in the sheet is recorded at a 1.0 h dwell
the first time it is seen; adjust it under <a href="/locations">Locations</a> and it holds from
then on.</p>
</form>"""


def render_workspace(arrangement, baseline, view: dict, message: str = "") -> str:
    """The plan, with the loads movable between drivers.

    ``baseline`` is the plan as the matcher built it. Every rearrangement is
    costed against that, so a dispatcher can see what their change bought or
    cost rather than just what it is.
    """
    config = arrangement.config
    center = view.get("center")
    rearranged = arrangement.groups != baseline.groups

    warnings = []
    if view.get("estimated_mileage"):
        warnings.append(
            "Some lanes use estimated mileage (great-circle x 1.20) rather than a routing source. "
            "Good for shaping the plan, not for dispatching."
        )
    if not config.windows.enforce:
        warnings.append("Delivery windows were ignored for this run.")

    def delta(now: float, before: float, unit: str = "", places: int = 1) -> str:
        """How this arrangement differs from the one the matcher built."""
        if not rearranged or abs(now - before) < 0.05:
            return ""
        return f'<span class="tag">{now - before:+.{places}f}{unit} vs the built plan</span>'

    problems = len(arrangement.problems)
    saved_name = view.get("saved_name", "")
    drivers = "".join(
        _driver(index, driver)
        for index, driver in enumerate(arrangement.drivers, start=1)
    )

    # Empty drivers a dispatcher has added and not filled yet. They are slots
    # on the page rather than part of the plan, and are numbered after it.
    spare_numbers = [
        str(arrangement.driver_count + offset)
        for offset in range(1, view.get("spare", 0) + 1)
    ]
    drivers += "".join(_empty_driver(number) for number in spare_numbers)
    next_free = str(arrangement.driver_count + len(spare_numbers) + 1)

    return f"""{message}
<div class="card">
<h2>{esc(view.get("sheet_name", "Plan"))} - {view.get("load_count", 0)} loads,
{view.get("stop_count", 0)} stops</h2>
<p class="hint">Out of {esc(center.label if center else config.dc_zip)}.</p>
<div class="stats">
  <div class="stat"><b>{arrangement.driver_count}</b><span>drivers
      {delta(arrangement.driver_count, baseline.driver_count, places=0)}</span></div>
  <div class="stat"><b>{arrangement.duty_hours:.1f}</b><span>hours on duty
      {delta(arrangement.duty_hours, baseline.duty_hours, " h")}</span></div>
  <div class="stat"><b>{arrangement.wait_hours:.1f}</b><span>hours waiting
      {delta(arrangement.wait_hours, baseline.wait_hours, " h")}</span></div>
  <div class="stat"><b>{problems}</b><span>drivers with a problem</span></div>
  <div class="stat"><b>{view.get("solo_hours", 0.0):.1f}</b><span>driver hours if unpaired</span></div>
</div>
<p class="hint">Limits {config.max_duty_hours:g} h duty and {config.max_drive_hours:g} h drive;
windows {'enforced' if config.windows.enforce else 'ignored'};
equipment {'must match' if config.match_equipment else 'may differ'};
mileage from {esc(view.get("router_name", "?"))}
{f', {view["fetched"]} new lanes cached' if view.get("fetched") else ''}.</p>
{''.join(f'<p class="note">{esc(text)}</p>' for text in warnings)}
</div>

<form method="post" action="/arrange">
<input type="hidden" name="loads" value="{esc(view.get("loads_json", "[]"))}">
<input type="hidden" name="settings" value="{esc(view.get("settings_json", "{{}}"))}">
<input type="hidden" name="baseline" value="{esc(view.get("baseline_json", "[]"))}">
<input type="hidden" name="spare" value="{esc(",".join(spare_numbers))}">
<div class="card">
<h2>Drivers</h2>
<p class="hint">The number beside a load is the driver running it, and it is the whole mechanism:
<strong>give two loads the same number</strong> to put them on one driver, or
<strong>a number nobody else has</strong> to give a load a driver of its own - {esc(next_free)} is
free. <em>Split</em> hands every load on a driver its own number in one go, and <em>Add a driver</em>
makes an empty one to move work into. Re-plan to see what any of it does to the day. The running
order within a driver is still chosen for you: every order is tried and the cheapest is kept.</p>
<p><button type="submit" name="action" value="arrange">Re-plan with these drivers</button>
<button class="quiet spaced" type="submit" name="action" value="add">Add a driver</button></p>
{drivers}
<p><button type="submit" name="action" value="arrange">Re-plan with these drivers</button>
<button class="quiet spaced" type="submit" name="action" value="add">Add a driver</button></p>
</div>
<div class="card">
<h2>Save this plan</h2>
<label for="plan_name">Name</label>
<input id="plan_name" type="text" name="plan_name" value="{esc(saved_name)}"
       placeholder="Tuesday 8/28 - two drivers on the Adirondack run">
<button type="submit" name="action" value="save">Save plan</button>
<p class="hint">Saved under {esc(center.name if center else config.dc_zip)}, and listed on
<a href="/plans">Saved plans</a>. Saving over a name replaces that plan. A saved plan keeps the
loads, not the miles or the dwell: reopening it re-costs the day against whatever the dwell says
then.</p>
</div>
</form>
<p><a href="/">Plan another sheet</a></p>"""


def _empty_driver(number: str) -> str:
    """A driver with nothing on them yet, waiting to be given work."""
    return f"""<div class="driver empty"><div class="head">
<span class="who">Driver {esc(number)}</span>
<span class="meta">no loads yet - put {esc(number)} beside any load to move it here</span>
<button class="quiet" type="submit" name="action" value="drop:{esc(number)}">remove</button>
</div></div>"""


def _driver(index: int, driver) -> str:
    """One driver: who they are, what it costs, and where their loads can go."""
    assignment = driver.assignment
    split = (
        f'<button class="quiet" type="submit" name="action" value="split:{index}">'
        "split</button>"
        if len(driver.load_ids) > 1
        else ""
    )
    moves = "".join(
        f'<label class="move">{esc(load_id)}'
        f'<input type="number" name="driver:{esc(load_id)}" value="{index}" min="1" step="1">'
        f'<input type="hidden" name="seq:{esc(load_id)}" value="{position}">'
        f"</label>"
        for position, load_id in enumerate(driver.load_ids, start=1)
    )
    orders = _orders(driver)
    notes = "".join(f'<p class="note">{esc(text)}</p>' for text in driver.warnings)

    if assignment is None:
        return f"""<div class="driver"><div class="head">
<span class="who">Driver {index}</span><span>{moves}</span>
<span class="meta">cannot be scheduled</span>{split}
</div><div class="pad">{notes}</div>{orders}</div>"""

    equipment = " + ".join(
        f'{esc(trip.load.load_id)} <span class="tag">{esc(trip.load.equipment)}</span> '
        f"{trip.miles:.0f} mi"
        for trip in assignment.trips
    )
    waiting = f", {assignment.wait_hours:.1f} h waiting" if assignment.wait_hours > 1e-6 else ""
    layover = (
        f'<span class="tag">layover: {assignment.shifts} shifts, '
        f'{assignment.rest_hours:.0f} h rest</span>'
        if assignment.is_layover
        else ""
    )
    stops = "".join(
        f"<tr><td>{esc(s.load_id)}</td>"
        f'<td class="num">{report.clock(s.arrive)} - {report.clock(s.depart)}</td>'
        f"<td>{esc(s.stop)}</td>"
        f'<td class="num">{esc(report.window(s.stop))}</td>'
        f'<td class="num">{f"{s.wait * 60:.0f}m" if s.wait > 1e-6 else ""}</td></tr>'
        for s in assignment.schedule
    )
    return f"""<div class="driver"><div class="head">
<span class="who">Driver {index}</span><span>{moves}</span>
<span class="meta">{equipment}</span>
<span class="meta">{report.clock(assignment.start_hour)} - {report.clock(assignment.finish_hour)},
{assignment.duty_hours:.1f} h duty, {assignment.drive_hours:.1f} h drive{waiting}</span>{layover}{split}
</div>{f'<div class="pad">{notes}</div>' if notes else ''}{orders}
<table><tr><th>Load</th><th>On site</th><th>Stop</th><th>Window</th><th>Wait</th></tr>
{stops}</table></div>"""


#: Running orders shown per driver. Two loads have two orders and three have
#: six; past that only the shortest few are worth the room.
MAX_ORDERS_SHOWN = 6


def _orders(driver) -> str:
    """What each running order for this driver's loads would cost.

    A pair that only works one way round is the whole reason this is here, so
    the orders that do not work are listed too, with what stops them.
    """
    if len(driver.options) < 2:
        return ""

    rows = []
    for position, option in enumerate(driver.options[:MAX_ORDERS_SHOWN]):
        running = position == 0
        if option.feasible:
            cost = (
                f'<td class="num">{option.duty_hours:.1f} h</td>'
                f'<td class="num">{option.drive_hours:.1f} h</td>'
            )
            action = (
                '<span class="tag">running</span>'
                if running
                else '<button class="quiet" type="submit" name="action" '
                f'value="order:{esc(",".join(option.load_ids))}">run this way</button>'
            )
        else:
            cost = f'<td class="note" colspan="2">{esc(option.reason)}</td>'
            action = '<span class="tag">running</span>' if running else ""
        rows.append(
            f'<tr><td class="num">{" &rarr; ".join(esc(load_id) for load_id in option.load_ids)}</td>'
            f"{cost}<td>{action}</td></tr>"
        )

    caption = (
        '<p class="hint">Single-shift figures: this driver sleeps out whichever order they run.</p>'
        if driver.is_layover
        else ""
    )
    return f"""<div class="orders"><table>
<tr><th>Running order</th><th>Duty</th><th>Drive</th><th></th></tr>
{"".join(rows)}</table>{caption}</div>"""


def render_saved_plans(store, message: str = "") -> str:
    centers = {center.zip: center for center in store.service_centers()}
    rows = "".join(
        f'<tr><td><a href="/plans/open?id={esc(saved.id)}">{esc(saved.name)}</a></td>'
        f'<td>{esc(centers[saved.service_center].name if saved.service_center in centers else saved.service_center)}</td>'
        f"<td>{esc(saved.sheet_name or '-')}</td>"
        f'<td class="num">{saved.load_count}</td>'
        f'<td class="num">{saved.driver_count}</td>'
        f'<td class="num">{esc(saved.saved_at)}</td>'
        f'<td><form method="post" action="/plans/delete">'
        f'<input type="hidden" name="id" value="{esc(saved.id)}">'
        f'<button class="quiet" type="submit">delete</button></form></td></tr>'
        for saved in store.saved_plans()
    ) or '<tr><td colspan="7">Nothing saved yet.</td></tr>'

    return f"""{message}
<div class="card"><h2>Saved plans</h2>
<p class="hint">A saved plan keeps the loads it was built from, so it reopens after the spreadsheet
is gone. Miles and dwell are not saved: reopening re-costs the day against what the dwell says
now, so a dwell you correct later shows up in every plan that touches that store.</p>
<table><tr><th>Name</th><th>Service center</th><th>Sheet</th><th>Loads</th><th>Drivers</th>
<th>Saved</th><th></th></tr>{rows}</table></div>"""


def render_service_centers(store, message: str = "") -> str:
    centers = store.service_centers()
    counts = {}
    for location in store.locations():
        counts[location.service_center] = counts.get(location.service_center, 0) + 1

    rows = "".join(
        f'<tr><td class="num">{esc(center.zip)}</td><td>{esc(center.name)}</td>'
        f'<td>{esc(center.where or "-")}</td>'
        f'<td class="num">{counts.get(center.zip, 0)}</td>'
        f'<td><a href="/locations?sc={esc(center.zip)}">locations</a></td></tr>'
        for center in centers
    ) or '<tr><td colspan="5">No service centers yet.</td></tr>'

    return f"""{message}
<div class="card"><h2>Service centers</h2>
<p class="hint">A service center is a pickup location: drivers load there and return there. Every
sheet is uploaded against one, and the stores it delivers to are kept under that service center,
so two service centers delivering to the same ZIP never share a dwell.</p>
<table><tr><th>ZIP</th><th>Name</th><th>Where</th><th>Locations</th><th></th></tr>{rows}</table>
</div>
<form class="card" method="post" action="/service-centers">
<h2>Add a service center</h2>
<div class="grid">
  <div><label for="sc_zip">ZIP</label>
       <input id="sc_zip" type="text" name="zip" required placeholder="01020"></div>
  <div><label for="sc_name">Name</label>
       <input id="sc_name" type="text" name="name" required placeholder="New England SC"></div>
  <div><label for="sc_city">City</label>
       <input id="sc_city" type="text" name="city" placeholder="Chicopee"></div>
  <div><label for="sc_state">State</label>
       <input id="sc_state" type="text" name="state" placeholder="MA"></div>
</div>
<p class="hint">The ZIP identifies the service center and is where every round trip starts and
ends. Saving an existing ZIP renames it and keeps its locations.</p>
<button type="submit">Save service center</button>
</form>"""


def render_locations(store, selected: str = "", message: str = "") -> str:
    centers = store.service_centers()
    if not centers:
        return f"""{message}<div class="card"><h2>Locations</h2>
<p class="note">Add a <a href="/service-centers">service center</a> first: locations are kept
under the service center that delivers to them.</p></div>"""

    known = {center.zip for center in centers}
    if selected not in known:
        selected = DEFAULT_DC_ZIP if DEFAULT_DC_ZIP in known else centers[0].zip
    chosen = store.service_center(selected)

    rows = []
    for location in store.locations(selected):
        where = ", ".join(part for part in (location.city, location.state) if part) or "-"
        coordinates = (
            f"{location.lat:.4f}, {location.lon:.4f}"
            if location.lat is not None and location.lon is not None
            else '<span class="note">missing</span>'
        )
        # The service center's own ZIP is a location with no store behind it.
        store_numbers = esc(location.store) if location.store else "-"
        rows.append(
            f"<tr><td class=\"num\">{esc(location.zip)}</td>"
            f'<td class="num">{store_numbers}</td><td>{esc(where)}</td>'
            f'<td><input class="num" type="number" step="0.25" min="0" max="12" '
            f'name="dwell:{esc(location.zip)}" value="{location.dwell_hours:.2f}"></td>'
            f'<td class="num">{coordinates}</td></tr>'
        )
    body = "".join(rows) or (
        f'<tr><td colspan="5">No sheet has been uploaded for {esc(chosen.name or selected)} yet.</td></tr>'
    )
    choices = "".join(
        f'<option value="{esc(center.zip)}"{" selected" if center.zip == selected else ""}>'
        f"{esc(center.label)}</option>"
        for center in centers
    )

    return f"""{message}
<form class="card" method="get" action="/locations">
<h2>Service center</h2>
<div class="grid"><div><label for="sc">Show the locations of</label>
<select id="sc" name="sc">{choices}</select></div></div>
<button type="submit">Show</button>
</form>
<form class="card" method="post" action="/locations">
<input type="hidden" name="service_center" value="{esc(selected)}">
<h2>Locations - {esc(chosen.name or selected)}</h2>
<p class="hint">Dwell is what the tool bills for time on the dock at each stop. A ZIP arrives here at
1.0 h the first time it appears in a sheet uploaded for this service center; what you set below is
kept and never overwritten by a later upload, and belongs to this service center alone. Store is
every store number a sheet has delivered to that ZIP from here.</p>
<table><tr><th>ZIP</th><th>Store</th><th>Where</th><th>Dwell (h)</th><th>Coordinates</th></tr>{body}</table>
<button type="submit">Save dwell</button>
</form>
<form class="card" method="post" action="/coordinates" enctype="multipart/form-data">
<input type="hidden" name="service_center" value="{esc(selected)}">
<h2>Coordinates</h2>
<p class="hint">Only the offline mileage estimate needs these. A routing API key
(PC*Miler, Google or HERE) makes them unnecessary. Upload a CSV of
<code>zip,lat,lon</code>, or submit with no file to try pgeocode if it is installed.
Coordinates belong to the ZIP, so filling them in serves every service center.</p>
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
        return _with_store(lambda store: page("Plan", plan_form({}, store.service_centers())))
    if path == "/plan" and method == "POST":
        return _handle_plan(read_form(environ))
    if path == "/arrange" and method == "POST":
        return _handle_arrange(read_form(environ))
    if path == "/plans" and method == "GET":
        return _with_store(lambda store: page("Saved plans", render_saved_plans(store)))
    if path == "/plans/open" and method == "GET":
        return _handle_open_plan(_query(environ).get("id", ""))
    if path == "/plans/delete" and method == "POST":
        return _handle_delete_plan(read_form(environ))
    if path == "/service-centers":
        if method == "GET":
            return _with_store(lambda store: page("Service centers", render_service_centers(store)))
        if method == "POST":
            return _handle_service_center(read_form(environ))
    if path == "/locations":
        if method == "GET":
            selected = _query(environ).get("sc", "")
            return _with_store(
                lambda store: page("Locations", render_locations(store, selected))
            )
        if method == "POST":
            return _handle_dwell(read_form(environ))
    if path == "/coordinates" and method == "POST":
        return _handle_coordinates(read_form(environ))
    if path == "/lanes" and method == "GET":
        return _with_store(lambda store: page("Lanes", render_lanes(store)))
    if path in ("/", "/plan", "/arrange", "/plans", "/plans/open", "/plans/delete",
                "/service-centers", "/locations", "/lanes", "/coordinates"):
        return "405 Method Not Allowed", page("Not allowed", _message("That method is not allowed here.", "error"))
    return "404 Not Found", page("Not found", _message("No such page.", "error"))


def _query(environ) -> dict[str, str]:
    """The query string, first value wins."""
    parsed = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
    return {name: values[0] for name, values in parsed.items() if values}


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
        "carrier": form.get("carrier", DEFAULT_CARRIER_ID),
        "dc_zip": form.get("dc_zip", DEFAULT_DC_ZIP) or DEFAULT_DC_ZIP,
        "earliest_start": form.get("earliest_start", "0"),
        "max_duty": form.get("max_duty", "14"),
        "max_drive": form.get("max_drive", "11"),
        "objective": form.get("objective", OBJECTIVE_DUTY),
        "enforce_windows": form.checked("enforce_windows"),
        "match_equipment": form.checked("match_equipment"),
    }


def _handle_plan(form: Form):
    settings = _settings(form)
    centers = _service_centers()
    if not any(center.zip == settings["dc_zip"] for center in centers):
        return "400 Bad Request", page(
            "Plan",
            plan_form(
                settings,
                centers,
                _message(
                    f"{settings['dc_zip']} is not a service center. Add it on the "
                    "Service centers page, then upload the sheet against it.",
                    "error",
                ),
            ),
        )

    upload = form.file("sheet")
    if upload is None:
        return "400 Bad Request", page(
            "Plan",
            plan_form(settings, centers, _message("Choose a dispatch workbook to upload.", "error")),
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
                    centers,
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
        center = store.service_center(settings["dc_zip"])
        store.ensure_locations(
            [db.Location(zip=stop.zip, city=stop.city, state=stop.state, store=stop.store,
                         service_center=center.zip)
             for load in parsed.loads for stop in load.stops]
            + [db.Location(zip=center.zip, city=center.city, state=center.state,
                           service_center=center.zip)]
        )

        config = _config(settings)
        trips, view = _cost(store, parsed.loads, config, center)
        view.update(sheet_name=parsed.sheet_name, stop_count=parsed.stop_count)

        built = plan(trips, config)
        groups = arrange_loads.groups_of(built)
        baseline = arrange_loads.arrange(trips, groups, config)
        view.update(
            settings_json=json.dumps(settings, separators=(",", ":")),
            loads_json=snapshot.dump_loads(parsed.loads),
            baseline_json=snapshot.dump_groups(groups),
        )
        body = render_workspace(baseline, baseline, view)
    except RoutingError as exc:
        return "400 Bad Request", page(
            "Plan",
            plan_form(
                settings,
                centers,
                _message(f"{exc}. Upload a zip,lat,lon CSV on the Locations page.", "error"),
            ),
        )
    finally:
        store.close()

    return "200 OK", page("Plan", body)


def _config(settings: dict) -> PairingConfig:
    """The pairing configuration one set of settings describes."""
    return PairingConfig(
        dc_zip=settings["dc_zip"],
        max_duty_hours=_number(settings.get("max_duty"), 14.0),
        max_drive_hours=_number(settings.get("max_drive"), 11.0),
        match_equipment=bool(settings.get("match_equipment")),
        objective=settings.get("objective", OBJECTIVE_DUTY),
        windows=WindowPolicy(
            enforce=bool(settings.get("enforce_windows")),
            earliest_start=_number(settings.get("earliest_start"), 0.0),
        ),
    )


def _number(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _cost(store, loads, config: PairingConfig, center):
    """Cost a set of loads against the miles and dwell on file right now.

    Every way into the plan page goes through here -- a fresh upload, a
    rearrangement, a saved plan reopened -- so all three price the day the same
    way, and a dwell corrected in between shows up in all of them.
    """
    router = router_from_env(os.environ.get("LOAD_PAIRING_ROUTER", "auto"))
    if router.name == ESTIMATED:
        geocode.fill_coordinates(store)

    mileage = MileageService(store, router)
    mileage.refresh()
    dwell = store.dwell_hours(center.zip)

    trips = build_trips(
        loads,
        miles_for=mileage.miles,
        dwell_for=lambda zip_code: dwell.get(zip_code, DEFAULT_DWELL_HOURS),
        config=config,
    )
    view = {
        "center": center,
        "load_count": len(list(loads)),
        "stop_count": sum(len(load.stops) for load in loads),
        "router_name": router.name,
        "fetched": mileage.fetched,
        "estimated_mileage": any(trip.estimated for trip in trips),
        "solo_hours": sum(trip.duty_hours for trip in trips),
    }
    return trips, view


def _rebuild(form: Form):
    """Rebuild the plan a submitted page carries, with the grouping it asks for.

    Returns ``(arrangement, baseline, view, settings)``. Raises
    :class:`SnapshotError` or :class:`ArrangeError` when the submission does not
    describe a plan, which the caller turns into a message rather than a crash.
    """
    settings = _settings_json(form.get("settings", ""))
    loads = snapshot.read_loads(form.get("loads", ""))
    baseline_groups = snapshot.read_groups(form.get("baseline", ""))
    grouping = _grouping(form, loads, baseline_groups)
    grouping, spare = _apply_action(form.get("action", ""), grouping, _spare_numbers(form))
    groups = tuple(tuple(group) for group in grouping.values())

    config = _config(settings)
    store = db.connect()
    try:
        center = store.service_center(settings["dc_zip"])
        if center is None:
            raise snapshot.SnapshotError(
                f"{settings['dc_zip']} is no longer a service center; this plan cannot be re-costed"
            )
        trips, view = _cost(store, loads, config, center)
    finally:
        store.close()

    view.update(
        sheet_name=settings.get("sheet_name", "Plan"),
        settings_json=json.dumps(settings, separators=(",", ":")),
        loads_json=snapshot.dump_loads(loads),
        baseline_json=snapshot.dump_groups(baseline_groups),
        saved_name=form.get("plan_name", "").strip(),
        # An empty driver a dispatcher asked for and has not filled yet. It is
        # a slot on the page, not part of the plan: nothing is costed for it.
        spare=len([number for number in spare if number not in grouping]),
    )
    arrangement = arrange_loads.arrange(trips, groups, config)
    baseline = arrange_loads.arrange(trips, baseline_groups, config)
    return arrangement, baseline, view, settings


def _grouping(form: Form, loads, fallback) -> dict[str, list[str]]:
    """Read the driver number beside each load into a grouping.

    Loads sharing a number share a driver: that is how two drivers are put
    together. A number nobody else has is a driver of its own, which is how one
    is added. A load whose number is missing or unreadable keeps its own driver
    rather than silently joining someone else's.

    Keyed by driver number so the buttons on the page -- split this driver,
    empty that one -- can name the driver a dispatcher is looking at.
    """
    numbered: dict[str, list[str]] = {}
    seen = False
    for load in loads:
        raw = form.get(f"driver:{load.load_id}", "").strip()
        try:
            key = str(int(float(raw)))
            seen = True
        except ValueError:
            key = f"~{load.load_id}"          # unreadable: leave it on its own
        numbered.setdefault(key, []).append(load.load_id)

    if not seen:
        numbered = {str(index): list(group) for index, group in enumerate(fallback, start=1)}
    else:
        # The turn order the page was showing. Sorting is stable, so loads
        # arriving from another driver keep the order they are listed in.
        for group in numbered.values():
            group.sort(key=lambda load_id: _sequence(form, load_id))
    return dict(sorted(numbered.items(), key=_driver_key))


def _sequence(form: Form, load_id: str) -> float:
    """Where a load runs in its driver's day, as the page had it."""
    try:
        return float(form.get(f"seq:{load_id}", "").strip())
    except ValueError:
        return 0.0


def _driver_key(item) -> tuple:
    key = item[0]
    return (True, 0, key) if key.startswith("~") else (False, int(key), "")


def _next_free(grouping: dict[str, list[str]], taken=()) -> str:
    """A driver number nobody is using."""
    used = {int(key) for key in grouping if not key.startswith("~")}
    used.update(int(number) for number in taken)
    return str(max(used, default=0) + 1)


def _apply_action(action: str, grouping: dict[str, list[str]], spare: list[str]):
    """Carry out a button on the plan page, returning the new grouping.

    ``split`` gives each of one driver's loads a driver of its own -- the quick
    way to add drivers. ``add`` and ``drop`` make and remove an empty driver to
    move loads into. Everything else is done with the numbers themselves.
    """
    verb, _, which = action.partition(":")

    if verb == "add":
        spare.append(_next_free(grouping, spare))
    elif verb == "drop" and which in spare:
        spare.remove(which)
    elif verb == "order" and which:
        # A running order picked off the page, named by its loads in sequence.
        wanted = [load_id for load_id in which.split(",") if load_id]
        for key, loads_here in grouping.items():
            if sorted(loads_here) == sorted(wanted):
                grouping[key] = wanted
                break
    elif verb == "split" and which in grouping:
        here = grouping.pop(which)
        grouping[which] = here[:1]                 # the first load keeps the number
        for load_id in here[1:]:
            grouping[_next_free(grouping, spare)] = [load_id]
    return dict(sorted(grouping.items(), key=_driver_key)), spare


def _spare_numbers(form: Form) -> list[str]:
    """The empty drivers the page was showing, as it numbered them."""
    return [part for part in form.get("spare", "").split(",") if part.strip().isdigit()]


def _settings_json(text: str) -> dict:
    if not text:
        raise snapshot.SnapshotError("no settings in this submission")
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise snapshot.SnapshotError(f"the settings in this submission are not readable: {exc}")
    if not isinstance(parsed, dict) or not parsed.get("dc_zip"):
        raise snapshot.SnapshotError("the settings in this submission name no service center")
    return parsed


def _handle_arrange(form: Form):
    """Re-cost a plan with the drivers the dispatcher asked for, or save it."""
    try:
        arrangement, baseline, view, settings = _rebuild(form)
    except (snapshot.SnapshotError, arrange_loads.ArrangeError) as exc:
        return "400 Bad Request", page(
            "Plan", _message(f"{exc}. Upload the sheet again to start over.", "error")
        )

    if form.get("action", "") != "save":
        return "200 OK", page("Plan", render_workspace(arrangement, baseline, view))


    name = form.get("plan_name", "").strip()
    if not name:
        note = _message("Give the plan a name before saving it.", "error")
        return "400 Bad Request", page("Plan", render_workspace(arrangement, baseline, view, note))

    store = db.connect()
    try:
        settings = dict(settings, sheet_name=view.get("sheet_name", "Plan"))
        plan_id, is_new = store.save_plan(
            db.SavedPlan(
                id="",
                name=name,
                service_center=settings["dc_zip"],
                sheet_name=view.get("sheet_name", ""),
                load_count=view.get("load_count", 0),
                driver_count=arrangement.driver_count,
                settings=json.dumps(settings, separators=(",", ":")),
                loads=view.get("loads_json", "[]"),
                groups=snapshot.dump_groups(arrangement.groups),
            )
        )
    finally:
        store.close()

    view["saved_name"] = name
    note = _message(
        f"Saved as \u201c{name}\u201d" + ("." if is_new else ", replacing the plan of that name.")
        + " It is on the Saved plans page."
    )
    return "200 OK", page("Plan", render_workspace(arrangement, baseline, view, note))


def _handle_open_plan(plan_id: str):
    """Reopen a saved plan, re-costed against the miles and dwell on file now."""
    store = db.connect()
    try:
        saved = store.saved_plan(plan_id)
        if saved is None:
            return "404 Not Found", page(
                "Saved plans", _message("That plan is no longer saved.", "error")
            )
        center = store.service_center(saved.service_center)
        if center is None:
            return "400 Bad Request", page(
                "Saved plans",
                _message(
                    f"{saved.name} was planned out of {saved.service_center}, which is no longer "
                    "a service center.",
                    "error",
                ),
            )
        try:
            settings = _settings_json(saved.settings)
            loads = snapshot.read_loads(saved.loads)
            groups = snapshot.read_groups(saved.groups)
            config = _config(settings)
            trips, view = _cost(store, loads, config, center)
            arrangement = arrange_loads.arrange(trips, groups, config)
        except (snapshot.SnapshotError, arrange_loads.ArrangeError) as exc:
            return "400 Bad Request", page(
                "Saved plans", _message(f"{saved.name} cannot be reopened: {exc}", "error")
            )
    finally:
        store.close()

    view.update(
        sheet_name=saved.sheet_name or settings.get("sheet_name", "Plan"),
        settings_json=json.dumps(settings, separators=(",", ":")),
        loads_json=saved.loads,
        baseline_json=saved.groups,
        saved_name=saved.name,
    )
    note = _message(
        f"\u201c{saved.name}\u201d as saved on {saved.saved_at}, re-costed against the dwell and "
        "miles on file now."
    )
    return "200 OK", page("Plan", render_workspace(arrangement, arrangement, view, note))


def _handle_delete_plan(form: Form):
    store = db.connect()
    try:
        deleted = store.delete_plan(form.get("id", ""))
        note = _message("Plan deleted." if deleted else "That plan was already gone.")
        return "200 OK", page("Saved plans", render_saved_plans(store, note))
    finally:
        store.close()


def _service_centers() -> list:
    store = db.connect()
    try:
        return store.service_centers()
    finally:
        store.close()


def _handle_service_center(form: Form):
    zip_code = normalize_zip(form.get("zip", ""))
    name = form.get("name", "").strip()

    store = db.connect()
    try:
        if not zip_code or not name:
            note = _message("A service center needs a ZIP and a name.", "error")
            return "400 Bad Request", page("Service centers", render_service_centers(store, note))

        added = store.save_service_center(
            db.ServiceCenter(
                zip=zip_code,
                name=name,
                city=form.get("city", "").strip(),
                state=form.get("state", "").strip().upper(),
            )
        )
        note = _message(
            f"{name} ({zip_code}) {'added' if added else 'updated'}. "
            "Upload a sheet against it on the Plan page."
        )
        return "200 OK", page("Service centers", render_service_centers(store, note))
    finally:
        store.close()


def _handle_dwell(form: Form):
    store = db.connect()
    try:
        selected = form.get("service_center", DEFAULT_DC_ZIP) or DEFAULT_DC_ZIP
        changed = 0
        for name, field in form.items():
            if not name.startswith("dwell:"):
                continue
            zip_code = name.split(":", 1)[1]
            try:
                hours = float(field.text)
            except ValueError:
                continue
            existing = store.location(zip_code, selected)
            if existing is not None and abs(existing.dwell_hours - hours) > 1e-9:
                store.set_dwell(zip_code, hours, selected)
                changed += 1
        note = _message(
            f"Saved {changed} dwell change{'' if changed == 1 else 's'}."
            if changed
            else "No dwell values changed."
        )
        return "200 OK", page("Locations", render_locations(store, selected, note))
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
        selected = form.get("service_center", DEFAULT_DC_ZIP) or DEFAULT_DC_ZIP
        return "200 OK", page("Locations", render_locations(store, selected, note))
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
