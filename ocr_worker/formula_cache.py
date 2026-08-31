"""Give every formula the value it works out to, so the file reads anywhere.

A spreadsheet cell holding a formula stores two things: the formula, and the
result the last program to open it calculated. openpyxl writes only the first.
Excel fills in the second when it opens the file — *unless* it will not
calculate, and it will not calculate in Protected View, which is the mode every
downloaded file opens in. So the customer opened their workbook and found the
Amount column empty, the Subtotal empty and the Total empty, while the figures
beside them showed fine. Every blank cell was a formula; every visible one was a
plain number.

The same blank appears in Google Sheets' preview, in a phone's file viewer, in a
mail client's attachment preview, and in any tool that reads the stored value
rather than recalculating — which is most of them.

So the result is written next to the formula. The formula stays live: edit a
quantity in Excel and the total still follows. But nothing has to recalculate
for the number to be *seen*, which is the difference between a workbook the
customer trusts and one that looks empty.

openpyxl has no API for this — a cell holds a formula or a value, never both —
so the values are injected into the saved file's XML. The file is a zip of XML
written by openpyxl moments earlier, so its shape is known rather than guessed
at, and :func:`cache_formula_values` leaves any cell it does not recognise
exactly as it found it.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

# <sheet name="Review" sheetId="1" r:id="rId1"/> in xl/workbook.xml
_SHEET = re.compile(rb'<sheet\b[^>]*\bname="([^"]*)"[^>]*\br:id="([^"]*)"', re.I)
# One relationship tag; its attributes are read out separately because writers
# order them differently — openpyxl puts Target before Id.
_RELATION = re.compile(rb'<Relationship\b[^>]*?/?>', re.I)
_ATTRIBUTE = re.compile(rb'\b(Id|Target)="([^"]*)"', re.I)
# A cell holding a formula whose result has not been worked out. openpyxl leaves
# an empty ``<v/>`` behind the formula, and that empty element is the blank the
# customer sees. A cell that already carries a real result does not match this,
# and is left exactly as it was found.
_FORMULA_CELL = re.compile(
    rb'(<c\b[^>]*\br="([A-Z]+\d+)"[^>]*>)'      # the cell, and its address
    rb'(\s*<f\b[^>]*>.*?</f>)'                  # the formula
    rb'(?:\s*<v\s*/>|\s*<v>\s*</v>)?\s*'        # an empty result, if one is there
    rb'(</c>)',
    re.S,
)


def _number(value: Any) -> bytes | None:
    """The result, formatted the way a spreadsheet stores one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / infinity
        return None
    return f"{value:.10g}".encode("ascii")


def _sheet_files(archive: zipfile.ZipFile) -> dict[str, str]:
    """Which XML file holds which sheet, by the sheet's own name."""
    try:
        workbook = archive.read("xl/workbook.xml")
        relations = archive.read("xl/_rels/workbook.xml.rels")
    except KeyError:
        return {}
    targets: dict[str, str] = {}
    for tag in _RELATION.findall(relations):
        attributes = {
            name.decode().casefold(): value.decode()
            for name, value in _ATTRIBUTE.findall(tag)
        }
        if attributes.get("id") and attributes.get("target"):
            targets[attributes["id"]] = attributes["target"]
    found: dict[str, str] = {}
    for name, identifier in _SHEET.findall(workbook):
        target = targets.get(identifier.decode())
        if not target:
            continue
        # Targets are relative to xl/, and may be written either way round.
        path = target.lstrip("/")
        found[_unescape(name.decode())] = path if path.startswith("xl/") else f"xl/{path}"
    return found


def _unescape(name: str) -> str:
    for entity, character in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                              ("&quot;", '"'), ("&apos;", "'")):
        name = name.replace(entity, character)
    return name


def _fill(sheet_xml: bytes, values: dict[str, Any]) -> bytes:
    def replace(match: re.Match) -> bytes:
        coordinate = match.group(2).decode()
        number = _number(values.get(coordinate))
        if number is None:
            return match.group(0)
        return match.group(1) + match.group(3) + b"<v>" + number + b"</v>" + match.group(4)

    return _FORMULA_CELL.sub(replace, sheet_xml)


def cache_formula_values(path: Path, values: dict[str, dict[str, Any]]) -> int:
    """Store each formula's result in the saved workbook. Returns how many.

    Never raises on a file it cannot rewrite: a workbook whose formulas show
    only once Excel recalculates is worse than one that does not, but it is far
    better than no workbook at all.
    """
    if not values or not any(values.values()):
        return 0
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as archive:
            files = _sheet_files(archive)
            entries = [(item, archive.read(item.filename)) for item in archive.infolist()]
    except (OSError, KeyError, zipfile.BadZipFile):
        return 0

    wanted = {
        files[title]: cells
        for title, cells in values.items()
        if cells and title in files
    }
    if not wanted:
        return 0

    filled = 0
    rewritten: list[tuple[zipfile.ZipInfo, bytes]] = []
    for item, data in entries:
        cells = wanted.get(item.filename)
        if cells:
            updated = _fill(data, cells)
            filled += updated.count(b"</f><v>") - data.count(b"</f><v>")
            data = updated
        rewritten.append((item, data))

    # Written beside the original and moved over it, so a failure half way
    # through leaves the workbook that already saved successfully intact.
    handle, temporary = tempfile.mkstemp(suffix=".xlsx", dir=str(path.parent))
    os.close(handle)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for item, data in rewritten:
                archive.writestr(item, data)
        shutil.move(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        return 0
    return filled
