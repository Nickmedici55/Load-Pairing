"""Turn a dispatch spreadsheet into :class:`Load` objects.

The sheet layout is nonstandard and this parser depends on its quirks:

* Rows 0-5 are a title block; the real header is at row 6.
* The header row repeats before every load block, so any row whose
  ``Carrier ID`` cell reads ``"Carrier ID"`` is skipped.
* A load block starts on the row where ``Carrier ID`` is populated. Rows below
  it with a blank ``Carrier ID`` but a populated ``Store`` are further stops on
  that same load.
* ZIPs come back from the sheet as numbers, so they are cast and zero-padded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Load, Stop
from .xlsx import SerialDateTime, Sheet, read_sheet

DEFAULT_HEADER_ROW = 6

#: The carrier these sheets are read for. Passing an empty carrier reads every
#: carrier on the sheet instead.
DEFAULT_CARRIER_ID = "PTAG"

CARRIER_ID = "Carrier ID"
LOAD_ID = "Load ID"
EQUIPMENT = "Trailer Equipment Type"
ORDER = "Order"
STORE = "Store"
WINDOW_OPEN = "Window Open"
WINDOW_CLOSE = "Window Close"
CITY = "City"
STATE = "State"
ZIP = "Zip"
PALLETS = "Total Pallets Shipped"

REQUIRED_COLUMNS = (CARRIER_ID, LOAD_ID, STORE, ZIP)

_TIME = re.compile(r"^\s*(\d{1,2})\s*[:.]\s*(\d{2})(?:\s*[:.]\s*(\d{2}))?\s*([AaPp])?\.?[Mm]?\.?\s*$")


class ParseError(Exception):
    """Raised when the sheet does not look like a dispatch sheet."""


@dataclass(frozen=True)
class ParsedSheet:
    sheet_name: str
    loads: tuple[Load, ...]
    skipped_rows: int

    @property
    def stop_count(self) -> int:
        return sum(len(load.stops) for load in self.loads)


def parse_workbook(
    path: str,
    tab: int | str = 3,
    carrier_id: str | None = None,
    header_row: int = DEFAULT_HEADER_ROW,
) -> ParsedSheet:
    """Read one tab of a dispatch workbook.

    Tabs 2 and 3 (``Reverse Order_2``, ``Dispatch Order_3``) hold the same
    loads in opposite stop sequence; tab 3 is the delivery order and is the
    default here.
    """
    return parse_sheet(read_sheet(path, tab), carrier_id=carrier_id, header_row=header_row)


def parse_sheet(sheet: Sheet, carrier_id: str | None = None, header_row: int = DEFAULT_HEADER_ROW) -> ParsedSheet:
    columns = _locate_columns(sheet.rows, header_row)
    loads: list[Load] = []
    skipped = 0

    current_header: dict[str, str] | None = None
    current_stops: list[Stop] = []

    def flush() -> None:
        nonlocal current_header, current_stops
        if current_header and current_stops:
            if carrier_id is None or current_header["carrier_id"] == carrier_id:
                loads.append(
                    Load(
                        load_id=current_header["load_id"],
                        carrier_id=current_header["carrier_id"],
                        equipment=current_header["equipment"],
                        stops=tuple(current_stops),
                    )
                )
        current_header = None
        current_stops = []

    for row in sheet.rows[header_row + 1:]:
        carrier = _string(_cell(row, columns.get(CARRIER_ID)))
        store = _string(_cell(row, columns.get(STORE)))

        if carrier == CARRIER_ID:          # the header, repeated before a block
            continue
        if not carrier and not store:      # blank spacer row
            continue

        if carrier:
            flush()
            current_header = {
                "carrier_id": carrier,
                "load_id": _string(_cell(row, columns.get(LOAD_ID))),
                "equipment": _string(_cell(row, columns.get(EQUIPMENT))),
            }
        elif current_header is None:
            skipped += 1               # a stop with no load block above it
            continue

        stop = _read_stop(row, columns)
        if stop is None:
            skipped += 1
            continue
        current_stops.append(stop)

    flush()
    return ParsedSheet(sheet_name=sheet.name, loads=tuple(loads), skipped_rows=skipped)


def _locate_columns(rows: list[list[object]], header_row: int) -> dict[str, int]:
    if header_row >= len(rows):
        raise ParseError(f"sheet has {len(rows)} rows; no header at row {header_row}")
    header = [_string(cell) for cell in rows[header_row]]
    columns = {name: index for index, name in enumerate(header) if name}

    missing = [name for name in REQUIRED_COLUMNS if name not in columns]
    if missing:
        raise ParseError(
            f"header row {header_row} is missing {', '.join(missing)}; found {header}"
        )
    return columns


def _read_stop(row: list[object], columns: dict[str, int]) -> Stop | None:
    zip_code = normalize_zip(_cell(row, columns.get(ZIP)))
    store = _string(_cell(row, columns.get(STORE)))
    if not zip_code or not store:
        return None
    return Stop(
        order=_string(_cell(row, columns.get(ORDER))),
        store=store,
        zip=zip_code,
        city=_string(_cell(row, columns.get(CITY))),
        state=_string(_cell(row, columns.get(STATE))),
        pallets=_number(_cell(row, columns.get(PALLETS))),
        window_open=parse_time(_cell(row, columns.get(WINDOW_OPEN))),
        window_close=parse_time(_cell(row, columns.get(WINDOW_CLOSE))),
    )


def _cell(row: list[object], index: int | None) -> object:
    if index is None or index >= len(row):
        return None
    return row[index]


def _string(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _number(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return 0.0


def normalize_zip(value: object) -> str:
    """ZIPs read back as floats. Cast, drop any +4, and zero-pad to five."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        text = str(int(value))
    else:
        text = str(value).strip()
        if not text:
            return ""
        text = text.split("-", 1)[0].strip()
        if text.endswith(".0"):
            text = text[:-2]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return ""
    return digits[:5].zfill(5) if len(digits) <= 5 else digits[:5]


def parse_time(value: object) -> float | None:
    """Read a delivery window bound as hours past midnight.

    Accepts an Excel time serial, a plain fraction of a day, ``"05:15"``,
    ``"5:15 PM"`` and ``"0515"``. Returns ``None`` for an empty cell.
    """
    if value is None:
        return None
    if isinstance(value, SerialDateTime):
        return round(value.hours_past_midnight, 6)
    if isinstance(value, (int, float)):
        number = float(value)
        if 0.0 <= number < 1.0:            # a bare fraction of a day
            return round(number * 24.0, 6)
        if 0.0 <= number <= 24.0:          # already hours
            return round(number, 6)
        if number >= 1.0:                  # a date+time serial
            return round((number % 1.0) * 24.0, 6)
        return None

    text = str(value).strip()
    if not text:
        return None

    match = _TIME.match(text)
    if match:
        hour, minute, second, meridiem = match.groups()
        hours = int(hour) + int(minute) / 60.0 + (int(second) / 3600.0 if second else 0.0)
        if meridiem:
            hour_of_day = int(hour) % 12
            hours = hour_of_day + int(minute) / 60.0 + (int(second) / 3600.0 if second else 0.0)
            if meridiem.lower() == "p":
                hours += 12.0
        return round(hours, 6) if 0.0 <= hours <= 24.0 else None

    if text.isdigit() and len(text) in (3, 4):     # 515 / 0515
        hours = int(text[:-2]) + int(text[-2:]) / 60.0
        return round(hours, 6) if 0.0 <= hours <= 24.0 else None

    try:
        return parse_time(float(text))
    except ValueError:
        return None
