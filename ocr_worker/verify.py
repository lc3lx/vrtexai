"""Arithmetic verification of a reconstructed document.

OCR confidence is a poor guide on blurred digits: on a phone photo of a screen
the recognizer read a quantity as "13" at 37% and the correct "3" at 22%, so
picking the higher score picks the wrong digit. Arithmetic is a far stronger
signal — 3 x $12.00 = $36.00 and 13 x $12.00 does not — so where a row carries
its own cross-check, that check decides, and every repair is flagged.

Nothing here invents a value. A cell is only ever replaced by one of the
readings the recognizer already produced for those same pixels.
"""
from __future__ import annotations

import re
from typing import Any

# The column-name vocabulary used to live here, one flat pattern per role, and
# whichever column matched first took the role. It moved to
# :mod:`table_shape`, where a heading is one of three kinds of evidence and the
# candidates are weighed against each other — "Product ID" no longer takes the
# description from "Product Name" for containing the word "product".

SUBTOTAL_LABEL = re.compile(r"(?:sub\s*total|subtotal|المجموع\s*الفرعي|الإجمالي\s*قبل)", re.I)
TAX_LABEL = re.compile(r"(?:tax|vat|gst|ضريبة|القيمة\s*المضافة)", re.I)
TOTAL_LABEL = re.compile(r"(?:grand\s*total|total\s*due|amount\s*due|^total$|الإجمالي|المجموع\s*الكلي|المستحق)", re.I)
# Deductions that sit between the subtotal and the total.
ADJUSTMENT_LABEL = re.compile(
    r"(?:discount|advance|deposit|paid|credit|rebate|خصم|دفعة\s*مقدمة|مقدم|المدفوع)", re.I
)

NUMBER = re.compile(r"-?\d[\d,٬٫]*(?:\.\d+)?")


def to_number(text: str) -> float | None:
    """Parse a money/quantity cell. Returns None when it is not a number."""
    if text is None:
        return None
    cleaned = str(text).strip()
    if not cleaned:
        return None
    # Arabic-Indic digits appear on bilingual documents.
    cleaned = cleaned.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    match = NUMBER.search(cleaned.replace(" ", ""))
    if not match:
        return None
    body = match.group(0).replace(",", "").replace("٬", "").replace("٫", ".")
    try:
        return float(body)
    except ValueError:
        return None


def _close(left: float, right: float, tolerance: float = 0.02) -> bool:
    return abs(left - right) <= max(tolerance, abs(right) * 0.005)


def resolve_roles(columns: list[str], rows: list[list[dict[str, Any]]]) -> dict[str, int]:
    """Find a qty/price/total mapping the table's own evidence confirms.

    The decision itself lives in :func:`table_shape.assign_roles`, which weighs
    the heading, the content of the column and the arithmetic between columns
    against each other instead of taking the first heading that matches a
    pattern. Both readers — this geometric one and the vision-led one — go
    through it, so a column named badly on one path is named badly on neither.

    The return shape is unchanged: ``{role: column index}`` plus ``agreement``,
    which :func:`verify` uses to decide how loudly to complain.
    """
    from table_shape import assign_roles

    texts = [[str(cell.get("text") or "") for cell in row] for row in rows]
    found = assign_roles(list(columns or []), texts)
    resolved: dict[str, int] = dict(found.columns)
    if found.columns:
        resolved["agreement"] = found.agreement  # type: ignore[assignment]
    return resolved


def _candidates(cell: dict[str, Any]) -> list[tuple[str, float]]:
    """Every reading of this cell the recognizer produced, best-first."""
    out = [(cell["text"], float(cell.get("conf") or 0.0))]
    for alternative in cell.get("alternatives") or []:
        out.append((str(alternative.get("text") or ""), float(alternative.get("conf") or 0.0)))
    return out


def _repair_row(
    row: list[dict[str, Any]], roles: dict[str, int], flag: bool = True
) -> tuple[bool, str]:
    """Try to satisfy qty x price = total using readings already on the cells.

    `flag` off means an unreconciled row is reported in the summary only — used
    when the column roles themselves are in doubt.
    """
    qty_index = roles.get("qty")
    price_index = roles.get("unit_price")
    total_index = roles.get("line_total")
    if qty_index is None or price_index is None or total_index is None:
        return True, ""
    for index in (qty_index, price_index, total_index):
        if index >= len(row):
            return True, ""

    total = to_number(row[total_index]["text"])
    price = to_number(row[price_index]["text"])
    quantity = to_number(row[qty_index]["text"])
    if total is None or price is None or quantity is None:
        return True, ""
    if _close(quantity * price, total):
        return True, ""

    # The quantity is usually the shortest and therefore least reliable cell.
    for text, _conf in _candidates(row[qty_index])[1:]:
        candidate = to_number(text)
        if candidate is not None and _close(candidate * price, total):
            row[qty_index]["text"] = text
            row[qty_index]["review"] = True
            row[qty_index]["note"] = f"صُحّح من «{quantity:g}» ليطابق الإجمالي"
            row[qty_index]["conf"] = min(float(row[qty_index].get("conf") or 0.0), 70.0)
            return True, "qty-repaired"
    for text, _conf in _candidates(row[price_index])[1:]:
        candidate = to_number(text)
        if candidate is not None and _close(quantity * candidate, total):
            row[price_index]["text"] = text
            row[price_index]["review"] = True
            row[price_index]["note"] = "صُحّح ليطابق الإجمالي"
            return True, "price-repaired"

    # Nothing reconciles: say so rather than pick a number.
    if flag:
        for index in (qty_index, price_index, total_index):
            row[index]["review"] = True
        row[qty_index]["note"] = (
            f"الكمية × السعر ({quantity:g} × {price:g}) لا تساوي الإجمالي {total:g}"
        )
    return False, "row-mismatch"


