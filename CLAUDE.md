# Load Pairing Tool — Project Spec

Takes a daily dispatch spreadsheet, costs each load as a round trip out of the
distribution center, and finds loads that can be run back-to-back by one driver
inside HOS limits.

Origin sheet: `NESC08_28.xlsx` — New England SC, Chicopee MA. Tab 3
(`Dispatch Order_3`), carrier ID `PTAG`. 34 PTAG loads, 96 stops.

## Domain model

Every load is a **round trip**: depart Chicopee loaded, hit every delivery stop
in the sheet, return to Chicopee empty. The stops in the sheet are deliveries
only — the origin is implicit and is always the DC.

**"Pairing" means two sequential round trips on one driver**, not two loads on
one trailer. Every load in the sample runs 21–28 pallets, which fills a 53'
either way. The driver returns to Chicopee, reloads, and goes back out.

### Time model

| Component | Rule |
|---|---|
| Load at DC | 1.0 h, once per trip |
| Each delivery stop | 1.0 h default, **overridable per location** |
| Drop and hook stop | 0.5 h, fixed — beats the location override |
| Line haul | miles ÷ 50 mph |

Trip duration = 1.0 + Σ(stop dwell) + (round-trip miles / 50).

`Window Close` is the delivery time: the hour the load is due at that stop. A
close of `00:00` means the end of that day (23:59), not the start of it, and
marks the stop as a **drop and hook** — the driver swaps trailers rather than
waiting out a live unload, so that stop costs 0.5 h whatever the location's
dwell says.

The per-location dwell override is the core feature. A location is inserted at
the 1.0 h default the first time its ZIP appears in any uploaded sheet. From
then on the app reads whatever the dispatcher has set. Data quality improves
with use.

### Pairing constraints

- Combined duty time ≤ 14 h
- Combined drive time ≤ 11 h
- A load whose own duty or drive is over those limits is **not rejected** — it
  runs solo with a layover, the driver sleeping out and finishing the next
  day. It cannot be paired, being already more than a shift.
- Delivery windows must be satisfiable in sequence — **implemented**
- The sheet's stop order is a suggestion. When it cannot meet every delivery
  time the stops are reordered and the load says so; the sheet's order always
  wins when it works. Tab 2 holding the same loads in the opposite sequence is
  the precedent for treating order as advisory.
- Trailer type compatibility — **not yet decided**

Matching is a max-weight maximum-cardinality matching over the graph of
feasible pairs. `networkx.max_weight_matching(G, maxcardinality=True)`.

## Schema (Postgres)

```sql
CREATE TABLE location (
    zip          TEXT PRIMARY KEY,
    city         TEXT,
    state        TEXT,
    lat          DOUBLE PRECISION,
    lon          DOUBLE PRECISION,
    dwell_hours  NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    first_seen   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE lane (
    from_zip    TEXT NOT NULL,
    to_zip      TEXT NOT NULL,
    miles       NUMERIC(7,1) NOT NULL,
    source      TEXT NOT NULL,          -- 'pcmiler' | 'google' | 'here' | 'estimated'
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_zip, to_zip)
);
```

Lanes are cached permanently on first fetch. `source = 'estimated'` marks rows
that came from the offline fallback (great-circle × 1.20 circuity) rather than a
routing API — those can be backfilled with real miles later without touching
anything else.

Mileage should come from a truck-legal routing source. These loads run Class 8
into Lake Placid, Massena, and Plattsburgh NY, where car routing and truck
routing diverge meaningfully. PC*Miler is the preferred source if a license is
available.

## Spreadsheet parsing

The sheet layout is nonstandard and the parser depends on its quirks:

- Rows 0–5 are a title block. The real header is at row 6.
- **The header row repeats before every load block.** Skip any row where
  `Carrier ID == 'Carrier ID'`.
- A load block starts at the row where `Carrier ID` is non-empty. Subsequent
  rows with a blank `Carrier ID` but a populated `Store` are additional stops on
  that same load.
- Columns used: `Carrier ID`, `Load ID`, `Trailer Equipment Type`, `Order`,
  `Store`, `Window Open`, `Window Close`, `City`, `State`, `Zip`,
  `Total Pallets Shipped`.
- Carrier ID defaults to `PTAG`; reading every carrier on the sheet is opt-in.
- ZIPs read back from pandas as floats. Cast and zero-pad to 5 characters.
- Tabs 2 and 3 (`Reverse Order_2`, `Dispatch Order_3`) contain the same loads in
  opposite stop sequence. Use tab 3.

## Open decisions

**1. Delivery window enforcement — settled and implemented.** Of the 34 PTAG
loads, 23 have wide 11:00–20:00 windows and pair freely. Eight have hard
morning windows — first stop opening at 05:15, 05:45, 06:14, 06:45, or 06:59.
Two loads with early windows cannot be paired, because both need to be the
first turn out.

The three loads showing 00:00–00:00 are **due by 23:59 that night** and are
**drop and hooks**, 0.5 h on the ground. They are not "no window" and they are
not midnight at the start of the day.

The driver leaves Chicopee at whatever hour lands them at the first stop of a
turn exactly as `Window Open` says it opens — no earlier, so nobody sits at a
receiver's door, and no later, so nothing downstream is given away. On the
second turn of a pair the driver holds at the DC rather than at the customer.
Only a real "no dispatch before" hour overrides that anchor, and then the day
starts as late as it can without missing a delivery time.

The pairing logic runs a real feasibility check rather than flagging suspect
pairs: it rejects a pair when no start time lands every stop at or before its
delivery time. Waiting on a stop that has not opened counts against the 14 h
duty limit.

**2. Trailer type.** Equipment types present: `53LG` (liftgate), `53PLG`
(liftgate pinwheel), `53RL` (roll door), `53PRL` (roll pinwheel), `48PLG`. The
current matching pairs across types, which only works if the driver drops and
hooks a different trailer at Chicopee between turns. If that's not operationally
allowed, restrict pairs to matching equipment. There's a `MATCH_EQUIP` flag in
the prototype for this.

## Baseline result

For reference — against the sample sheet, with estimated mileage and no window
enforcement:

- 34 loads, 242 total hours if each runs solo
- 15 pairs + 4 solo = **19 drivers instead of 34**
- Load `10375781` (Plattsburgh / Massena / Lake Placid NY, 574 mi, 15.5 h)
  exceeds the 14 h duty limit on its own, so it runs as a layover
- Shortest load is `10375774` (Holyoke MA, 1 stop, 9 mi round trip, 2.2 h)

Adding window enforcement will reduce the pair count. That's expected, not a
regression.

## Prior art

`load_pairing_prototype.py` is a working offline version: parsing, costing, the
SQLite version of the schema, and the matching. Logic is sound and verified
against the sample sheet. It needs porting to Postgres and a real mileage
source, not rewriting.
