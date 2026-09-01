"""Find loads that can be run back-to-back by one driver.

Pairing here means two sequential round trips on one driver, not two loads on
one trailer: every load in the sample runs 21-28 pallets and fills a 53' either
way, so the driver returns to Chicopee, reloads, and goes back out.

A pair is feasible when, in one order or the other:

* combined duty time is at most 14 h,
* combined drive time is at most 11 h,
* every delivery window can still be met (see :mod:`loadpairing.windows`),
* and, when ``match_equipment`` is on, both loads want the same trailer.

Feasible pairs become edges of a graph and the plan is a maximum-cardinality,
maximum-weight matching over it -- fewest drivers first, then the tie-break
named by ``objective``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .costing import DEFAULT_DWELL_HOURS, cost_trip
from .matching import max_weight_matching
from .models import Assignment, Load, Trip
from .windows import Schedule, WindowPolicy, schedule

MAX_DUTY_HOURS = 14.0
MAX_DRIVE_HOURS = 11.0

#: Spec open decision #2. Equipment types in the sample are 53LG, 53PLG, 53RL,
#: 53PRL and 48PLG. Pairing across types only works if the driver drops and
#: hooks a different trailer at the DC between turns; turn this on to forbid it.
MATCH_EQUIP = False

OBJECTIVE_DUTY = "duty"
OBJECTIVE_WAIT = "wait"


@dataclass(frozen=True)
class PairingConfig:
    dc_zip: str = "01020"
    max_duty_hours: float = MAX_DUTY_HOURS
    max_drive_hours: float = MAX_DRIVE_HOURS
    match_equipment: bool = MATCH_EQUIP
    objective: str = OBJECTIVE_DUTY
    windows: WindowPolicy = field(default_factory=WindowPolicy)
    matcher: str = "auto"


@dataclass(frozen=True)
class Candidate:
    """A feasible pair, in the order that works."""

    first: Trip
    second: Trip
    schedule: Schedule

    @property
    def load_ids(self) -> tuple[str, str]:
        return self.first.load.load_id, self.second.load.load_id

    @property
    def duty_hours(self) -> float:
        return self.schedule.duty_hours

    @property
    def drive_hours(self) -> float:
        return self.first.drive_hours + self.second.drive_hours

    @property
    def wait_hours(self) -> float:
        return self.duty_hours - (self.first.duty_hours + self.second.duty_hours)


@dataclass(frozen=True)
class Rejection:
    load_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Plan:
    assignments: tuple[Assignment, ...]
    unschedulable: tuple[Rejection, ...]
    candidates: tuple[Candidate, ...]
    matcher: str
    config: PairingConfig

    @property
    def drivers(self) -> int:
        return len(self.assignments)

    @property
    def pairs(self) -> tuple[Assignment, ...]:
        return tuple(a for a in self.assignments if a.is_pair)

    @property
    def solos(self) -> tuple[Assignment, ...]:
        return tuple(a for a in self.assignments if not a.is_pair)

    @property
    def load_count(self) -> int:
        return sum(len(a.trips) for a in self.assignments) + len(self.unschedulable)

    @property
    def solo_hours(self) -> float:
        """Driver hours if every schedulable load ran on its own."""
        return sum(trip.duty_hours for a in self.assignments for trip in a.trips)

    @property
    def estimated_mileage(self) -> bool:
        return any(trip.estimated for a in self.assignments for trip in a.trips)


def build_trips(
    loads: Iterable[Load],
    miles_for,
    dwell_for=None,
    config: PairingConfig = PairingConfig(),
) -> list[Trip]:
    dwell_for = dwell_for or (lambda _zip: DEFAULT_DWELL_HOURS)
    return [cost_trip(load, config.dc_zip, miles_for, dwell_for) for load in loads]


def solo_schedule(trip: Trip, config: PairingConfig) -> Schedule:
    return schedule([trip], config.windows)


def pair_schedule(first: Trip, second: Trip, config: PairingConfig) -> Schedule:
    return schedule([first, second], config.windows)


def evaluate_pair(first: Trip, second: Trip, config: PairingConfig) -> Candidate | Rejection:
    """Test one unordered pair, trying both running orders."""
    ids = (first.load.load_id, second.load.load_id)

    if config.match_equipment and first.load.equipment != second.load.equipment:
        return Rejection(ids, f"equipment {first.load.equipment} != {second.load.equipment}")

    drive = first.drive_hours + second.drive_hours
    if drive > config.max_drive_hours + 1e-9:
        return Rejection(ids, f"combined drive {drive:.1f} h over the {config.max_drive_hours:g} h limit")

    working = first.duty_hours + second.duty_hours
    if working > config.max_duty_hours + 1e-9:
        return Rejection(ids, f"combined duty {working:.1f} h over the {config.max_duty_hours:g} h limit")

    best: Optional[Candidate] = None
    reasons: list[str] = []
    for a, b in ((first, second), (second, first)):
        result = pair_schedule(a, b, config)
        if not result.feasible:
            reasons.append(f"{a.load.load_id} then {b.load.load_id}: {result.reason}")
            continue
        if result.duty_hours > config.max_duty_hours + 1e-9:
            reasons.append(
                f"{a.load.load_id} then {b.load.load_id}: {result.duty_hours:.1f} h "
                f"on duty once waiting on windows is counted"
            )
            continue
        candidate = Candidate(first=a, second=b, schedule=result)
        if best is None or candidate.duty_hours < best.duty_hours:
            best = candidate

    if best is None:
        return Rejection(ids, "; ".join(reasons) or "no feasible order")
    return best


def _weight(candidate: Candidate, config: PairingConfig) -> int:
    """Integer tie-break weight; cardinality still comes first."""
    if config.objective == OBJECTIVE_WAIT:
        return int(round((config.max_duty_hours - candidate.wait_hours) * 100))
    return int(round(candidate.duty_hours * 100))


def plan(trips: Iterable[Trip], config: PairingConfig = PairingConfig()) -> Plan:
    """Build the driver plan for one day's loads."""
    trips = list(trips)
    schedulable: list[Trip] = []
    unschedulable: list[Rejection] = []
    solo: dict[str, Schedule] = {}

    for trip in trips:
        if trip.drive_hours > config.max_drive_hours + 1e-9:
            unschedulable.append(
                Rejection(
                    (trip.load.load_id,),
                    f"{trip.drive_hours:.1f} h driving exceeds the "
                    f"{config.max_drive_hours:g} h limit on its own",
                )
            )
            continue
        result = solo_schedule(trip, config)
        if not result.feasible:
            unschedulable.append(Rejection((trip.load.load_id,), result.reason))
            continue
        if result.duty_hours > config.max_duty_hours + 1e-9:
            unschedulable.append(
                Rejection(
                    (trip.load.load_id,),
                    f"{result.duty_hours:.1f} h on duty exceeds the "
                    f"{config.max_duty_hours:g} h limit and cannot run in a single shift",
                )
            )
            continue
        schedulable.append(trip)
        solo[trip.load.load_id] = result

    candidates: list[Candidate] = []
    by_ids: dict[frozenset, Candidate] = {}
    for i in range(len(schedulable)):
        for j in range(i + 1, len(schedulable)):
            outcome = evaluate_pair(schedulable[i], schedulable[j], config)
            if isinstance(outcome, Candidate):
                candidates.append(outcome)
                by_ids[frozenset(outcome.load_ids)] = outcome

    edges = [(*candidate.load_ids, _weight(candidate, config)) for candidate in candidates]
    matching, matcher = max_weight_matching(edges, maxcardinality=True, prefer=config.matcher)

    assignments: list[Assignment] = []
    paired: set[str] = set()
    for pair in matching:
        candidate = by_ids[pair]
        paired.update(candidate.load_ids)
        assignments.append(
            Assignment(
                trips=(candidate.first, candidate.second),
                start_hour=candidate.schedule.start_hour,
                finish_hour=candidate.schedule.finish_hour,
                schedule=candidate.schedule.stops,
            )
        )

    for trip in schedulable:
        if trip.load.load_id in paired:
            continue
        result = solo[trip.load.load_id]
        assignments.append(
            Assignment(
                trips=(trip,),
                start_hour=result.start_hour,
                finish_hour=result.finish_hour,
                schedule=result.stops,
            )
        )

    assignments.sort(key=lambda a: (a.start_hour, a.load_ids))
    return Plan(
        assignments=tuple(assignments),
        unschedulable=tuple(unschedulable),
        candidates=tuple(candidates),
        matcher=matcher if edges else "none",
        config=config,
    )
