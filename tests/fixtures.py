"""Build dispatch workbooks for the tests.

Real dispatch sheets cannot be committed, so the fixtures reproduce the layout
the parser depends on: a title block, a header at row 6 that repeats before
every load block, stop rows with a blank Carrier ID, numeric ZIPs, and windows
stored as Excel time serials.
"""

from __future__ import annotations

import zipfile
from xml.sax.saxutils import escape

HEADER = [
    "Carrier ID",
    "Load ID",
    "Trailer Equipment Type",
    "Order",
    "Store",
    "Window Open",
    "Window Close",
    "City",
    "State",
    "Zip",
    "Total Pallets Shipped",
]

TIME_STYLE = 1          # cellXfs index whose number format is a time

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheets}
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="hh:mm"/></numFmts>
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
</cellXfs>
</styleSheet>"""


class Time(float):
    """A cell Excel would store as a time serial."""

    __slots__ = ()


def hours(value: float) -> Time:
    """``5.25`` hours -> the serial Excel writes for 05:15."""
    return Time(value / 24.0)


def column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def write_workbook(path: str, sheets: dict[str, list[list]]) -> str:
    """Write a workbook. ``sheets`` maps a tab name to its rows of cells."""
    strings: list[str] = []
    string_index: dict[str, int] = {}

    def shared(text: str) -> int:
        if text not in string_index:
            string_index[text] = len(strings)
            strings.append(text)
        return string_index[text]

    sheet_xml: list[str] = []
    for rows in sheets.values():
        body = []
        for row_number, row in enumerate(rows, start=1):
            cells = []
            for column, value in enumerate(row):
                if value is None or value == "":
                    continue
                ref = f"{column_name(column)}{row_number}"
                if isinstance(value, Time):
                    cells.append(f'<c r="{ref}" s="{TIME_STYLE}"><v>{float(value)!r}</v></c>')
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    cells.append(f'<c r="{ref}" t="s"><v>{shared(str(value))}</v></c>')
            body.append(f'<row r="{row_number}">{"".join(cells)}</row>')
        sheet_xml.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(body)}</sheetData></worksheet>'
        )

    names = list(sheets)
    workbook_sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(names, start=1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{workbook_sheets}</sheets></workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{i}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(names) + 1)
        )
        + f'<Relationship Id="rId{len(names) + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
        f'<Relationship Id="rId{len(names) + 2}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/></Relationships>'
    )
    shared_strings = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(strings)}" '
        f'uniqueCount="{len(strings)}">'
        + "".join(f"<si><t>{escape(text)}</t></si>" for text in strings)
        + "</sst>"
    )
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(names) + 1)
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES.format(sheets=overrides))
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/sharedStrings.xml", shared_strings)
        zf.writestr("xl/styles.xml", STYLES)
        for i, xml in enumerate(sheet_xml, start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", xml)
    return path


def title_block() -> list[list]:
    """Rows 0-5: the title block that sits above the real header."""
    return [
        ["New England SC - Chicopee MA"],
        ["Dispatch Order"],
        [],
        ["Generated 08/28"],
        [],
        [],
    ]


def load_block(
    carrier: str,
    load_id: str,
    equipment: str,
    stops: list[dict],
    repeat_header: bool = True,
) -> list[list]:
    """One load: an optional repeated header, then a row per stop."""
    rows: list[list] = [list(HEADER)] if repeat_header else []
    for index, stop in enumerate(stops):
        rows.append(
            [
                carrier if index == 0 else "",
                load_id if index == 0 else "",
                equipment if index == 0 else "",
                stop.get("order", str(index + 1)),
                stop["store"],
                stop.get("open"),
                stop.get("close"),
                stop.get("city", ""),
                stop.get("state", ""),
                stop["zip"],
                stop.get("pallets", 24),
            ]
        )
    return rows


def dispatch_sheet(loads: list[dict]) -> list[list]:
    """A whole tab: title block, header, then one block per load."""
    rows = title_block()
    rows.append(list(HEADER))
    for index, load in enumerate(loads):
        rows.extend(
            load_block(
                load.get("carrier", "PTAG"),
                load["load_id"],
                load.get("equipment", "53LG"),
                load["stops"],
                repeat_header=index > 0,
            )
        )
    return rows
