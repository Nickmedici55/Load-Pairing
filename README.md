# Load Pairing Tool

Takes a daily dispatch spreadsheet, costs each load as a round trip out of the
distribution center, and finds loads that can be run back-to-back by one driver
inside HOS limits.

`CLAUDE.md` holds the project spec this implements.

Sample output, against a generated sheet with estimated mileage. `--carrier`
already defaults to `PTAG`, so the OTHR load on the sheet is left out:

```
$ loadpairing plan dispatch.xlsx --schedule

Dispatch Order_3: 5 loads, 7 stops

5 loads -> 3 drivers (2 pairs + 0 solo + 1 layover)
25.5 h of driver time if every schedulable load ran on its own; 6 feasible pairs found, matched by builtin
limits: 14 h duty, 11 h drive; windows enforced; equipment may differ

Driver  1  10375781 (53RL, 2 stops, 572 mi, 14.4 h)  ->  00:00-00:27 +1d  14.4 h duty, 11.4 h drive  [layover: 2 shifts, 10 h rest; delivery times need a dispatcher]
            10375781  05:21-06:21  Plattsburgh (Plattsburgh, NY 12901)  window 05:15-09:00
            10375781  08:05-09:05  Massena (Massena, NY 13662)  window any
Driver  2  10375778 (53LG, 2 stops, 26 mi, 3.5 h) + 10375775 (53RL, 1 stop, 2 mi, 2.0 h)  ->  05:10-12:01  6.8 h duty, 0.6 h drive, 1.3 h waiting
            10375778  06:15-07:15  Springfield (Springfield, MA 01109)  window 06:15-11:00
            10375778  07:30-08:30  Westfield (Westfield, MA 01085)  window any
            10375775  11:00-12:00  Chicopee St (Chicopee, MA 01013)  window 11:00-20:00
Driver  3  10375774 (53LG, 1 stop, 9 mi, 2.2 h) + 10375790 (53PLG, 1 stop, 93 mi, 3.4 h)  ->  09:54-15:27  5.5 h duty, 2.0 h drive
            10375774  11:00-12:00  Holyoke (Holyoke, MA 01040)  window 11:00-20:00
            10375790  14:01-14:31  Pittsfield DC (Pittsfield, MA 01201)  window 00:00-23:59 D&H
```

Drivers 2 and 3 leave the DC on the clock of their first stop — 05:10 to be at
Springfield as it opens at 06:15. The Pittsfield stop is a drop and hook, due
by 23:59 and half an hour on the ground. Driver 1's load is more work than one
shift holds, so it runs as a layover rather than being dropped.

## Running it

Python 3.9 or newer, no required dependencies:

```
python -m loadpairing plan sheet.xlsx --tab 3 --carrier PTAG
```

or `pip install -e .` for a `loadpairing` command.

Three optional extras change how it runs, not what it does:

| Extra | Effect if missing |
|---|---|
| `psycopg` | a `postgresql://` database URL is refused; SQLite still works |
| `networkx` | the built-in blossom matcher runs instead (same answer) |
| `pgeocode` | ZIP coordinates must come from `--centroids` or be typed in |
| `gunicorn` | use `loadpairing serve` (the stdlib server) instead |

### Commands

```
loadpairing serve                               the web front end, on localhost:8000
loadpairing sheets BOOK.xlsx                    list the tabs
loadpairing plan BOOK.xlsx --service-center 01020   build the driver plan
loadpairing service-centers list                the pickup locations
loadpairing service-centers add 06103 "Hartford SC" --city Hartford --state CT
loadpairing locations list                      every ZIP seen, with its store and dwell
loadpairing locations list --service-center 01020   only one service center's
loadpairing locations dwell 01040 1.75 --service-center 01020   override a dwell
loadpairing locations coords --csv centroids.csv   fill in ZIP coordinates
loadpairing lanes list | lanes estimated        inspect the mileage cache
```

`--db` (or `$LOAD_PAIRING_DB`) takes a `postgresql://` URL or a SQLite path;
it defaults to `load_pairing.sqlite3` in the working directory. The schema is
created on first connect; `schema/postgres.sql` is the same DDL for a DBA who
would rather apply it by hand.

`--carrier` defaults to `PTAG`; pass `--carrier ""` to read every carrier on
the sheet.

Useful `plan` options: `--router {auto,pcmiler,google,here,estimated}`,
`--centroids FILE`, `--match-equipment`, `--no-windows`, `--earliest-start H`,
`--max-duty H`, `--max-drive H`, `--schedule`, `--json`.

