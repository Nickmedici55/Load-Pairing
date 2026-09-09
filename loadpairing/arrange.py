"""Put the loads on drivers by hand and cost the result.

:mod:`loadpairing.pairing` answers "what is the fewest drivers this sheet can
run on". This module answers a different question: "I want *these* loads on one
driver -- what does that do to the day?"

A dispatcher knows things the matcher does not: who is already out, which
receiver will wait, which store the regular driver knows. So the arrangement
here is whatever they say it is, and nothing is rejected. A grouping that
breaks the duty limit or misses a delivery time still comes back costed, with
the breakage named. The point is to show the consequence, not to refuse.

Within one driver the running order is the dispatcher's too. The same pair can
be feasible one way round and impossible the other, so every order is costed and
kept: the one being run, and what each of the others would have cost. That is
what answers "is this pair doable" -- not a verdict, but both answers side by
side.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations
from typing import Iterable, Optional, Sequence

from .models import Assignment, Trip
from .pairing import PairingConfig, Plan
from .windows import Schedule, schedule, schedule_layover

#: Orders are enumerated up to this many loads on one driver (24 permutations).
#: Beyond it the dispatcher's own order stands -- nobody runs six turns anyway.
MAX_ORDERED = 4


class ArrangeError(Exception):
    """Raised when a grouping does not describe the loads it claims to."""


@dataclass(frozen=True)
class Option:
    """One running order for a driver's loads, costed."""

    load_ids: tuple[str, ...]
    feasible: bool
    duty_hours: float = 0.0
    drive_hours: float = 0.0
    wait_hours: float = 0.0
    reason: str = ""

    @property
    def label(self) -> str:
        return " then ".join(self.load_ids)


@dataclass(frozen=True)
class Driver:
    """One driver's worth of an arrangement, costed."""

    load_ids: tuple[str, ...]
    assignment: Optional[Assignment]
    warnings: tuple[str, ...] = ()
    #: Every running order tried, the one being run first.
    options: tuple[Option, ...] = ()

    @property
    def alternatives(self) -> tuple[Option, ...]:
        return self.options[1:]

    @property
    def best_alternative(self) -> Optional[Option]:
        """The cheapest other order that works, if one does."""
        running = self.options[0] if self.options else None
        for option in self.alternatives:
            if not option.feasible:
                continue
            if running is None or not running.feasible:
                return option
            if option.duty_hours < running.duty_hours - 0.05:
                return option
        return None

    @property
    def scheduled(self) -> bool:
        """True when this driver's day could be put on the clock at all."""
        return self.assignment is not None

    @property
    def duty_hours(self) -> float:
        return self.assignment.duty_hours if self.assignment else 0.0

    @property
    def drive_hours(self) -> float:
        return self.assignment.drive_hours if self.assignment else 0.0

    @property
    def wait_hours(self) -> float:
        return max(0.0, self.assignment.wait_hours) if self.assignment else 0.0

    @property
    def is_layover(self) -> bool:
        return bool(self.assignment and self.assignment.is_layover)


@dataclass(frozen=True)
class Arrangement:
    """A whole day laid out the way the dispatcher asked for."""

    drivers: tuple[Driver, ...]
    config: PairingConfig

    @property
    def driver_count(self) -> int:
        return len(self.drivers)

    @property
    def duty_hours(self) -> float:
        return sum(driver.duty_hours for driver in self.drivers)

    @property
    def wait_hours(self) -> float:
        return sum(driver.wait_hours for driver in self.drivers)

    @property
    def problems(self) -> tuple[Driver, ...]:
        """Drivers that break a limit, miss a delivery time, or sleep out."""
        return tuple(driver for driver in self.drivers if driver.warnings)

    @property
    def groups(self) -> tuple[tuple[str, ...], ...]:
        return tuple(driver.load_ids for driver in self.drivers)


def groups_of(plan: Plan) -> tuple[tuple[str, ...], ...]:
    """The grouping a built plan represents, ready to be handed back edited."""
    return tuple(assignment.load_ids for assignment in plan.assignments) + tuple(
        rejection.load_ids for rejection in plan.unschedulable
    )


def arrange(
    trips: Iterable[Trip],
    groups: Sequence[Sequence[str]],
    config: PairingConfig = PairingConfig(),
) -> Arrangement:
    """Cost a grouping of loads onto drivers.

    ``groups`` names one driver per entry. Every load must appear exactly once:
    a plan that drops a load or runs it twice is a mistake worth reporting
    rather than costing.
    """
    by_id = {trip.load.load_id: trip for trip in trips}
    _check(by_id, groups)

    drivers = [_cost(tuple(by_id[load_id] for load_id in group), config) for group in groups]
    drivers.sort(key=lambda driver: (not driver.scheduled, _start_of(driver), driver.load_ids))
    return Arrangement(drivers=tuple(drivers), config=config)


def _start_of(driver: Driver) -> float:
    return driver.assignment.start_hour if driver.assignment else 0.0