def _line_amounts(rows: list[list[dict[str, Any]]], roles: dict[str, int]) -> list[float]:
    """Line-item amounts only, skipping totals rows that sit inside the table.

    A "Subtotal ... $350.00" line often shares the item table's columns and is
    grouped with it. Counting its amount as another line item made the sum come
    to twice the real total and produced a confident, wrong warning. A real line
    has a quantity or a unit price; a totals row has only an amount.
    """
    total_index = roles.get("line_total")
    if total_index is None:
        return []
    quantity_index, price_index = roles.get("qty"), roles.get("unit_price")
    amounts: list[float] = []
    for row in rows:
        if total_index >= len(row):
            continue
        amount = to_number(row[total_index]["text"])
        if amount is None:
            continue
        has_detail = any(
            index is not None and index < len(row) and to_number(row[index]["text"]) is not None
            for index in (quantity_index, price_index)
        )
        if has_detail or (quantity_index is None and price_index is None):
            amounts.append(amount)
    return amounts


def _find_labelled_amount(regions: list[dict[str, Any]], pattern: re.Pattern[str]) -> dict[str, Any] | None:
    """Locate a labelled amount such as Subtotal / Tax / Total anywhere on the page."""
    for region in regions:
        if region.get("kind") not in {"key_value", "table"}:
            continue
        for row in region.get("rows") or []:
            texts = [cell["text"].strip() for cell in row]
            for index, text in enumerate(texts):
                if not text or not pattern.search(text):
                    continue
                for candidate in row[index + 1:]:
                    if to_number(candidate["text"]) is not None:
                        return candidate
    return None


def verify(document: dict[str, Any]) -> list[str]:
    """Cross-check the document in place. Returns human-readable notes."""
    notes: list[str] = []
    regions = document.get("regions") or []

    line_totals: list[float] = []
    for region in regions:
        if region.get("kind") != "table":
            continue
        rows = region.get("rows") or []
        roles = resolve_roles(region.get("columns") or [], rows)
        if "line_total" not in roles:
            continue
        # When most rows disagree the column roles are probably wrong, not the
        # data. Say so once instead of painting the whole table yellow.
        quiet = float(roles.get("agreement") or 0.0) < 0.5 and len(rows) > 3
        repaired = mismatched = 0
        for row in rows:
            ok, outcome = _repair_row(row, roles, flag=not quiet)
            if outcome.endswith("repaired"):
                repaired += 1
            elif not ok:
                mismatched += 1
        if repaired:
            notes.append(f"تم تصحيح {repaired} صف/صفوف بناءً على الإجمالي")
        if mismatched and quiet:
            notes.append("تعذّر التحقق حسابياً من هذا الجدول؛ القيم كما قُرئت.")
        elif mismatched:
            notes.append(f"{mismatched} صف/صفوف لا تتوازن حسابياً — راجعها")
        line_totals.extend(_line_amounts(rows, roles))
        region["roles"] = roles

    subtotal_cell = _find_labelled_amount(regions, SUBTOTAL_LABEL)
    tax_cell = _find_labelled_amount(regions, TAX_LABEL)
    total_cell = _find_labelled_amount(regions, TOTAL_LABEL)

    subtotal = to_number(subtotal_cell["text"]) if subtotal_cell else None
    tax = to_number(tax_cell["text"]) if tax_cell else None
    total = to_number(total_cell["text"]) if total_cell else None

    if line_totals and subtotal is not None:
        summed = round(sum(line_totals), 2)
        if not _close(summed, subtotal, tolerance=0.05):
            subtotal_cell["review"] = True
            subtotal_cell["note"] = f"مجموع البنود {summed:g} لا يساوي المجموع الفرعي {subtotal:g}"
            notes.append(subtotal_cell["note"])
        else:
            notes.append("مجموع البنود يطابق المجموع الفرعي")

    if subtotal is not None and total is not None:
        adjustment_cell = _find_labelled_amount(regions, ADJUSTMENT_LABEL)
        adjustment = to_number(adjustment_cell["text"]) if adjustment_cell else None
        base = subtotal + (tax or 0.0)
        # An advance or discount may be printed with or without its minus sign,
        # so both readings count as reconciled. The goal is to confirm the
        # figures are consistent, not to impose one house style of invoice.
        candidates = [base]
        if adjustment:
            candidates += [base + adjustment, base - abs(adjustment)]
        if any(_close(value, total, tolerance=0.05) for value in candidates):
            notes.append("الإجمالي يطابق المجموع الفرعي زائد الضريبة")
        else:
            total_cell["review"] = True
            total_cell["note"] = f"المجموع الفرعي + الضريبة ({base:g}) لا يساوي الإجمالي {total:g}"
            notes.append(total_cell["note"])

    return notes
