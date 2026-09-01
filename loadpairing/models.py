"""Core domain objects.

Every load is a round trip out of the distribution center: depart the DC
loaded, hit every delivery stop in sheet order, return to the DC empty. The
stops that appear in a dispatch sheet are deliveries only -- the origin is
implicit and is always the DC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

NO_WINDOW = None


@dataclass(frozen=True)
class Stop:
    """One delivery on a load, in the sequence the sheet lists it."""

    order: str
    store: str
    zip: str
    city: str = ""
    state: str = ""
    pallets: float = 0.0
    window_open: Optional[float] = NO_WINDOW    # hours past midnight
    window_close: Optional[float] = NO_WINDOW   # hours past midnight

    @property
    def has_window(self) -> bool:
        return self.window_open is not None and self.window_close is not None

    @property
    def delivery_time(self) -> Optional[float]:
        """The appointment: Window Close is when the load is due."""
        return self.window_close

    @property
    def is_drop_and_hook(self) -> bool:
        """A 00:00 delivery time means a trailer swap, not a live unload.

        It also means end of that day rather than the start of it -- see
        :data:`loadpairing.windows.END_OF_DAY`.
        """
        return self.window_close == 0.0

    def __str__(self) -> str:
        where = ", ".join(p for p in (self.city, self.state) if p)
        return f"{self.store} ({where} {self.zip})" if where else f"{self.store} ({self.zip})"


@dataclass(frozen=True)
class Load:
    """A single round trip's worth of freight."""

    load_id: str
    carrier_id: str
    equipment: str
    stops: tuple[Stop, ...]

    @property
    def zips(self) -> tuple[str, ...]:
        return tuple(s.zip for s in self.stops)

    @property
    def pallets(self) -> float:
        return sum(s.pallets for s in self.stops)

    def __str__(self) -> str:
        return f"{self.load_id} ({self.equipment}, {len(self.stops)} stops)"


@dataclass(frozen=True)
class Leg:
    """One line-haul movement between two ZIPs."""

    from_zip: str
    to_zip: str
    miles: float
    source: str

    @property
    def hours(self) -> float:
        from .costing import AVG_SPEED_MPH

        return self.miles / AVG_SPEED_MPH


@dataclass(frozen=True)
class Trip:
    """A costed round trip for one load."""

    load: Load
    legs: tuple[Leg, ...]
    dwell_hours: tuple[float, ...]   # parallel to load.stops
    load_hours: float

    @property
    def miles(self) -> float:
        return sum(leg.miles for leg in self.legs)

    @property
    def drive_hours(self) -> float:
        return sum(leg.hours for leg in self.legs)

    @property
    def stop_hours(self) -> float:
        return sum(self.dwell_hours)

    @property
    def duty_hours(self) -> float:
        """Total on-duty time with no waiting on closed delivery windows."""
        return self.load_hours + self.stop_hours + self.drive_hours

    @property
    def estimated(self) -> bool:
        """True when any leg's mileage came from the offline fallback."""
        return any(leg.source == "estimated" for leg in self.legs)


@dataclass(frozen=True)
class Assignment:
    """One driver's day: either a single trip or two run back-to-back."""

    trips: tuple[Trip, ...]
    start_hour: float
    finish_hour: float
    schedule: tuple["ScheduledStop", ...] = field(default_factory=tuple)
    shifts: int = 1
    rest_hours: float = 0.0

    @property
    def is_pair(self) -> bool:
        return len(self.trips) == 2

    @property
    def is_layover(self) -> bool:
        """More work than one shift holds: the driver sleeps out."""
        return self.shifts > 1

    @property
    def load_ids(self) -> tuple[str, ...]:
        return tuple(t.load.load_id for t in self.trips)

    @property
    def drive_hours(self) -> float:
        return sum(t.drive_hours for t in self.trips)

    @property
    def duty_hours(self) -> float:
        """On-duty time: elapsed, less any rest taken on a layover."""
        return self.finish_hour - self.start_hour - self.rest_hours

    @property
    def working_hours(self) -> float:
        """On-duty time excluding waiting."""
        return sum(t.duty_hours for t in self.trips)

    @property
    def wait_hours(self) -> float:
        return self.duty_hours - self.working_hours


@dataclass(frozen=True)
class ScheduledStop:
    """Where a stop lands on the clock once a start time is chosen."""

    load_id: str
    stop: Stop
    arrive: float
    depart: float
    dwell: float

    @property
    def wait(self) -> float:
        """Time spent sitting on a window that has not opened yet."""
        return max(0.0, self.depart - self.arrive - self.dwell)