## The web app

`loadpairing serve` (or gunicorn in production) puts the same thing behind a
browser, which is what the spec's "uploaded sheet" and dispatcher-set dwell
imply:

* **/** — upload a workbook, pick the tab, carrier, service center and limits,
  get the driver plan with every stop on the clock. From there, rearrange it:
  move loads between drivers, add a driver or collapse two together, and re-plan
  to see what it does to the day. Save the result under a name.
* **/plans** — plans saved under a name, ready to reopen. A saved plan keeps
  the loads it was built from, so it outlives the spreadsheet; miles and dwell
  are re-read on open.
* **/service-centers** — the pickup locations. Every sheet is uploaded against
  one, and its stores are kept under it, so two service centers delivering to
  the same ZIP never share a dwell. A ZIP identifies a service center.
* **/locations** — one service center's ZIPs, with the store numbers delivered
  there and the dwell in an editable field. This is where the data quality
  accrues. Also takes a `zip,lat,lon` CSV for the offline mileage estimate;
  coordinates belong to the ZIP and serve every service center.
* **/lanes** — the mileage cache, with the estimated rows called out.
* **/healthz** — plain `ok`, for a platform health check.

It is a plain WSGI application (`loadpairing.web:application`) with no
framework, so gunicorn, uWSGI or `wsgiref` all serve it.

## Deploying

`requirements.txt`, `gunicorn.conf.py` and `railpack.json` are all that a
Railway-style deploy needs; the start command is
`gunicorn loadpairing.web:application`, and the config file reads `$PORT`
itself so nothing depends on shell expansion. On another platform, any WSGI
host works — point it at `loadpairing.web:application`.

Three environment variables matter:

| Variable | Why |
|---|---|
| `DATABASE_URL` | a Postgres URL, e.g. from an attached database. **Without it the app falls back to SQLite on the container's disk, which most platforms wipe on every deploy** — taking every dwell override with it. Attach a database before anyone relies on this. |
| `LOAD_PAIRING_PASSWORD` | turns on HTTP basic auth (user `dispatch`, or set `LOAD_PAIRING_USER`). **Unset, the app is open to anyone with the URL** — no login, and uploaded sheets and plans are readable. Set it on anything reachable from the internet. |
| `PCMILER_API_KEY` / `GOOGLE_MAPS_API_KEY` / `HERE_API_KEY` | the routing source. With none set the app falls back to estimated mileage and needs ZIP coordinates from the Locations page. |

`LOAD_PAIRING_DB` overrides `DATABASE_URL` if you want to point at something
else, and `LOAD_PAIRING_ROUTER` pins the routing source instead of `auto`.

## How a load is costed

Every load is a round trip: depart the DC loaded, hit every delivery stop in
sheet order, return to the DC empty. The stops in the sheet are deliveries
only; the origin is implicit and is always the DC.

    trip duration = 1.0 h loading at the DC
                  + the dwell at each delivery stop
                  + round-trip miles / 50 mph

Dwell defaults to 1.0 h and is overridable per location. A ZIP is inserted into
`location` at the default the first time it appears in any uploaded sheet, and
from then on the tool reads whatever the dispatcher has set — a later upload of
the same ZIP never overwrites it. Data quality improves with use.

A **drop and hook** is the exception: 0.5 h, fixed, whatever the location's
dwell says. The override describes how long a live unload takes there, and a
trailer swap is not one.

Pairing means two sequential round trips on one driver, not two loads on one
trailer: every load in the sample runs 21–28 pallets and fills a 53' either
way, so the driver returns to the DC, reloads, and goes back out.

A pair is feasible when combined duty is at most 14 h, combined drive is at
most 11 h, every delivery window can still be met, and — if
`--match-equipment` is set — both loads want the same trailer. Both running
orders are tried and the better one is kept.

## Layovers

A load whose own duty or drive is over a single shift's limit is not
impossible — the driver sleeps out and finishes the next day. Those loads run
solo with a layover: the schedule drops in a 10 h rest wherever the next stop
would break the driving or duty limit, and the rest is not counted as duty.
They are never paired, being already more than a shift.

**Delivery times are not checked across a layover.** The sheet gives an hour
of the day, not a date, so once a trip runs past midnight there is no way to
tell which day a stop is due on. Those loads come back with their stops laid
out and their shift count, and the delivery times are a dispatcher's call.



Feasible pairs are the edges of a graph and the plan is a maximum-cardinality,
maximum-weight matching over it: fewest drivers first, then a tie-break that
packs the fullest shifts together (`--objective wait` minimises idle time
instead).

