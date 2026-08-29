"""Tests for the formatted, formula-bearing workbook.

The important one is :class:`FormulaIntegrityTests`. openpyxl writes formulas
without evaluating them, so a broken reference is invisible until the customer
opens the file and Excel shows ``#REF!`` or ``#VALUE!``. Rather than install an
office suite to recalculate, every formula is parsed here and each cell it
points at is checked for existence and for holding a number — which is exactly
the condition under which those two errors appear.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import excel_builder

CELL_REFERENCE = re.compile(r"(?<![A-Z0-9_!:])(\$?[A-Z]{1,3}\$?\d{1,7})(?![(\d])")
RANGE_REFERENCE = re.compile(r"(\$?[A-Z]{1,3}\$?\d{1,7}):(\$?[A-Z]{1,3}\$?\d{1,7})")
ERROR_LITERALS = ("#REF!", "#VALUE!", "#NAME?", "#DIV/0!", "#NULL!", "#NUM!")


def document(**overrides) -> dict:
    base = {
        "document_type": "invoice",
        "direction": "rtl",
        "currency": "SAR",
        "title": "فاتورة ضريبية",
        "page": 1,
        "header": {"supplier": "شركة الأفق", "invoice_number": "INV-2201"},
        "columns": ["الوصف", "الكمية", "سعر الوحدة", "الإجمالي"],
        "column_roles": ["description", "qty", "unit_price", "line_total"],
        "items": [
            {"description": "قلم", "qty": 2, "unit_price": 15.5, "line_total": 31.0,
             "review": {}, "notes": {}},
            {"description": "دفتر", "qty": 3, "unit_price": 10.0, "line_total": 30.0,
             "review": {}, "notes": {}},
            {"description": "ممحاة", "qty": 10, "unit_price": 1.25, "line_total": 12.5,
             "review": {}, "notes": {}},
        ],
        "totals": {"subtotal": 73.5, "tax_rate": 0.15, "tax_amount": 11.03, "grand_total": 84.53},
        "totals_review": {},
        "totals_notes": {},
        "notes": ["الدفع خلال 30 يوماً"],
    }
    base.update(overrides)
    return base


class WorkbookCase(unittest.TestCase):
    """Builds a workbook once per test and reopens it the way Excel would."""

    def build(self, *documents):
        from openpyxl import load_workbook

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "invoice.png"
        destination = Path(directory.name) / "invoice_cleaned.xlsx"
        result = excel_builder.write_ai_workbook(
            destination, source, list(documents) or [document()]
        )
        self.result = result
        self.destination = result[4]
        self.book = load_workbook(self.destination)
        self.addCleanup(self.book.close)
        # The workbook opens on a review summary, so the page sheets start at
        # index 1. Found by the name the builder gives it in this document's own
        # language — an English document's front sheet is called "Review" — and
        # asserted to be first, so a future sheet added at the front does not
        # silently point every assertion at it.
        expected = excel_builder.words_for(
            str((list(documents) or [document()])[0].get("direction") or "ltr")
        )("review")
        self.summary = self.book.worksheets[0]
        self.assertEqual(self.summary.title, expected)
        self.pages = self.book.worksheets[1:]
        self.sheet = self.pages[0]
        return self.sheet

    def find_row(self, text: str) -> int:
        for row in self.sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.strip() == text:
                    return cell.row
        raise AssertionError(f"لم يُعثر على الصف «{text}»")

    def formula_cells(self):
        for row in self.sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    yield cell


class FormulaTests(WorkbookCase):
    def test_line_total_is_a_product_formula_not_a_number(self):
        sheet = self.build()
        header_row = self.find_row("الوصف")
        first = header_row + 1
        self.assertEqual(sheet.cell(first, 4).value, f"=B{first}*C{first}")
        self.assertEqual(sheet.cell(first + 2, 4).value, f"=B{first + 2}*C{first + 2}")

    def test_subtotal_sums_exactly_the_item_rows(self):
        sheet = self.build()
        first = self.find_row("الوصف") + 1
        last = first + 2
        subtotal_row = self.find_row("المجموع الفرعي")
        self.assertEqual(sheet.cell(subtotal_row, 4).value, f"=SUM(D{first}:D{last})")

    def test_tax_multiplies_the_subtotal_cell_by_the_rate(self):
        sheet = self.build()
        subtotal_row = self.find_row("المجموع الفرعي")
        tax_row = self.find_row("الضريبة")
        self.assertEqual(sheet.cell(tax_row, 4).value, f"=D{subtotal_row}*0.15")

    def test_grand_total_adds_the_subtotal_and_tax_cells(self):
        sheet = self.build()
        subtotal_row = self.find_row("المجموع الفرعي")
        tax_row = self.find_row("الضريبة")
        grand_row = self.find_row("الإجمالي النهائي")
        self.assertEqual(sheet.cell(grand_row, 4).value, f"=D{subtotal_row}+D{tax_row}")

    def test_discount_is_subtracted_from_the_grand_total(self):
        sheet = self.build(document(totals={
            "subtotal": 73.5, "discount": 10.0, "tax_amount": 9.53, "grand_total": 73.03,
        }))
        grand_row = self.find_row("الإجمالي النهائي")
        self.assertIn("-ABS(D", str(sheet.cell(grand_row, 4).value))

    def test_the_read_value_survives_as_a_comment_on_the_formula(self):
        sheet = self.build()
        first = self.find_row("الوصف") + 1
        comment = sheet.cell(first, 4).comment
        self.assertIsNotNone(comment)
        self.assertIn("31.00", comment.text)

    def test_a_row_missing_a_price_keeps_the_read_total_instead_of_a_formula(self):
        sheet = self.build(document(items=[
            {"description": "خدمة", "qty": None, "unit_price": None, "line_total": 500.0,
             "review": {}, "notes": {}},
        ]))
        first = self.find_row("الوصف") + 1
        self.assertEqual(sheet.cell(first, 4).value, 500.0)


class FormulaIntegrityTests(WorkbookCase):
    """The static replacement for recalculating in an office suite."""

    def _assert_sound(self, sheet):
        checked = 0
        for cell in self.formula_cells():
            formula = cell.value
            targets: list[str] = []
            for start, end in RANGE_REFERENCE.findall(formula):
                targets.extend(self._expand(sheet, start, end))
            trimmed = RANGE_REFERENCE.sub("", formula)
            targets.extend(CELL_REFERENCE.findall(trimmed))
            self.assertTrue(targets, f"صيغة بلا مراجع: {formula}")
            for reference in targets:
                target = sheet[reference.replace("$", "")]
                self.assertIsNotNone(
                    target.value,
                    f"{cell.coordinate} = {formula} يشير إلى خلية فارغة {reference} (#VALUE!)",
                )
                # A formula may reference another formula; both resolve to numbers.
                if isinstance(target.value, str) and target.value.startswith("="):
                    continue
                self.assertIsInstance(
                    target.value, (int, float),
                    f"{cell.coordinate} = {formula} يشير إلى نص في {reference} (#VALUE!)",
                )
                checked += 1
        self.assertGreater(checked, 0, "لم تُكتب أي صيغة في الملف")

    @staticmethod
    def _expand(sheet, start: str, end: str) -> list[str]:
        from openpyxl.utils import range_boundaries, get_column_letter

        left, top, right, bottom = range_boundaries(f"{start}:{end}".replace("$", ""))
        return [
            f"{get_column_letter(column)}{row}"
            for row in range(top, bottom + 1)
            for column in range(left, right + 1)
        ]

    def test_every_reference_resolves_to_a_number(self):
        sheet = self.build()
        self._assert_sound(sheet)

    def test_integrity_holds_for_a_single_item_document(self):
        sheet = self.build(document(items=[
            {"description": "قلم", "qty": 1, "unit_price": 9.0, "line_total": 9.0,
             "review": {}, "notes": {}},
        ], totals={"subtotal": 9.0, "grand_total": 9.0}))
        self._assert_sound(sheet)

    def test_no_error_literal_is_ever_written(self):
        self.build()
        for row in self.sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    for literal in ERROR_LITERALS:
                        self.assertNotIn(literal, cell.value)

    def test_no_formula_is_written_when_there_are_no_items(self):
        # An empty table must not leave a SUM over a range that does not exist.
        sheet = self.build(document(items=[], totals={"grand_total": 12.0}))
        for cell in self.formula_cells():
            self.assertNotIn("SUM(", str(cell.value))
        self._ = sheet


class FormattingTests(WorkbookCase):
    def test_every_cell_uses_arial(self):
        sheet = self.build()
        named = [
            cell for row in sheet.iter_rows() for cell in row
            if cell.value is not None and cell.font and cell.font.name
        ]
        self.assertTrue(named)
        for cell in named:
            self.assertEqual(cell.font.name, "Arial", cell.coordinate)

    def test_the_item_header_is_bold_white_on_the_brand_colour(self):
        sheet = self.build()
        header = sheet.cell(self.find_row("الوصف"), 1)
        self.assertTrue(header.font.bold)
        self.assertIn("FFFFFF", str(header.font.color.rgb))
        self.assertIn(excel_builder.BAND, str(header.fill.fgColor.rgb))

    def test_every_item_cell_has_a_thin_border(self):
        sheet = self.build()
        first = self.find_row("الوصف") + 1
        for column in range(1, 5):
            border = sheet.cell(first, column).border
            for side in (border.left, border.right, border.top, border.bottom):
                self.assertEqual(side.style, "thin")

    def test_money_columns_carry_the_document_currency(self):
        sheet = self.build()
        first = self.find_row("الوصف") + 1
        self.assertIn("ر.س", sheet.cell(first, 3).number_format)
        self.assertIn("#,##0.00", sheet.cell(first, 4).number_format)

    def test_quantity_uses_its_own_format(self):
        sheet = self.build()
        first = self.find_row("الوصف") + 1
        self.assertEqual(sheet.cell(first, 2).number_format, "#,##0.###")

    def test_currency_falls_back_to_a_plain_number_format(self):
        sheet = self.build(document(currency=""))
        first = self.find_row("الوصف") + 1
        self.assertEqual(sheet.cell(first, 4).number_format, "#,##0.00")

    def test_a_hostile_currency_string_cannot_break_the_format(self):
        # The currency comes from a model reading pixels; it is untrusted input.
        fmt = excel_builder.money_format('X";@;[Red]')
        self.assertEqual(fmt.count('"'), 2)

    def test_column_widths_are_set_and_clamped(self):
        sheet = self.build()
        widths = [dimension.width for dimension in sheet.column_dimensions.values()]
        self.assertTrue(widths)
        for width in widths:
            self.assertGreaterEqual(width, 10)
            self.assertLessEqual(width, 60)

    def test_an_arabic_cell_reads_right_to_left(self):
        # Per cell, not per sheet: these tables mix an Arabic description with
        # an English SKU, and one sheet-wide setting misplaces the punctuation
        # of whichever language loses.
        sheet = self.build()
        first = self.find_row("الوصف") + 1
        self.assertEqual(sheet.cell(first, 1).alignment.readingOrder, 2)
        self.assertEqual(sheet.cell(first, 1).alignment.horizontal, "right")

    def test_an_english_cell_in_the_same_table_reads_left_to_right(self):
        mixed = document(items=[
            {"description": "Ladies' Garments", "qty": 2, "unit_price": 15.5,
             "line_total": 31.0, "review": {}, "notes": {}},
        ], totals={})
        sheet = self.build(mixed)
        first = self.find_row("الوصف") + 1
        self.assertEqual(sheet.cell(first, 1).alignment.readingOrder, 1)
        self.assertEqual(sheet.cell(first, 1).alignment.horizontal, "left")

    def test_a_numeric_only_cell_is_left_to_the_context(self):
        self.assertEqual(excel_builder.reading_order("1024417"), 0)

    def test_an_empty_cell_is_left_to_the_context(self):
        self.assertEqual(excel_builder.reading_order(""), 0)

    def test_an_arabic_document_opens_right_to_left(self):
        sheet = self.build()
        self.assertTrue(sheet.sheet_view.rightToLeft)

    def test_an_english_document_stays_left_to_right(self):
        sheet = self.build(document(direction="ltr"))
        self.assertFalse(sheet.sheet_view.rightToLeft)

    def test_the_item_table_is_frozen_below_its_header(self):
        sheet = self.build()
        self.assertEqual(sheet.freeze_panes, f"A{self.find_row('الوصف') + 1}")

    def test_page_text_outside_the_table_is_kept(self):
        self.build()
        self.find_row("الدفع خلال 30 يوماً")


class ReviewTests(WorkbookCase):
    def test_a_flagged_cell_is_yellow_and_queued(self):
        flagged = document()
        flagged["items"][1]["review"]["qty"] = True
        flagged["items"][1]["notes"]["qty"] = "راجع الكمية"
        sheet = self.build(flagged)
        row = self.find_row("الوصف") + 2
        self.assertIn("FFF2CC", str(sheet.cell(row, 2).fill.fgColor.rgb))
        _records, low, review_items, _template, _path = self.result
        self.assertEqual(low, 1)
        self.assertEqual(len(review_items), 1)
        self.assertEqual(review_items[0]["column"], 2)
        self.assertEqual(review_items[0]["row"], row)

    def test_a_flagged_formula_cell_queues_the_quantity_instead(self):
        # apply_review_file writes the corrected value straight into the cell,
        # so queueing the formula cell would replace the formula with a constant.
        flagged = document()
        flagged["items"][0]["review"]["line_total"] = True
        sheet = self.build(flagged)
        row = self.find_row("الوصف") + 1
        self.assertTrue(str(sheet.cell(row, 4).value).startswith("="))
        _records, _low, review_items, _template, _path = self.result
        self.assertEqual(len(review_items), 1)
        self.assertEqual(review_items[0]["column"], 2)

    def test_no_queued_cell_ever_points_at_a_formula(self):
        flagged = document()
        flagged["items"][0]["review"]["line_total"] = True
        flagged["items"][1]["review"]["unit_price"] = True
        flagged["totals_review"]["grand_total"] = True
        sheet = self.build(flagged)
        _records, _low, review_items, _template, _path = self.result
        for item in review_items:
            value = sheet.cell(item["row"], item["column"]).value
            self.assertFalse(
                isinstance(value, str) and value.startswith("="),
                f"عنصر مراجعة يشير إلى صيغة في {item['row']},{item['column']}",
            )

    def test_a_flagged_total_is_highlighted(self):
        flagged = document()
        flagged["totals_review"]["subtotal"] = True
        flagged["totals_notes"]["subtotal"] = "المجموع لا يطابق"
        sheet = self.build(flagged)
        cell = sheet.cell(self.find_row("المجموع الفرعي"), 4)
        self.assertIn("FFF2CC", str(cell.fill.fgColor.rgb))

    def test_the_review_queue_points_at_the_saved_workbook(self):
        flagged = document()
        flagged["items"][0]["review"]["qty"] = True
        self.build(flagged)
        _records, _low, review_items, _template, path = self.result
        self.assertEqual(review_items[0]["output"], str(path))


class StructureTests(WorkbookCase):
    def test_record_count_matches_the_item_count(self):
        self.build()
        self.assertEqual(self.result[0], 3)

    def test_one_sheet_per_page(self):
        self.build(document(page=1), document(page=2))
        self.assertEqual(len(self.pages), 2)
        self.assertEqual(self.result[0], 6)

    def test_the_workbook_opens_on_the_review_summary(self):
        # What a reviewer needs first is "is any of this wrong", not row 41.
        self.build()
        self.assertIs(self.book.worksheets[0], self.summary)

    def test_an_english_document_is_written_in_english(self):
        """No Arabic anywhere in an English invoice's workbook.

        The complaint this guards against is a real one: an English invoice came
        back with Arabic headings over its figures, which stops the sheet being
        a transcription of the page it came from.
        """
        # Every scrap of content is English here, so anything Arabic left in the
        # workbook can only have come from the builder's own vocabulary.
        self.build(document(
            direction="ltr",
            title="TAX INVOICE",
            columns=["Item", "Qty", "Unit Price", "Amount"],
            column_roles=["description", "qty", "unit_price", "line_total"],
            items=[{"description": "Steel bracket", "qty": 2,
                    "unit_price": 15.5, "line_total": 31.0}],
            header={"supplier": "Northwind Ltd", "invoice_number": "INV-9"},
            totals={"subtotal": 31.0, "grand_total": 31.0},
            notes=["Payment due within 30 days."],
        ))
        for sheet in self.book.worksheets:
            text = " ".join(
                str(cell.value) for row in sheet.iter_rows() for cell in row
                if isinstance(cell.value, str)
            )
            found = excel_builder.ARABIC.findall(text)
            self.assertFalse(found, f"Arabic in sheet {sheet.title!r}: {found[:8]}")
        self.assertFalse(self.sheet.sheet_view.rightToLeft)
        self.assertFalse(self.summary.sheet_view.rightToLeft)
        self.assertEqual(self.summary.title, "Review")

    def test_an_arabic_document_is_written_in_arabic_and_reads_right_to_left(self):
        self.build(document(direction="rtl"))
        self.assertTrue(self.sheet.sheet_view.rightToLeft)
        self.assertTrue(self.summary.sheet_view.rightToLeft)
        self.assertEqual(self.summary.title, "المراجعة")
        text = " ".join(
            str(cell.value) for row in self.summary.iter_rows() for cell in row
            if isinstance(cell.value, str)
        )
        self.assertIn("ملخّص المراجعة", text)

    def test_the_summary_lists_every_flagged_value_with_a_reason(self):
        self.build(document(items=[{
            "description": "قلم", "qty": 2, "unit_price": 15.5, "line_total": 34.0,
            "review": {"line_total": True}, "notes": {"line_total": "لا يطابق الضرب"},
        }], totals={}))
        text = "\n".join(
            str(cell.value) for row in self.summary.iter_rows() for cell in row if cell.value
        )
        self.assertIn("لا يطابق الضرب", text)

    def test_the_summary_says_so_when_nothing_needs_review(self):
        self.build()
        text = "\n".join(
            str(cell.value) for row in self.summary.iter_rows() for cell in row if cell.value
        )
        self.assertIn("لا شيء", text)

    def test_the_item_table_can_be_sorted_and_filtered(self):
        sheet = self.build()
        self.assertIsNotNone(sheet.auto_filter.ref)

    def test_the_sheet_is_set_up_to_print_on_one_width(self):
        sheet = self.build()
        self.assertEqual(sheet.page_setup.fitToWidth, 1)
        self.assertTrue(sheet.sheet_properties.pageSetUpPr.fitToPage)

    def test_headings_follow_the_printed_column_order(self):
        # Item keys arrive in schema order; the printed table put قيمة first.
        sheet = self.build(document(
            columns=["الإجمالي", "سعر الوحدة", "الكمية", "الوصف"],
            column_roles=["line_total", "unit_price", "qty", "description"],
        ))
        row = self.find_row("الإجمالي")
        self.assertEqual(sheet.cell(row, 1).value, "الإجمالي")
        self.assertEqual(sheet.cell(row, 4).value, "الوصف")

    def test_the_formula_follows_the_reordered_columns(self):
        sheet = self.build(document(
            columns=["الإجمالي", "سعر الوحدة", "الكمية", "الوصف"],
            column_roles=["line_total", "unit_price", "qty", "description"],
        ))
        first = self.find_row("الإجمالي") + 1
        self.assertEqual(sheet.cell(first, 1).value, f"=C{first}*B{first}")

    def test_an_unknown_field_becomes_a_plain_column(self):
        sheet = self.build(document(items=[
            {"description": "قلم", "qty": 2, "unit_price": 15.5, "line_total": 31.0,
             "warranty": "سنتان", "review": {}, "notes": {}},
        ], totals={}))
        row = self.find_row("الوصف")
        self.assertEqual(sheet.cell(row, 5).value, "warranty")
        self.assertEqual(sheet.cell(row + 1, 5).value, "سنتان")

    def test_a_page_with_no_table_still_writes_its_text(self):
        self.build(document(items=[], totals={}, notes=["نص حر", "سطر ثانٍ"]))
        self.find_row("نص حر")
        self.find_row("سطر ثانٍ")


if __name__ == "__main__":
    unittest.main()