def _check(by_id: dict[str, Trip], groups: Sequence[Sequence[str]]) -> None:
    seen: list[str] = [load_id for group in groups for load_id in group]

    unknown = [load_id for load_id in seen if load_id not in by_id]
    if unknown:
        raise ArrangeError(f"no such load on this sheet: {', '.join(sorted(set(unknown)))}")

    twice = sorted({load_id for load_id in seen if seen.count(load_id) > 1})
    if twice:
        raise ArrangeError(f"load on two drivers at once: {', '.join(twice)}")

    missing = sorted(set(by_id) - set(seen))
    if missing:
        raise ArrangeError(f"load left off every driver: {', '.join(missing)}")

    if any(not group for group in groups):
        raise ArrangeError("a driver has no loads")


def _cost(trips: tuple[Trip, ...], config: PairingConfig) -> Driver:
    """Schedule one driver's loads in the order given, and cost the others.

    The order is the dispatcher's: it is run as listed rather than quietly
    improved, because a plan that reorders itself cannot be checked against
    what was asked for. What the other orders would have cost comes back
    alongside, so a pair that only works one way round says so instead of
    simply failing.
    """
    load_ids = tuple(trip.load.load_id for trip in trips)
    warnings: list[str] = []

    drive = sum(trip.drive_hours for trip in trips)
    working = sum(trip.duty_hours for trip in trips)

    if drive > config.max_drive_hours + 1e-9:
        warnings.append(
            f"{drive:.1f} h of driving, over the {config.max_drive_hours:g} h limit"
        )

    options, schedules = _options(trips, config)

    # More work than a shift holds: the driver sleeps out, which is how the
    # planner treats a load too big to pair rather than calling it impossible.
    if working > config.max_duty_hours + 1e-9 or drive > config.max_drive_hours + 1e-9:
        return _layover(
            trips, config, warnings, "more work than one shift holds", options
        )

    running = options[0]

    if not running.feasible:
        warnings.append(running.reason)
        better = Driver(load_ids, None, (), options).best_alternative
        if better is not None:
            warnings.append(
                f"{'the other way round' if len(load_ids) == 2 else better.label} it works: "
                f"{better.duty_hours:.1f} h on duty and {better.drive_hours:.1f} h driving"
            )
        return Driver(load_ids, None, tuple(warnings), options)

    if running.duty_hours > config.max_duty_hours + 1e-9:
        # The work fits a shift; waiting on delivery times is what does not.
        waiting = running.duty_hours - working
        return _layover(
            trips,
            config,
            warnings,
            f"{running.duty_hours:.1f} h on duty once the {waiting:.1f} h waiting on delivery "
            f"windows is counted, over the {config.max_duty_hours:g} h limit",
            options,
        )

    return Driver(load_ids, _assignment(trips, schedules[0]), tuple(warnings), options)


def _options(trips: tuple[Trip, ...], config: PairingConfig):
    """Cost every running order, the one being run first.

    Beyond :data:`MAX_ORDERED` loads the orders are not enumerated: the count
    grows as a factorial, and nobody runs six turns in a day.
    """
    orders = [trips] if len(trips) > MAX_ORDERED else list(permutations(trips))
    costed = []
    for order in orders:
        result = schedule(order, config.windows)
        costed.append((_option(order, result), result))

    running, rest = costed[0], costed[1:]
    rest.sort(key=lambda pair: (not pair[0].feasible, pair[0].duty_hours))
    ordered = [running, *rest]
    return tuple(item[0] for item in ordered), tuple(item[1] for item in ordered)


def _option(trips: tuple[Trip, ...], result: Schedule) -> Option:
    load_ids = tuple(trip.load.load_id for trip in trips)
    if not result.feasible:
        return Option(load_ids, feasible=False, reason=result.reason)
    return Option(
        load_ids,
        feasible=True,
        duty_hours=result.duty_hours,
        drive_hours=sum(trip.drive_hours for trip in trips),
        wait_hours=max(0.0, result.duty_hours - sum(trip.duty_hours for trip in trips)),
    )


def _layover(
    trips: tuple[Trip, ...],
    config: PairingConfig,
    warnings: list[str],
    why: str,
    options: tuple[Option, ...] = (),
) -> Driver:
    """Lay a driver's work across shifts, saying why it did not fit in one.

    Delivery times are not checked across a rest: the sheet gives an hour of
    the day, not a date, so which day a stop is due on is a dispatcher's call.
    """
    result = schedule_layover(trips, config.windows, config.shift_limits)
    warnings.append(
        f"{why} -- a layover of {result.shifts} shifts with {result.rest_hours:.0f} h rest, "
        "and delivery times across the break are a dispatcher's call"
    )
    return Driver(
        tuple(trip.load.load_id for trip in trips),
        _assignment(trips, result),
        tuple(warnings),
        options,
    )


def _assignment(trips: tuple[Trip, ...], result: Schedule) -> Assignment:
    return Assignment(
        trips=trips,
        start_hour=result.start_hour,
        finish_hour=result.finish_hour,
        schedule=result.stops,
        shifts=result.shifts,
        rest_hours=result.rest_hours,
    )
