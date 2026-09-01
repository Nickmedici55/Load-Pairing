# Load Pairing Tool

Takes a daily dispatch spreadsheet, costs each load as a round trip out of the
distribution center, and finds loads that can be run back-to-back by one driver
inside HOS limits.

`CLAUDE.md` holds the project spec this implements.

Sample output, against a generated sheet with estimated mileage:

```
$ loadpairing plan dispatch.xlsx --tab 3 --carrier PTAG --earliest-start 4 --schedule

Dispatch Order_3: 4 loads, 7 stops

4 loads -> 2 drivers (1 pair + 1 solo, 1 unschedulable)
7.7 h of driver time if every schedulable load ran on its own; 3 feasible pairs found, matched by builtin
limits: 14 h duty, 11 h drive; windows enforced; equipment may differ
note: some lanes use estimated mileage (great-circle x 1.20), not a routing source

Driver  1  10375778 (53LG, 2 stops, 26 mi, 3.5 h) + 10375774 (53LG, 1 stop, 9 mi, 2.2 h)  ->  06:24-12:06  5.7 h duty, 0.7 h drive
            10375778  07:28-08:28  Springfield (Springfield, MA 01109)  window 06:15-11:00
            10375778  08:43-09:43  Westfield (Westfield, MA 01085)  window any
            10375774  11:00-12:00  Holyoke (Holyoke, MA 01040)  window 11:00-20:00
Driver  2  10375775 (53RL, 1 stop, 2 mi, 2.0 h)  ->  09:59-12:01  2.0 h duty, 0.0 h drive
            10375775  11:00-12:00  Chicopee St (Chicopee, MA 01013)  window 11:00-20:00

Unschedulable:
  10375781: 11.5 h driving exceeds the 11 h limit on its own
```

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

### Commands

```
loadpairing sheets BOOK.xlsx                    list the tabs
loadpairing plan BOOK.xlsx [options]            build the driver plan
loadpairing locations list                      every ZIP seen, with its dwell
loadpairing locations dwell 01040 1.75          override one location's dwell
loadpairing locations coords --csv centroids.csv   fill in ZIP coordinates
loadpairing lanes list | lanes estimated        inspect the mileage cache
```

`--db` (or `$LOAD_PAIRING_DB`) takes a `postgresql://` URL or a SQLite path;
it defaults to `load_pairing.sqlite3` in the working directory. The schema is
created on first connect; `schema/postgres.sql` is the same DDL for a DBA who
would rather apply it by hand.

Useful `plan` options: `--router {auto,pcmiler,google,here,estimated}`,
`--centroids FILE`, `--match-equipment`, `--no-windows`, `--midnight
{no-window,strict}`, `--earliest-start H`, `--max-duty H`, `--max-drive H`,
`--schedule`, `--json`.

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

Pairing means two sequential round trips on one driver, not two loads on one
trailer: every load in the sample runs 21–28 pallets and fills a 53' either
way, so the driver returns to the DC, reloads, and goes back out.

A pair is feasible when combined duty is at most 14 h, combined drive is at
most 11 h, every delivery window can still be met, and — if
`--match-equipment` is set — both loads want the same trailer. Both running
orders are tried and the better one is kept.

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

Spec open decision #1, now implemented. Given a driver's trips, the scheduler
finds a start time that lands every stop inside its window, or reports which
stop it cannot reach in time.

The search is exact rather than a scan over candidate start times: arrival at
every stop is non-decreasing in the start time, so meeting every window close
is downward closed in it, and elapsed duty is non-increasing in it. One
backward pass finds the latest start that violates nothing — also the start
that wastes the least time waiting — and the schedule is then pulled back to
the earliest start that still wastes none, so the driver is not held at the DC
for no reason. Waiting on a window counts against the 14 h duty limit.

Two things are worth knowing:

* **`00:00–00:00` is read as "no window"** by default, which is what the spec
  suspects it means. `--midnight strict` treats it literally instead. This
  still needs confirming against the source system.
* **`--earliest-start` matters more than it looks.** It is the hour before
  which no driver may be dispatched, and it is what makes a hard morning window
  unpairable behind another turn. It defaults to `0.0`, which lets the
  scheduler start a driver at any hour; set it to the real dispatch floor
  (e.g. `--earliest-start 4`) before reading anything into the pair count.

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

77 tests, no dependencies, under a second. They cover the parser against
generated workbooks that reproduce the sheet's quirks, the costing arithmetic,
window feasibility, the lane cache and dwell-override rules, and the planner
end to end through the CLI.

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
