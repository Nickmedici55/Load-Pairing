"""A small read-only .xlsx reader built on the stdlib.

An .xlsx file is a zip of XML parts. Reading dispatch sheets needs very little
of the format: the sheet list, the shared-string table, and the cell values of
one worksheet. Doing it here keeps the tool free of a pandas/openpyxl install
on a dispatcher's machine.

Values come back as ``str``, ``float`` or ``None``. Cells that Excel stores as
a time or date (a serial number carrying a numeric format) are returned as
``float`` days -- ``parsing`` turns those into hours past midnight.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Built-in numeric formats that mean "this number is a date or a time".
DATETIME_BUILTINS = frozenset(range(14, 23)) | frozenset({27, 30, 36, 45, 46, 47, 50, 57})
_DATETIME_CODE = re.compile(r"[yYmMdDhHsS]")


class XlsxError(Exception):
    """Raised when a workbook cannot be read."""


@dataclass(frozen=True)
class Sheet:
    name: str
    rows: list[list[object]]


def _cell_ref_to_index(ref: str) -> int:
    """``'AB7'`` -> zero-based column 27."""
    col = 0
    for ch in ref:
        if not ch.isalpha():
            break
        col = col * 26 + (ord(ch.upper()) - 64)
    return col - 1


def _text(node) -> str:
    return "".join(node.itertext())


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    return [_text(si) for si in ET.fromstring(raw).findall(f"{MAIN_NS}si")]


def _datetime_style_ids(zf: zipfile.ZipFile) -> set[int]:
    """Style indices whose number format renders as a date or a time."""
    try:
        root = ET.fromstring(zf.read("xl/styles.xml"))
    except KeyError:
        return set()

    custom: dict[int, str] = {}
    for fmt in root.iter(f"{MAIN_NS}numFmt"):
        custom[int(fmt.get("numFmtId", "0"))] = fmt.get("formatCode", "")

    datetime_styles: set[int] = set()
    cell_xfs = root.find(f"{MAIN_NS}cellXfs")
    if cell_xfs is None:
        return datetime_styles
    for index, xf in enumerate(cell_xfs.findall(f"{MAIN_NS}xf")):
        fmt_id = int(xf.get("numFmtId", "0"))
        code = custom.get(fmt_id)
        if fmt_id in DATETIME_BUILTINS or (code and _DATETIME_CODE.search(_strip_literals(code))):
            datetime_styles.add(index)
    return datetime_styles


def _strip_literals(code: str) -> str:
    """Drop quoted literals and escapes so ``"Mar"`` is not read as a month."""
    return re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", code)


def _sheet_paths(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """``[(sheet name, part path)]`` in workbook tab order."""
    try:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    except KeyError as exc:  # pragma: no cover - not a spreadsheet at all
        raise XlsxError("xl/workbook.xml is missing; not an .xlsx file") from exc

    targets: dict[str, str] = {}
    try:
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        rels = None
    if rels is not None:
        for rel in rels.findall(f"{PKG_REL_NS}Relationship"):
            target = rel.get("Target", "")
            target = target[1:] if target.startswith("/") else f"xl/{target}"
            targets[rel.get("Id", "")] = target.replace("xl/xl/", "xl/")

    sheets = []
    for index, sheet in enumerate(workbook.iter(f"{MAIN_NS}sheet"), start=1):
        name = sheet.get("name", f"Sheet{index}")
        path = targets.get(sheet.get(f"{REL_NS}id", ""), f"xl/worksheets/sheet{index}.xml")
        sheets.append((name, path))
    if not sheets:
        raise XlsxError("workbook declares no sheets")
    return sheets


def sheet_names(path: str) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return [name for name, _ in _sheet_paths(zf)]


def read_sheet(path: str, tab: int | str = 1) -> Sheet:
    """Read one worksheet.

    ``tab`` is a 1-based tab number (the spec's "tab 3") or a sheet name.
    """
    with zipfile.ZipFile(path) as zf:
        sheets = _sheet_paths(zf)
        if isinstance(tab, str):
            matches = [s for s in sheets if s[0] == tab]
            if not matches:
                raise XlsxError(f"no sheet named {tab!r}; have {[s[0] for s in sheets]}")
            name, part = matches[0]
        else:
            if not 1 <= tab <= len(sheets):
                raise XlsxError(f"tab {tab} is out of range; workbook has {len(sheets)} tabs")
            name, part = sheets[tab - 1]

        strings = _shared_strings(zf)
        datetime_styles = _datetime_style_ids(zf)
        try:
            raw = zf.read(part)
        except KeyError as exc:
            raise XlsxError(f"worksheet part {part} is missing") from exc

    return Sheet(name=name, rows=_parse_rows(raw, strings, datetime_styles))


def _parse_rows(raw: bytes, strings: list[str], datetime_styles: set[int]) -> list[list[object]]:
    rows: list[list[object]] = []
    root = ET.fromstring(raw)
    for row in root.iter(f"{MAIN_NS}row"):
        cells: list[object] = []
        for cell in row.findall(f"{MAIN_NS}c"):
            ref = cell.get("r")
            index = _cell_ref_to_index(ref) if ref else len(cells)
            while len(cells) < index:
                cells.append(None)
            cells.append(_cell_value(cell, strings, datetime_styles))
        rows.append(cells)
    return rows


def _cell_value(cell, strings: list[str], datetime_styles: set[int]) -> object:
    kind = cell.get("t", "n")

    if kind == "inlineStr":
        node = cell.find(f"{MAIN_NS}is")
        return _text(node).strip() if node is not None else None

    value = cell.find(f"{MAIN_NS}v")
    if value is None or value.text is None:
        return None
    text = value.text

    if kind == "s":
        try:
            return strings[int(text)].strip()
        except (ValueError, IndexError):
            return None
    if kind in ("str", "e"):
        return text.strip()
    if kind == "b":
        return text.strip() not in ("0", "")

    try:
        number = float(text)
    except ValueError:
        return text.strip()

    style = cell.get("s")
    if style is not None and int(style) in datetime_styles:
        return SerialDateTime(number)
    return number


class SerialDateTime(float):
    """An Excel serial date/time. Still a float, but tagged for the parser."""

    __slots__ = ()

    @property
    def hours_past_midnight(self) -> float:
        return (float(self) % 1.0) * 24.0
