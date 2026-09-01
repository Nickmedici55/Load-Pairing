"""Delivery-window feasibility.

Spec open decision #1: the matcher used to ignore windows and only flag
suspect pairs. This module makes it a real check -- given a sequence of trips
run back-to-back, it decides whether a start time exists that lands every stop
inside its window, and returns the resulting schedule.

Two facts about the simulation make an exact answer cheap:

* Arrival at every stop is non-decreasing in the start time, so "no stop
  arrives after its window closes" is downward closed -- if a start works,
  every earlier start works too.
* Elapsed duty (finish minus start) is non-increasing in the start time,
  because starting later burns waiting time rather than adding to it.

So the best start is the latest one that violates no window close. A backward
pass over the stops finds it in one sweep; a forward pass from there produces
the schedule and the duty total to test against the 14 h limit.

Times are hours past midnight on the day the driver starts. A window whose
close is before its open is read as running past midnight and its close is
pushed a day out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import ScheduledStop, Stop

MIDNIGHT_NO_WINDOW = "no-window"
MIDNIGHT_STRICT = "strict"

#: How to read a 00:00-00:00 window. The spec flags this as unconfirmed: it
#: most likely means "no window" rather than a stop that must be hit exactly at
#: midnight, so that is the default.
DEFAULT_MIDNIGHT_POLICY = MIDNIGHT_NO_WINDOW

OPEN_ENDED = (-math.inf, math.inf)


@dataclass(frozen=True)
class WindowPolicy:
    """How windows are read and when a driver may start."""

    enforce: bool = True
    midnight: str = DEFAULT_MIDNIGHT_POLICY
    earliest_start: float = 0.0
    latest_start: float = 24.0

    def window_for(self, stop: Stop) -> tuple[float, float]:
        if not self.enforce or not stop.has_window:
            return OPEN_ENDED
        open_at, close_at = float(stop.window_open), float(stop.window_close)
        if open_at == 0.0 and close_at == 0.0 and self.midnight == MIDNIGHT_NO_WINDOW:
            return OPEN_ENDED
        if close_at < open_at:                 # window runs past midnight
            close_at += 24.0
        return open_at, close_at


@dataclass(frozen=True)
class Schedule:
    """The outcome of scheduling one driver's trips."""

    feasible: bool
    start_hour: float = 0.0
    finish_hour: float = 0.0
    stops: tuple[ScheduledStop, ...] = ()
    reason: str = ""

    @property
    def duty_hours(self) -> float:
        return self.finish_hour - self.start_hour


@dataclass(frozen=True)
class _Task:
    """One stop flattened out of the trip sequence, with its travel and dwell."""

    load_id: str
    stop: Stop
    travel_in: float     # driving hours from the previous point to this stop
    dwell: float
    open_at: float
    close_at: float


def flatten(trips, policy: WindowPolicy) -> tuple[list[_Task], float]:
    """Flatten a driver's trips into one task list.

    Back-to-back trips are one long sequence: load at the DC, deliver, drive
    home, reload, deliver again, drive home. The reload and the run back out
    ride along as the next stop's inbound travel, so the only piece left over
    is the final run home, returned as the tail.
    """
    tasks: list[_Task] = []
    pending = 0.0

    for trip in trips:
        if not trip.load.stops:
            raise ValueError(f"load {trip.load.load_id} has no delivery stops")
        stop_legs = trip.legs[:-1]           # the last leg is the run home
        pending += trip.load_hours + stop_legs[0].hours

        for index, stop in enumerate(trip.load.stops):
            travel_in = pending if index == 0 else stop_legs[index].hours
            open_at, close_at = policy.window_for(stop)
            tasks.append(
                _Task(
                    load_id=trip.load.load_id,
                    stop=stop,
                    travel_in=travel_in,
                    dwell=trip.dwell_hours[index],
                    open_at=open_at,
                    close_at=close_at,
                )
            )
            pending = 0.0

        pending = trip.legs[-1].hours

    return tasks, pending


def latest_start(tasks: list[_Task]) -> float:
    """The latest start hour that violates no window close, or +inf."""
    latest_arrival = math.inf
    for task in reversed(tasks):
        latest_arrival = min(task.close_at, latest_arrival - task.dwell)
        latest_arrival -= task.travel_in
    return latest_arrival


def schedule(trips, policy: WindowPolicy = WindowPolicy()) -> Schedule:
    """Place one driver's trips on the clock.

    Starts as late as every window close allows -- that is the start that
    wastes the least time waiting -- then pulls back to the earliest start that
    still wastes none, so the driver is not held at the DC for no reason.
    """
    trips = tuple(trips)
    if not trips:
        return Schedule(feasible=False, reason="no trips")

    tasks, tail = flatten(trips, policy)
    if not tasks:
        return Schedule(feasible=False, reason="no delivery stops")

    start = min(latest_start(tasks), policy.latest_start)
    if start < policy.earliest_start:
        late = _first_violation(tasks, policy.earliest_start)
        return Schedule(
            feasible=False,
            reason=(
                f"cannot reach {late} before its window closes"
                if late
                else "no start time satisfies every delivery window"
            ),
        )

    start -= _idle_slack(tasks, start, floor=policy.earliest_start)
    scheduled, finish = _walk(tasks, start, tail)
    if scheduled is None:
        return Schedule(feasible=False, reason=finish)
    return Schedule(feasible=True, start_hour=start, finish_hour=float(finish), stops=tuple(scheduled))


def _idle_slack(tasks: list[_Task], start: float, floor: float) -> float:
    """How much earlier the day can start without anyone waiting on a window."""
    slack = start - floor
    clock = start
    for task in tasks:
        arrive = clock + task.travel_in
        slack = min(slack, arrive - task.open_at)
        clock = max(arrive, task.open_at) + task.dwell
    return max(0.0, slack)


def _walk(tasks: list[_Task], start: float, tail: float):
    """Walk the day forward from ``start``; returns (stops, finish) or (None, why)."""
    scheduled: list[ScheduledStop] = []
    clock = start
    for task in tasks:
        arrive = clock + task.travel_in
        if arrive > task.close_at + 1e-9:
            return None, f"arrives at {task.stop} after its window closes"
        depart = max(arrive, task.open_at) + task.dwell
        scheduled.append(
            ScheduledStop(
                load_id=task.load_id,
                stop=task.stop,
                arrive=arrive,
                depart=depart,
                dwell=task.dwell,
            )
        )
        clock = depart
    return scheduled, clock + tail


def _first_violation(tasks: list[_Task], start: float) -> str:
    """Name the stop that makes an earliest-possible start still too late."""
    clock = start
    for task in tasks:
        arrive = clock + task.travel_in
        if arrive > task.close_at + 1e-9:
            return str(task.stop)
        clock = max(arrive, task.open_at) + task.dwell
    return ""
