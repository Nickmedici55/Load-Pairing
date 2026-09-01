"""Load Pairing Tool.

Costs each load on a daily dispatch sheet as a round trip out of the
distribution center and finds loads that can be run back-to-back by one driver
inside HOS limits.
"""

from .costing import AVG_SPEED_MPH, DEFAULT_DWELL_HOURS, LOAD_HOURS_AT_DC, cost_trip
from .models import Assignment, Leg, Load, ScheduledStop, Stop, Trip
from .pairing import MAX_DRIVE_HOURS, MAX_DUTY_HOURS, PairingConfig, Plan, build_trips, plan
from .parsing import parse_workbook
from .windows import WindowPolicy, schedule

__version__ = "0.1.0"

__all__ = [
    "AVG_SPEED_MPH",
    "Assignment",
    "DEFAULT_DWELL_HOURS",
    "LOAD_HOURS_AT_DC",
    "MAX_DRIVE_HOURS",
    "MAX_DUTY_HOURS",
    "Leg",
    "Load",
    "PairingConfig",
    "Plan",
    "ScheduledStop",
    "Stop",
    "Trip",
    "WindowPolicy",
    "build_trips",
    "cost_trip",
    "parse_workbook",
    "plan",
    "schedule",
]
