"""Human-readable and JSON renderings of a plan."""

from __future__ import annotations

from .models import Assignment
from .pairing import Plan
from .windows import END_OF_DAY, format_hour


#: Hours are rendered the same way wherever they appear.
clock = format_hour


def delivery_time(stop) -> float:
    """Window Close is the delivery time; 00:00 means the end of that day."""
    return END_OF_DAY if stop.window_close == 0.0 else stop.window_close


def window(stop) -> str:
    if not stop.has_window:
        return "any"
    text = f"{clock(stop.window_open)}-{clock(delivery_time(stop))}"
    return f"{text} D&H" if stop.is_drop_and_hook else text


def plural(count: int, noun: str, suffix: str = "s") -> str:
    return f"{count} {noun}" + ("" if count == 1 else suffix)


def render(plan: Plan, show_schedule: bool = False) -> str:
    lines: list[str] = []
    config = plan.config

    single_shift_solos = [a for a in plan.solos if not a.is_layover]
    makeup = f"{plural(len(plan.pairs), 'pair')} + {len(single_shift_solos)} solo"
    if plan.layovers:
        makeup += f" + {plural(len(plan.layovers), 'layover')}"
    if plan.unschedulable:
        makeup += f", {len(plan.unschedulable)} unschedulable"
    lines.append(f"{plural(plan.load_count, 'load')} -> {plural(plan.drivers, 'driver')} ({makeup})")
    lines.append(
        f"{plan.solo_hours:.1f} h of driver time if every schedulable load ran on its own; "
        f"{plural(len(plan.candidates), 'feasible pair')} found, matched by {plan.matcher}"
    )
    lines.append(
        f"limits: {config.max_duty_hours:g} h duty, {config.max_drive_hours:g} h drive; "
        f"windows {'enforced' if config.windows.enforce else 'ignored'}; "
        f"equipment {'must match' if config.match_equipment else 'may differ'}"
    )
    if plan.resequenced:
        lines.append(
            f"note: {plural(len(plan.resequenced), 'load')} had stops reordered -- the sheet's "
            "order could not meet every delivery time"
        )
    if plan.estimated_mileage:
        lines.append("note: some lanes use estimated mileage (great-circle x 1.20), not a routing source")
    lines.append("")

    for index, assignment in enumerate(plan.assignments, start=1):
        lines.append(f"Driver {index:>2}  {_headline(assignment)}")
        if show_schedule:
            for scheduled in assignment.schedule:
                waited = f"  waited {scheduled.wait * 60:.0f}m" if scheduled.wait > 1e-6 else ""
                lines.append(
                    f"            {scheduled.load_id}  {clock(scheduled.arrive)}"
                    f"-{clock(scheduled.depart)}  {scheduled.stop}"
                    f"  window {window(scheduled.stop)}{waited}"
                )

    if plan.unschedulable:
        lines.append("")
        lines.append("Unschedulable:")
        for rejection in plan.unschedulable:
            lines.append(f"  {', '.join(rejection.load_ids)}: {rejection.reason}")

    return "\n".join(lines)


def _headline(assignment: Assignment) -> str:
    trips = " + ".join(
        f"{trip.load.load_id} ({trip.load.equipment}, {plural(len(trip.load.stops), 'stop')}, "
        f"{trip.miles:.0f} mi, {trip.duty_hours:.1f} h"
        + (", resequenced" if trip.resequenced else "")
        + ")"
        for trip in assignment.trips
    )
    tail = (
        f"  ->  {clock(assignment.start_hour)}-{clock(assignment.finish_hour)}  "
        f"{assignment.duty_hours:.1f} h duty, {assignment.drive_hours:.1f} h drive"
    )
    if assignment.wait_hours > 1e-6:
        tail += f", {assignment.wait_hours:.1f} h waiting"
    if assignment.is_layover:
        tail += (
            f"  [layover: {assignment.shifts} shifts, "
            f"{assignment.rest_hours:.0f} h rest; delivery times need a dispatcher]"
        )
    return trips + tail


def as_dict(plan: Plan) -> dict:
    return {
        "loads": plan.load_count,
        "drivers": plan.drivers,
        "pairs": len(plan.pairs),
        "solos": len(plan.solos),
        "layovers": len(plan.layovers),
        "resequenced": [trip.load.load_id for trip in plan.resequenced],
        "solo_hours": round(plan.solo_hours, 2),
        "matcher": plan.matcher,
        "feasible_pairs": len(plan.candidates),
        "estimated_mileage": plan.estimated_mileage,
        "config": {
            "dc_zip": plan.config.dc_zip,
            "max_duty_hours": plan.config.max_duty_hours,
            "max_drive_hours": plan.config.max_drive_hours,
            "match_equipment": plan.config.match_equipment,
            "objective": plan.config.objective,
            "enforce_windows": plan.config.windows.enforce,
        },
        "assignments": [
            {
                "loads": list(assignment.load_ids),
                "start": round(assignment.start_hour, 3),
                "finish": round(assignment.finish_hour, 3),
                "duty_hours": round(assignment.duty_hours, 2),
                "drive_hours": round(assignment.drive_hours, 2),
                "shifts": assignment.shifts,
                "layover": assignment.is_layover,
                "rest_hours": round(assignment.rest_hours, 2),
                "wait_hours": round(assignment.wait_hours, 2) or 0.0,
                "miles": round(sum(trip.miles for trip in assignment.trips), 1),
                "equipment": [trip.load.equipment for trip in assignment.trips],
                "resequenced": [
                    trip.load.load_id for trip in assignment.trips if trip.resequenced
                ],
                "stops": [
                    {
                        "load_id": scheduled.load_id,
                        "store": scheduled.stop.store,
                        "zip": scheduled.stop.zip,
                        "city": scheduled.stop.city,
                        "state": scheduled.stop.state,
                        "arrive": round(scheduled.arrive, 3),
                        "depart": round(scheduled.depart, 3),
                        "wait_hours": round(scheduled.wait, 3) or 0.0,
                        "window_open": scheduled.stop.window_open,
                        "window_close": scheduled.stop.window_close,
                        "delivery_time": (
                            round(delivery_time(scheduled.stop), 3)
                            if scheduled.stop.has_window
                            else None
                        ),
                        "drop_and_hook": scheduled.stop.is_drop_and_hook,
                        "dwell_hours": round(scheduled.dwell, 3),
                    }
                    for scheduled in assignment.schedule
                ],
            }
            for assignment in plan.assignments
        ],
        "unschedulable": [
            {"loads": list(rejection.load_ids), "reason": rejection.reason}
            for rejection in plan.unschedulable
        ],
    }