## Mileage

Mileage should come from a truck-legal routing source. These loads run Class 8
into Lake Placid, Massena and Plattsburgh NY, where car routing and truck
routing diverge meaningfully. PC*Miler is preferred; set one of
`PCMILER_API_KEY`, `GOOGLE_MAPS_API_KEY` or `HERE_API_KEY` and `--router auto`
picks it up.

Every lane fetched is cached permanently in `lane`. With no key set, the tool
falls back to great-circle distance × 1.20 circuity and marks those rows
`source = 'estimated'`, so `loadpairing lanes estimated` lists exactly what
needs backfilling later. **Plans built on estimated mileage are for shaping the
approach, not for dispatching** — the Adirondack lanes are where the estimate
is worst.

The offline estimate needs ZIP coordinates, which come from `--centroids`
(a `zip,lat,lon` CSV), from `pgeocode` if installed, or from
`locations coords`.

## Delivery windows

`Window Close` is the delivery time — the hour the load is due at that stop —
and `Window Open` is the earliest the receiver will take it.

**The driver leaves the DC at whatever hour lands them at the first stop of a
turn exactly as it opens.** Not earlier, so nobody sits at a receiver's door;
not later, so nothing downstream is given away. On the second turn of a pair
the driver holds at the DC rather than at the customer. That anchor is also
the earliest the trip can possibly progress — a stop cannot be served before
it opens — so it is the schedule most likely to make every later delivery
time.

`--earliest-start` is the one hour before which no driver may roll. When the
anchor would need a dispatch earlier than that, the day falls back to a search:
arrival at every stop is non-decreasing in the start time, so meeting every
delivery time is downward closed in it, and elapsed duty is non-increasing in
it. One backward pass finds the latest start that violates nothing, and the
schedule is pulled back to the earliest start that wastes no waiting. Waiting
counts against the 14 h duty limit either way.

Two things are worth knowing:

* **A `00:00` close means 23:59 that night, not midnight at the start of the
  day.** It also marks the stop as a drop and hook, priced at 0.5 h. So those
  loads are due by end of day and cost half an hour on the ground.
* **`--earliest-start` defaults to `0`, and should usually stay there.** It is
  a gate, not a preference: raising it can only push loads out of feasibility,
  because it forbids the early dispatch a morning delivery time needs. Set it
  only if there is a real hour before which nobody rolls.

Enforcing windows reduces the pair count. That is expected, not a regression.

## Trailer type

Spec open decision #2, still open. Equipment types in the sample are `53LG`,
`53PLG`, `53RL`, `53PRL` and `48PLG`. Pairing across types only works if the
driver drops and hooks a different trailer at the DC between turns. The default
allows it, matching the prototype; `--match-equipment` forbids it. Nothing else
changes, so it is cheap to run both and compare.

## Spreadsheet parsing

The sheet layout is nonstandard and the parser depends on its quirks: a title
block in rows 0–5, the real header at row 6, that header repeated before every
load block, a load block starting where `Carrier ID` is populated, and stop
rows below it with a blank `Carrier ID`. ZIPs read back as numbers and are cast
and zero-padded to five. Tabs 2 and 3 (`Reverse Order_2`, `Dispatch Order_3`)
hold the same loads in opposite stop sequence — tab 3 is the default.

Reading `.xlsx` is done directly against the zip-of-XML rather than through
pandas/openpyxl, which is what keeps the tool dependency-free on a
dispatcher's machine. `--header-row` covers a sheet whose title block is a
different height.

## Tests

```
python -m unittest discover -s tests -t .
```

118 tests, no dependencies, under a second. They cover the parser against
generated workbooks that reproduce the sheet's quirks, the costing arithmetic,
window feasibility, the lane cache and dwell-override rules, and the planner
end to end through both the CLI and the WSGI app — including multipart
uploads, the dwell round trip and basic auth.

The matcher is checked against an exact subset DP on random graphs. Outside the
suite it has been run against that oracle on 20,000 random graphs (up to 12
vertices, four densities, both cardinality modes) with no disagreement.

## What is not verified here

The sample workbook `NESC08_28.xlsx` and `load_pairing_prototype.py` are not in
this repository, so the spec's baseline — 34 PTAG loads, 96 stops, 15 pairs + 4
solo — has not been reproduced. The tests run against generated fixtures built
to the layout the spec describes. Point the tool at the real sheet with
`--router estimated --no-windows` to compare against that baseline directly;
the numbers should line up before any of the window results are trusted.
