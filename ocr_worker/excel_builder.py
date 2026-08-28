"""Professional Excel output for the AI-led path.

Two things separate this from :mod:`export`:

* **Real formulas.** A line total is written as ``=B5*C5`` and a subtotal as
  ``=SUM(D5:D12)``, so the sheet recalculates when the customer edits a
  quantity. The number the model actually read is not thrown away — it is
  attached to the cell as a comment, and the cell is highlighted when the two
  disagree. That way the formula is live and the evidence is still there.
* **Formatting.** Arial throughout, a coloured bold header band, thin borders
  on every table cell, currency and quantity number formats, and column widths
  measured from the content.

One sheet per page, one workbook per source file.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

from common import YELLOW
from export import _save_workbook
from templates import build_template

FONT_NAME = "Arial"
BAND = "1F4E79"       # header band
BAND_TEXT = "FFFFFF"
RULE = "B4C6E7"       # table grid
STRIPE = "F2F7FC"     # banded rows, a tint of the header band
LABEL_FILL = "DDEBF7"  # field labels
TITLE_TEXT = "1F4E79"

# Roles the builder gives special treatment. Anything else is a text column.
MONEY_ROLES = {"unit_price", "line_total", "discount", "tax"}
QTY_ROLES = {"qty"}

_HEADER_LABELS = {
    "supplier": "المورد",
    "client_name": "العميل",
    "invoice_number": "رقم الفاتورة",
    "invoice_date": "تاريخ الفاتورة",
    "due_date": "تاريخ الاستحقاق",
    "tax_number": "الرقم الضريبي",
    "payment_terms": "شروط الدفع",
}
_ROLE_LABELS = {
    "description": "الوصف",
    "sku": "رمز الصنف",
    "qty": "الكمية",
    "unit_price": "سعر الوحدة",
    "line_total": "الإجمالي",
    "discount": "الخصم",
    "tax": "الضريبة",
    "unit": "الوحدة",
    "date": "التاريخ",
}
_TOTAL_LABELS = {
    "subtotal": "المجموع الفرعي",
    "discount": "الخصم",
    "tax_amount": "الضريبة",
    "tax_rate": "نسبة الضريبة",
    "grand_total": "الإجمالي النهائي",
}
_TOTAL_ORDER = ("subtotal", "discount", "tax_amount", "grand_total")

_CURRENCY_SYMBOLS = {
    "SAR": "ر.س", "AED": "د.إ", "QAR": "ر.ق", "KWD": "د.ك", "EGP": "ج.م",
    "JOD": "د.أ", "USD": "$", "EUR": "€", "GBP": "£",
}


def money_format(currency: str) -> str:
    """An Excel number format carrying the document's own currency."""
    symbol = _CURRENCY_SYMBOLS.get((currency or "").strip().upper(), (currency or "").strip())
    # Quotes and semicolons would end the literal and corrupt the format string.
    symbol = re.sub(r'["\\;\[\]]', "", symbol)[:6]
    return f'#,##0.00" {symbol}"' if symbol else "#,##0.00"


