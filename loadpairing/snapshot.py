"""Everything needed to rebuild a plan, as JSON.

A plan is not a document the tool owns: it is what falls out of a sheet, the
lane cache and the dwell overrides. To let a dispatcher rearrange one -- and to
save one under a name -- the loads have to survive the upload that produced
them, because the spreadsheet does not.

So a snapshot carries the parsed loads, the settings they were planned under,
and the grouping of loads onto drivers. Miles and dwell are deliberately left
out: those are read fresh from the database when the plan is rebuilt, so a
saved plan reflects the dwell the dispatcher has set since.

The JSON round-trips through a hidden form field as well as through the
database, which means it comes back from a browser. Nothing here trusts it:
every field is checked and a bad one raises :class:`SnapshotError` rather than
reaching the scheduler.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .models import Load, Stop


class SnapshotError(Exception):
    """Raised when a snapshot is missing or malformed."""


def dump_loads(loads: Iterable[Load]) -> str:
    return json.dumps([_load_as_dict(load) for load in loads], separators=(",", ":"))


def read_loads(text: str) -> tuple[Load, ...]:
    return tuple(_load_from_dict(item) for item in _read_list(text, "loads"))


def dump_groups(groups: Iterable[Iterable[str]]) -> str:
    return json.dumps([list(group) for group in groups], separators=(",", ":"))


def read_groups(text: str) -> tuple[tuple[str, ...], ...]:
    groups = []
    for item in _read_list(text, "groups"):
        if not isinstance(item, list):
            raise SnapshotError("each driver in a grouping must be a list of load IDs")
        groups.append(tuple(_text(load_id, "load ID") for load_id in item))
    return tuple(groups)


def _read_list(text: str, what: str) -> list:
    if not text:
        raise SnapshotError(f"no {what} in this submission")
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise SnapshotError(f"the {what} in this submission are not readable: {exc}") from exc
    if not isinstance(parsed, list):
        raise SnapshotError(f"the {what} in this submission are not a list")
    return parsed


def _load_as_dict(load: Load) -> dict:
    return {
        "load_id": load.load_id,
        "carrier_id": load.carrier_id,
        "equipment": load.equipment,
        "stops": [
            {
                "order": stop.order,
                "store": stop.store,
                "zip": stop.zip,
                "city": stop.city,
                "state": stop.state,
                "pallets": stop.pallets,
                "window_open": stop.window_open,
                "window_close": stop.window_close,
            }
            for stop in load.stops
        ],
    }


def _load_from_dict(item: Any) -> Load:
    if not isinstance(item, dict):
        raise SnapshotError("a load in this submission is not an object")
    stops = item.get("stops")
    if not isinstance(stops, list) or not stops:
        raise SnapshotError(
            f"load {item.get('load_id', '?')} in this submission has no delivery stops"
        )
    return Load(
        load_id=_text(item.get("load_id"), "load ID"),
        carrier_id=_text(item.get("carrier_id", ""), "carrier ID", required=False),
        equipment=_text(item.get("equipment", ""), "equipment", required=False),
        stops=tuple(_stop_from_dict(stop) for stop in stops),
    )


def _stop_from_dict(item: Any) -> Stop:
    if not isinstance(item, dict):
        raise SnapshotError("a stop in this submission is not an object")
    return Stop(
        order=_text(item.get("order", ""), "order", required=False),
        store=_text(item.get("store", ""), "store", required=False),
        zip=_text(item.get("zip"), "ZIP"),
        city=_text(item.get("city", ""), "city", required=False),
        state=_text(item.get("state", ""), "state", required=False),
        pallets=_number(item.get("pallets", 0.0), "pallets") or 0.0,
        window_open=_number(item.get("window_open"), "window open"),
        window_close=_number(item.get("window_close"), "window close"),
    )


def _text(value: Any, what: str, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, (str, int, float)):
        raise SnapshotError(f"{what} in this submission is not a value")
    text = str(value).strip()
    if required and not text:
        raise SnapshotError(f"a {what} is missing from this submission")
    return text


def _number(value: Any, what: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SnapshotError(f"{what} in this submission is not a number")
    try:
        return float(value)
    except ValueError as exc:
        raise SnapshotError(f"{what} in this submission is not a number") from exc
