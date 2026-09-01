"""Cost a load as a round trip out of the distribution center.

    trip duration = 1.0 (load at the DC)
                  + sum of the dwell at each delivery stop
                  + round-trip miles / 50

A stop's dwell is 1.0 h by default, whatever the dispatcher has set for that
location, or 30 minutes if the stop is a drop and hook.

The per-location dwell is the piece that improves with use: a ZIP is inserted
at the 1.0 h default the first time it appears in an uploaded sheet, and from
then on the tool reads whatever the dispatcher has set for it.
"""

from __future__ import annotations

from typing import Callable, Protocol

from .models import Leg, Load, Stop, Trip


class MilesLookup(Protocol):
    """Maps a lane to ``(miles, source)``."""

    def __call__(self, from_zip: str, to_zip: str) -> tuple[float, str]: ...


DwellLookup = Callable[[str], float]

AVG_SPEED_MPH = 50.0
LOAD_HOURS_AT_DC = 1.0
DEFAULT_DWELL_HOURS = 1.0

#: Time on the ground for a drop and hook. A stop whose delivery time reads
#: 00:00 is a trailer swap: the driver drops one and hooks another rather than
#: waiting out a live unload, so it beats the location's dwell.
DROP_AND_HOOK_HOURS = 0.5


def round_trip_zips(dc_zip: str, load: Load) -> tuple[tuple[str, str], ...]:
    """The legs of a round trip: DC out, stop to stop, and back to the DC."""
    points = [dc_zip, *load.zips, dc_zip]
    return tuple((points[i], points[i + 1]) for i in range(len(points) - 1))


def cost_trip(
    load: Load,
    dc_zip: str,
    miles_for: MilesLookup,
    dwell_for: DwellLookup,
    load_hours: float = LOAD_HOURS_AT_DC,
) -> Trip:
    """Build a costed :class:`Trip` for one load.

    ``miles_for`` maps a ``(from_zip, to_zip)`` pair to ``(miles, source)`` and
    ``dwell_for`` maps a ZIP to its dwell in hours.
    """
    legs = []
    for from_zip, to_zip in round_trip_zips(dc_zip, load):
        miles, source = miles_for(from_zip, to_zip)
        legs.append(Leg(from_zip=from_zip, to_zip=to_zip, miles=float(miles), source=source))

    dwell = tuple(stop_dwell(stop, dwell_for) for stop in load.stops)
    return Trip(load=load, legs=tuple(legs), dwell_hours=dwell, load_hours=float(load_hours))


def stop_dwell(stop: Stop, dwell_for: DwellLookup) -> float:
    """Time on the ground at one stop.

    A drop and hook is half an hour whatever the location's dwell says: the
    dispatcher's override describes how long a live unload takes there, and a
    trailer swap is not one.
    """
    if stop.is_drop_and_hook:
        return DROP_AND_HOOK_HOURS
    return float(dwell_for(stop.zip))


def solo_hours(trip: Trip) -> float:
    return trip.duty_hours