def _column_letter(index: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(index)


def plan_columns(document: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Decide the item table's columns as ``(field, heading, role)``.

    The item objects carry the data, but the model also reports the headings it
    saw printed and a role for each. Those headings are used when they can be
    matched to a role, so the sheet reads like the original document instead of
    like the schema.
    """
    from ai_extract import ROLES

    headings: dict[str, str] = {}
    order: list[str] = []
    roles = document.get("column_roles") or []
    columns = document.get("columns") or []
    for index, role in enumerate(roles):
        if role in ROLES and role != "other" and role not in headings:
            heading = str(columns[index]).strip() if index < len(columns) else ""
            headings[role] = heading or _ROLE_LABELS.get(role, role)
            order.append(role)

    fields: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(field: str) -> None:
        key = field.casefold()
        if key in seen:
            return
        seen.add(key)
        role = key if key in ROLES else "other"
        heading = headings.get(role) or _ROLE_LABELS.get(role) or field
        fields.append((field, heading, role))

    # Model-declared order first, so the sheet mirrors the printed table.
    present: list[str] = []
    for item in document.get("items") or []:
        for key in item:
            if key in {"review", "notes"} or key in present:
                continue
            present.append(key)
    for role in order:
        for field in present:
            if field.casefold() == role:
                add(field)
    for field in present:
        add(field)
    return fields


ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")


def reading_order(text: Any) -> int:
    """Excel's per-cell reading order: 0 context, 1 left-to-right, 2 right-to-left.

    Set per cell rather than per sheet because these documents mix scripts
    inside one table — an Arabic description beside an English SKU. A sheet-wide
    setting pushes the punctuation of every foreign cell to the wrong end, so
    each cell is told which way its own text runs.
    """
    value = str(text or "")
    if not value.strip():
        return 0
    arabic = len(ARABIC.findall(value))
    latin = len(re.findall(r"[A-Za-z]", value))
    if arabic and arabic >= latin:
        return 2
    if latin:
        return 1
    return 0


def _text_alignment(text: Any, *, wrap: bool = False, vertical: str = "top"):
    """Align a text cell to the side its own script starts from."""
    from openpyxl.styles import Alignment

    order = reading_order(text)
    return Alignment(
        vertical=vertical,
        wrap_text=wrap,
        readingOrder=order,
        horizontal="right" if order == 2 else "left" if order == 1 else None,
    )


def _style_cell(cell, *, border, bold: bool = False, size: int = 11) -> None:
    from openpyxl.styles import Font

    cell.font = Font(name=FONT_NAME, size=size, bold=bold)
    cell.border = border


def _track(widths: dict[int, int], column: int, text: Any) -> None:
    length = len(str(text if text is not None else ""))
    widths[column] = max(widths.get(column, 10), min(length + 3, 60))


def _write_page(
    sheet,
    document: dict[str, Any],
    source: Path,
    destination: Path,
    styles: dict[str, Any],
) -> tuple[int, int, list[dict[str, Any]]]:
    """Render one page. Returns (records, flagged rows, review items)."""
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    thin = styles["thin"]
    yellow = styles["yellow"]
    band = styles["band"]
    label_fill = styles["label_fill"]
    stripe = styles.get("stripe")
    currency = money_format(str(document.get("currency") or ""))
    quantity_format = "#,##0.###"

    if str(document.get("direction") or "ltr") == "rtl":
        sheet.sheet_view.rightToLeft = True

    widths: dict[int, int] = {}
    review_items: list[dict[str, Any]] = []
    records = flagged = 0
    fields = plan_columns(document)
    width = max(len(fields), 2)
    row = 1

    def flag(column: int, header: str, value: Any, note: str) -> None:
        """Queue a cell for manual review.

        Only literal cells are queued. A formula cell must never enter the
        queue: ``apply_review_file`` writes the corrected value straight into
        the cell, which would silently replace the formula with a constant.
        """
        review_items.append({
            "output": str(destination),
            "sheet": sheet.title,
            "row": row,
            "column": column,
            "header": header,
            "value": "" if value is None else value,
            "confidence": "",
            "suggestion": note,
        })

    # ---- title -----------------------------------------------------------
    title = str(document.get("title") or "").strip() or source.stem
    cell = sheet.cell(row, 1, title)
    cell.font = Font(name=FONT_NAME, size=14, bold=True, color=TITLE_TEXT)
    cell.alignment = Alignment(vertical="center")
    if width > 1:
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
    sheet.row_dimensions[row].height = 22
    _track(widths, 1, title)
    row += 1

    subtitle = f"{source.name} — صفحة {document.get('page', 1)}"
    cell = sheet.cell(row, 1, subtitle)
    cell.font = Font(name=FONT_NAME, size=9, italic=True, color="7F7F7F")
    if width > 1:
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
    row += 2

    # ---- header fields ---------------------------------------------------
    header = document.get("header") or {}
    if header:
        for key, value in header.items():
            label = _HEADER_LABELS.get(str(key).casefold(), str(key))
            label_cell = sheet.cell(row, 1, label)
            _style_cell(label_cell, border=thin, bold=True)
            label_cell.fill = label_fill
            value_cell = sheet.cell(row, 2, value)
            _style_cell(value_cell, border=thin)
            value_cell.alignment = _text_alignment(value, wrap=len(str(value)) > 50)
            label_cell.alignment = _text_alignment(label)
            _track(widths, 1, label)
            _track(widths, 2, value)
            row += 1
        row += 1

    # ---- item table ------------------------------------------------------
    items = list(document.get("items") or [])
    role_columns: dict[str, int] = {}
    first_item_row = last_item_row = 0
    if items and fields:
        header_row = row
        for index, (field, heading, role) in enumerate(fields, start=1):
            cell = sheet.cell(row, index, heading)
            cell.font = Font(name=FONT_NAME, size=11, bold=True, color=BAND_TEXT)
            cell.fill = band
            cell.border = thin
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            _track(widths, index, heading)
            if role != "other" and role not in role_columns:
                role_columns[role] = index
        sheet.row_dimensions[row].height = 20
        row += 1
        first_item_row = row

        qty_column = role_columns.get("qty")
        price_column = role_columns.get("unit_price")
        total_column = role_columns.get("line_total")
        can_compute = bool(qty_column and price_column and total_column)

        for item in items:
            reviews = item.get("review") or {}
            notes = item.get("notes") or {}
            painted = False
            for index, (field, heading, role) in enumerate(fields, start=1):
                value = item.get(field)
                is_formula = False
                if can_compute and role == "line_total":
                    qty = item.get("qty")
                    price = item.get("unit_price")
                    if qty is not None and price is not None:
                        cell = sheet.cell(
                            row,
                            index,
                            f"={_column_letter(qty_column)}{row}*{_column_letter(price_column)}{row}",
                        )
                        is_formula = True
                        if value is not None:
                            cell.comment = Comment(
                                f"القيمة المقروءة من الصورة: {value:,.2f}", "Vertex"
                            )
                        _track(widths, index, f"{(value or qty * price):,.2f}")
                    else:
                        cell = sheet.cell(row, index, value)
                        _track(widths, index, value)
                else:
                    cell = sheet.cell(row, index, "" if value is None else value)
                    _track(widths, index, value)

                _style_cell(cell, border=thin)
                # Banded rows. On a wide grid the eye loses its line between the
                # description and the amount, and these tables are wide by
                # nature — this invoice carries nine columns.
                if stripe is not None and (row - first_item_row) % 2 == 1:
                    cell.fill = stripe
                if role in MONEY_ROLES:
                    cell.number_format = currency
                    cell.alignment = Alignment(horizontal="right")
                elif role in QTY_ROLES:
                    cell.number_format = quantity_format
                    cell.alignment = Alignment(horizontal="center")
                else:
                    cell.alignment = _text_alignment(value, wrap=len(str(value or "")) > 45)

                note = notes.get(field) or notes.get(role)
                if note and not is_formula:
                    cell.comment = Comment(str(note), "Vertex")
                if reviews.get(field) or reviews.get(role):
                    cell.fill = yellow
                    painted = True
                    if not is_formula:
                        flag(index, heading, value, str(note or ""))
                    elif qty_column:
                        # Redirect the fix to the quantity, which is a literal.
                        flag(qty_column, _ROLE_LABELS["qty"], item.get("qty"), str(note or ""))
            records += 1
            if painted:
                flagged += 1
            row += 1
        last_item_row = row - 1
        sheet.freeze_panes = sheet.cell(first_item_row, 1).coordinate
        # Sort and filter from the headings. A reviewer's first instinct on a
        # flagged invoice is to sort by amount or filter to one product line,
        # and without this they have to select the range by hand every time.
        sheet.auto_filter.ref = (
            f"{_column_letter(1)}{header_row}:{_column_letter(len(fields))}{last_item_row}"
        )
        row += 1

    # ---- totals ----------------------------------------------------------
    totals = document.get("totals") or {}
    totals_review = document.get("totals_review") or {}
    totals_notes = document.get("totals_notes") or {}
    if totals:
        total_column = role_columns.get("line_total")
        value_column = total_column or 2
        label_column = max(1, value_column - 1)
        written: dict[str, int] = {}
        for key in _TOTAL_ORDER:
            if key not in totals and not (key == "subtotal" and first_item_row):
                continue
            value = totals.get(key)
            label_cell = sheet.cell(row, label_column, _TOTAL_LABELS.get(key, key))
            is_grand = key == "grand_total"
            _style_cell(label_cell, border=thin, bold=True)
            label_cell.fill = label_fill
            label_cell.alignment = Alignment(horizontal="right")
            _track(widths, label_column, label_cell.value)

            formula = None
            if key == "subtotal" and total_column and first_item_row and last_item_row:
                letter = _column_letter(total_column)
                formula = f"=SUM({letter}{first_item_row}:{letter}{last_item_row})"
            elif key == "tax_amount" and "subtotal" in written and totals.get("tax_rate"):
                letter = _column_letter(value_column)
                formula = f"={letter}{written['subtotal']}*{float(totals['tax_rate'])}"
            elif is_grand and "subtotal" in written:
                letter = _column_letter(value_column)
                parts = [f"{letter}{written['subtotal']}"]
                if "tax_amount" in written:
                    parts.append(f"+{letter}{written['tax_amount']}")
                if "discount" in written:
                    parts.append(f"-ABS({letter}{written['discount']})")
                formula = "=" + "".join(parts)

            value_cell = sheet.cell(row, value_column, formula if formula else value)
            _style_cell(value_cell, border=thin, bold=True, size=12 if is_grand else 11)
            value_cell.number_format = currency
            value_cell.alignment = Alignment(horizontal="right")
            if formula is not None and value is not None:
                value_cell.comment = Comment(
                    f"القيمة المقروءة من الصورة: {value:,.2f}", "Vertex"
                )
            _track(widths, value_column, f"{value or 0:,.2f}")
            if is_grand:
                value_cell.border = Border(
                    left=thin.left, right=thin.right,
                    top=Side(style="thin", color=BAND),
                    bottom=Side(style="double", color=BAND),
                )
            note = totals_notes.get(key)
            if note and formula is None:
                value_cell.comment = Comment(str(note), "Vertex")
            if totals_review.get(key):
                value_cell.fill = yellow
                flagged += 1
                if formula is None:
                    flag(value_column, str(label_cell.value), value, str(note or ""))
            written[key] = row
            row += 1
        row += 1

    # ---- notes -----------------------------------------------------------
    notes_list = [str(note) for note in (document.get("notes") or []) if str(note).strip()]
    if notes_list:
        cell = sheet.cell(row, 1, "ملاحظات ونصوص أخرى في الصفحة")
        cell.font = Font(name=FONT_NAME, size=11, bold=True, color=BAND_TEXT)
        cell.fill = band
        cell.border = thin
        if width > 1:
            sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
        row += 1
        for note in notes_list:
            cell = sheet.cell(row, 1, note)
            _style_cell(cell, border=thin)
            cell.alignment = _text_alignment(note, wrap=True)
            if width > 1:
                sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
            _track(widths, 1, note[:60])
            row += 1

    if row == 1:
        sheet.cell(1, 1, "لم يتم استخراج أي بيانات من هذه الصفحة.")
    for column, size in widths.items():
        sheet.column_dimensions[_column_letter(column)].width = max(10, size)

    # Printable as it stands. These sheets get printed and passed around an
    # accounts office, and a nine-column invoice spilling onto a second sheet
    # with no headings on it is useless on paper.
    sheet.page_setup.orientation = "landscape" if width > 6 else "portrait"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.print_options.horizontalCentered = True
    if first_item_row:
        sheet.print_title_rows = f"{first_item_row - 1}:{first_item_row - 1}"
    return records, flagged, review_items


def _sheet_title(source: Path, page: int, total: int, taken: set[str]) -> str:
    base = re.sub(r"[\\/*?:\[\]]", "-", source.stem)[:26] or "Extracted"
    title = base if total <= 1 else f"{base[:24]}-{page}"
    candidate, suffix = title[:31], 2
    while candidate in taken:
        candidate = f"{title[:28]}_{suffix}"[:31]
        suffix += 1
    taken.add(candidate)
    return candidate


def _write_summary(book, styles, source: Path, documents, records: int,
                   review_items: list[dict[str, Any]]) -> None:
    """A front sheet saying what was read and what still needs a human.

    Put first, and deliberately: the reviewer's question is never "what does row
    41 say" but "is any of this wrong". A workbook that opens on a wall of
    figures makes them hunt for the yellow cells; one that opens on a list of
    them, with the reason beside each, turns the check into a short read.
    """
    from openpyxl.styles import Alignment, Font, PatternFill

    sheet = book.create_sheet(title="المراجعة", index=0)
    thin, band, label_fill = styles["thin"], styles["band"], styles["label_fill"]
    if any(str(d.get("direction") or "") == "rtl" for d in documents):
        sheet.sheet_view.rightToLeft = True
    sheet.sheet_view.showGridLines = False

    def band_row(row: int, text: str, span: int = 4) -> int:
        cell = sheet.cell(row, 1, text)
        cell.font = Font(name=FONT_NAME, size=11, bold=True, color=BAND_TEXT)
        cell.fill = band
        cell.border = thin
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=span)
        sheet.row_dimensions[row].height = 20
        return row + 1

    def pair(row: int, label: str, value: Any) -> int:
        left = sheet.cell(row, 1, label)
        left.font = Font(name=FONT_NAME, size=11, bold=True)
        left.fill = label_fill
        left.border = thin
        left.alignment = _text_alignment(label)
        right = sheet.cell(row, 2, value)
        _style_cell(right, border=thin)
        right.alignment = _text_alignment(value)
        sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        return row + 1

    title = sheet.cell(1, 1, "ملخّص المراجعة")
    title.font = Font(name=FONT_NAME, size=16, bold=True, color=TITLE_TEXT)
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=4)
    row = 3

    row = band_row(row, "المصدر")
    row = pair(row, "الملف", source.name)
    row = pair(row, "الصفحات", len(documents))
    row = pair(row, "البنود المستخرجة", records)
    row += 1

    row = band_row(row, "ما جرى التحقق منه")
    # Named so the reviewer knows what the absence of a flag actually means.
    row = pair(row, "الشكل", "الأرقام أرقام والأدوار من مجموعة معروفة")
    row = pair(row, "الحساب", "الكمية × السعر = الإجمالي، ومجموع البنود = المجموع الفرعي")
    row = pair(row, "البكسلات", "كل رقم له ما يقابله في قراءة مستقلة للصورة")
    row += 1

    row = band_row(row, "يحتاج مراجعة")
    if not review_items:
        cell = sheet.cell(row, 1, "لا شيء. كل قيمة لها دليل في الصورة وحسابها صحيح.")
        _style_cell(cell, border=thin)
        cell.alignment = _text_alignment(cell.value)
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        row += 1
    else:
        for index, heading in enumerate(("الورقة", "الخلية", "القيمة", "السبب"), start=1):
            cell = sheet.cell(row, index, heading)
            cell.font = Font(name=FONT_NAME, size=11, bold=True, color=BAND_TEXT)
            cell.fill = band
            cell.border = thin
            cell.alignment = Alignment(horizontal="center")
        row += 1
        for item in review_items:
            where = f"{_column_letter(int(item.get('column') or 1))}{item.get('row') or ''}"
            for index, value in enumerate(
                # "suggestion" is the key `flag` writes; it carries the reason
                # the gate objected, which is the only column a reviewer reads.
                (item.get("sheet") or "", where, item.get("value"),
                 item.get("suggestion") or "تحتاج تأكيداً"), start=1
            ):
                cell = sheet.cell(row, index, value)
                _style_cell(cell, border=thin)
                cell.alignment = _text_alignment(value, wrap=index == 4)
            row += 1

    for column, width in ((1, 26), (2, 34), (3, 20), (4, 52)):
        sheet.column_dimensions[_column_letter(column)].width = width
    sheet.page_setup.fitToWidth = 1
    sheet.sheet_properties.pageSetUpPr.fitToPage = True


def write_ai_workbook(
    destination: Path,
    source: Path,
    documents: Sequence[dict[str, Any]],
) -> tuple[int, int, list[dict[str, Any]], dict[str, Any], Path]:
    """Build the workbook. Same 5-tuple as the writers in :mod:`export`."""
    from openpyxl import Workbook
    from openpyxl.styles import Border, PatternFill, Side

    book = Workbook()
    book.remove(book.active)
    side = Side(style="thin", color=RULE)
    styles = {
        "thin": Border(left=side, right=side, top=side, bottom=side),
        "yellow": PatternFill(fill_type="solid", fgColor=YELLOW),
        "band": PatternFill(fill_type="solid", fgColor=BAND),
        "label_fill": PatternFill(fill_type="solid", fgColor=LABEL_FILL),
        "stripe": PatternFill(fill_type="solid", fgColor=STRIPE),
    }

    records = low = 0
    review_items: list[dict[str, Any]] = []
    taken: set[str] = set()
    first_headings: list[str] = []

    for index, document in enumerate(documents, start=1):
        sheet = book.create_sheet(title=_sheet_title(source, index, len(documents), taken))
        page_records, page_low, page_review = _write_page(
            sheet, document, source, destination, styles
        )
        records += page_records
        low += page_low
        review_items.extend(page_review)
        if not first_headings:
            first_headings = [heading for _field, heading, _role in plan_columns(document)]

    if not book.worksheets:
        sheet = book.create_sheet(title="Extracted")
        sheet.cell(1, 1, "لم يتم استخراج أي بيانات من هذا الملف.")

    _write_summary(book, styles, source, list(documents), records, review_items)
    book.active = 0

    destination = _save_workbook(book, destination)
    for item in review_items:
        item["output"] = str(destination)
    kind = str((documents[0] if documents else {}).get("document_type") or "document")
    template = build_template(
        source.name,
        kind if kind in {"invoice", "table"} else "document",
        json.dumps((documents[0] if documents else {}).get("header") or {}, ensure_ascii=False),
        first_headings,
    )
    return records, low, review_items, template, destination
