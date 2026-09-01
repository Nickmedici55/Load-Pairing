"""Delivery-window feasibility.

Spec open decision #1: the matcher used to ignore windows and only flag
suspect pairs. This module makes it a real check -- given a sequence of trips
run back-to-back, it decides whether a start time exists that lands every stop
inside its window, and returns the resulting schedule.

The driver leaves Chicopee at whatever hour lands them at the first stop of a
turn exactly as that stop opens -- not earlier, so nobody sits at a receiver's
door, and not later, so nothing downstream is given away. That is the anchor.
It is also the earliest the trip can possibly progress, since a stop cannot be
served before it opens, so it is the schedule most likely to meet every later
delivery time.

When the anchor would need a dispatch earlier than the operation allows, the
day falls back to the latest start that misses nothing, pulled back to the
earliest start that wastes no time waiting. Two facts make that exact answer
cheap:

* Arrival at every stop is non-decreasing in the start time, so "no stop
  arrives after its window closes" is downward closed -- if a start works,
  every earlier start works too.
* Elapsed duty (finish minus start) is non-increasing in the start time,
  because starting later burns waiting time rather than adding to it.

So the latest start that violates nothing is found by one backward pass over
the stops, and a forward pass from there produces the schedule and the duty
total to test against the 14 h limit.

Times are hours past midnight on the day the driver starts. Window Close is
the delivery time -- the appointment the load is due by -- and Window Open is
the earliest the receiver will take it. A close of 00:00 means the end of that
day rather than the start of it, so it is read as 23:59 (and marks the stop as
a drop and hook, which :mod:`loadpairing.costing` prices at 30 minutes). Any
other window whose close falls before its open runs past midnight and has its
close pushed a day out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import ScheduledStop, Stop

#: What a 00:00 delivery time means: the end of that day, not the start of it.
END_OF_DAY = 23.0 + 59.0 / 60.0

OPEN_ENDED = (-math.inf, math.inf)


@dataclass(frozen=True)
class WindowPolicy:
    """How windows are read and when a driver may start."""

    enforce: bool = True
    earliest_start: float = 0.0
    latest_start: float = 24.0

    def window_for(self, stop: Stop) -> tuple[float, float]:
        """The hours between which the stop will take the load."""
        if not self.enforce or not stop.has_window:
            return OPEN_ENDED

        open_at, close_at = float(stop.window_open), float(stop.window_close)
        if close_at == 0.0:                    # due by end of day, not midnight
            close_at = END_OF_DAY
        elif close_at < open_at:               # window runs past midnight
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
    first_of_turn: bool = False   # the driver comes to this one straight from the DC


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
                    first_of_turn=index == 0,
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


def anchored_start(tasks: list[_Task]) -> float:
    """The hour to leave the DC to reach the first stop exactly as it opens.

    ``-inf`` when the first stop has no opening hour to aim at.
    """
    first = tasks[0]
    if not math.isfinite(first.open_at):
        return -math.inf
    return first.open_at - first.travel_in


def schedule(trips, policy: WindowPolicy = WindowPolicy()) -> Schedule:
    """Place one driver's trips on the clock."""
    trips = tuple(trips)
    if not trips:
        return Schedule(feasible=False, reason="no trips")

    tasks, tail = flatten(trips, policy)
    if not tasks:
        return Schedule(feasible=False, reason="no delivery stops")

    start = anchored_start(tasks)
    if start < policy.earliest_start:
        # Too early to dispatch. Fall back to the latest start that misses
        # nothing, then pull back to the earliest that wastes no waiting.
        start = min(latest_start(tasks), policy.latest_start)
        if start < policy.earliest_start:
            _stops, why = _walk(tasks, policy.earliest_start, tail)
            return Schedule(
                feasible=False,
                reason=why if _stops is None else "no start time satisfies every delivery time",
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
            return None, (
                f"cannot reach {task.stop} by {format_hour(task.close_at)}; "
                f"leaving the DC at {format_hour(start)} the earliest arrival "
                f"is {format_hour(arrive)}"
            )
        if task.first_of_turn and arrive < task.open_at:
            # The driver holds at the DC rather than at the receiver's door.
            arrive = task.open_at
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


def format_hour(hours: float) -> str:
    """``6.25`` -> ``06:15``; anything past midnight is marked ``+1d``."""
    day, rest = divmod(hours, 24.0)
    minutes = int(round(rest * 60))
    if minutes == 1440:
        day, minutes = day + 1, 0
    text = f"{minutes // 60:02d}:{minutes % 60:02d}"
    return f"{text} +{int(day)}d" if day >= 1 else text
