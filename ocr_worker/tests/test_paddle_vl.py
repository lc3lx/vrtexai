"""Tests for turning PaddleOCR-VL page structure into the extraction payload.

PaddleOCR-VL reports layout: titles, text blocks, and HTML table grids. It does
not say which column is a quantity, who the supplier is, or what the totals
mean. That mapping is this module's job, and it is where a silent mistake would
produce a confident, wrong invoice — so it is tested against fixed structures
rather than against the model.

Nothing here needs PaddleOCR 3.x: the conversion takes plain dictionaries.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import paddle_vl

ITEM_TABLE = """
<table>
  <tr><td>الوصف</td><td>الكمية</td><td>سعر الوحدة</td><td>الإجمالي</td></tr>
  <tr><td>قلم</td><td>2</td><td>15.50</td><td>31.00</td></tr>
  <tr><td>دفتر</td><td>3</td><td>10.00</td><td>30.00</td></tr>
  <tr><td>ممحاة</td><td>10</td><td>1.25</td><td>12.50</td></tr>
</table>
"""


def page(**overrides) -> dict:
    base = {
        "result": {
            "parsing_res_list": [
                {"block_label": "doc_title", "block_content": "فاتورة ضريبية"},
                {"block_label": "text",
                 "block_content": "المورد: شركة الأفق\nرقم الفاتورة: INV-2201"},
                {"block_label": "table", "block_content": ITEM_TABLE},
                {"block_label": "text",
                 "block_content": "المجموع الفرعي: 73.50\nالضريبة: 11.03\nالإجمالي: 84.53"},
            ]
        },
        "markdown": "# فاتورة ضريبية",
    }
    base.update(overrides)
    return base


class TableShapeTests(unittest.TestCase):
    def test_html_rows_reuses_the_existing_parser(self):
        rows = paddle_vl.html_rows(ITEM_TABLE)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1], ["قلم", "2", "15.50", "31.00"])

    def test_html_entities_are_decoded_into_the_cell(self):
        # Seen on a real licence: cells arrived as "Handbags &amp; Leather" and
        # "Ladies&#x27; Garments" and were written into the sheet that way.
        rows = paddle_vl.html_rows(
            "<table><tr><td>Handbags &amp; Leather</td><td>Ladies&#x27; Garments</td></tr></table>"
        )
        self.assertEqual(rows[0], ["Handbags & Leather", "Ladies' Garments"])

    def test_a_text_only_first_row_is_treated_as_a_header(self):
        self.assertTrue(paddle_vl._looks_like_header(["الوصف", "الكمية", "الإجمالي"]))

    def test_a_row_carrying_numbers_is_not_a_header(self):
        self.assertFalse(paddle_vl._looks_like_header(["قلم", "2", "31.00"]))

    def test_a_single_cell_row_is_not_a_header(self):
        # A stray caption above the grid must not be eaten as column names.
        self.assertFalse(paddle_vl._looks_like_header(["البنود"]))

    def test_the_widest_tallest_table_is_chosen_as_the_item_grid(self):
        small = [["الرقم الضريبي", "300000"]]
        large = paddle_vl.html_rows(ITEM_TABLE)
        self.assertIs(paddle_vl._pick_table([small, large]), large)


class DirectionTests(unittest.TestCase):
    def test_arabic_text_reads_right_to_left(self):
        self.assertEqual(paddle_vl._direction("فاتورة ضريبية للعميل"), "rtl")

    def test_english_text_reads_left_to_right(self):
        self.assertEqual(paddle_vl._direction("Tax Invoice for customer"), "ltr")

    def test_a_bilingual_form_reads_right_to_left(self):
        # The failure this replaces: a real UAE licence prints every field in
        # both languages, the English half has more letters, and a majority vote
        # therefore opened an Arabic document left-to-right.
        bilingual = (
            "Company Name اسم الشركة  Legal Type الشكل القانوني  "
            "Expiry Date تاريخ الانتهاء  Register No. رقم السجل"
        )
        self.assertEqual(paddle_vl._direction(bilingual), "rtl")

    def test_an_english_page_with_one_arabic_stamp_stays_left_to_right(self):
        mostly_english = "Commercial Invoice for goods delivered to the buyer " * 3 + "ختم"
        self.assertEqual(paddle_vl._direction(mostly_english), "ltr")

    def test_a_page_with_no_letters_does_not_crash(self):
        self.assertEqual(paddle_vl._direction("12345 67.89 %"), "ltr")


class TotalsTests(unittest.TestCase):
    def test_labelled_amounts_are_read_from_page_text(self):
        totals = paddle_vl._totals_from_lines(
            ["المجموع الفرعي: 73.50", "الضريبة: 11.03", "الإجمالي: 84.53"]
        )
        self.assertEqual(totals["subtotal"], 73.50)
        self.assertEqual(totals["tax_amount"], 11.03)
        self.assertEqual(totals["grand_total"], 84.53)

    def test_the_subtotal_line_is_not_claimed_twice_as_the_grand_total(self):
        # "الإجمالي قبل الضريبة" matches the broad total label too. Consuming the
        # line under the specific label is what stops a phantom mismatch.
        totals = paddle_vl._totals_from_lines(["الإجمالي قبل الضريبة: 73.50"])
        self.assertEqual(totals.get("subtotal"), 73.50)
        self.assertNotIn("grand_total", totals)

    def test_a_label_with_no_number_after_it_is_ignored(self):
        self.assertEqual(paddle_vl._totals_from_lines(["الإجمالي:"]), {})

    def test_english_labels_are_read_too(self):
        totals = paddle_vl._totals_from_lines(["Subtotal 100.00", "Grand Total 115.00"])
        self.assertEqual(totals["subtotal"], 100.00)
        self.assertEqual(totals["grand_total"], 115.00)


class RoleTests(unittest.TestCase):
    def test_roles_come_from_arithmetic_not_from_the_heading_alone(self):
        rows = [[paddle_vl._cell(value) for value in row]
                for row in paddle_vl.html_rows(ITEM_TABLE)[1:]]
        roles = paddle_vl._roles_for(["الوصف", "الكمية", "سعر الوحدة", "الإجمالي"], rows)
        self.assertEqual(roles, ["description", "qty", "unit_price", "line_total"])

    def test_unnamed_columns_are_still_resolved_when_the_maths_holds(self):
        # A scan that lost its header row must not lose its formulas.
        rows = [[paddle_vl._cell(value) for value in row]
                for row in paddle_vl.html_rows(ITEM_TABLE)[1:]]
        roles = paddle_vl._roles_for([], rows)
        self.assertEqual(roles[1], "qty")
        self.assertEqual(roles[2], "unit_price")
        self.assertEqual(roles[3], "line_total")

    def test_a_table_whose_numbers_relate_to_nothing_gets_no_invented_roles(self):
        rows = [[paddle_vl._cell(value) for value in row] for row in [
            ["بند", "5", "9", "77"],
            ["بند", "3", "4", "12000"],
            ["بند", "8", "2", "31"],
        ]]
        roles = paddle_vl._roles_for(["أ", "ب", "ج", "د"], rows)
        self.assertNotIn("line_total", roles)


class PayloadTests(unittest.TestCase):
    def test_a_full_page_converts_to_the_extraction_schema(self):
        payload = paddle_vl.to_payload(page())
        self.assertEqual(payload["document_type"], "invoice")
        self.assertEqual(payload["direction"], "rtl")
        self.assertEqual(payload["title"], "فاتورة ضريبية")
        self.assertEqual(payload["column_roles"],
                         ["description", "qty", "unit_price", "line_total"])
        self.assertEqual(len(payload["items"]), 3)
        # Numeric roles arrive as numbers: a reader returns text, and converting
        # here is what stops the shape gate rejecting every cell.
        self.assertEqual(payload["items"][0]["qty"], 2.0)
        self.assertEqual(payload["items"][0]["unit_price"], 15.5)
        self.assertEqual(payload["totals"]["grand_total"], 84.53)

    def test_an_unreadable_numeric_cell_is_kept_verbatim_for_review(self):
        broken = ITEM_TABLE.replace("<td>2</td>", "<td>غير محدد</td>")
        payload = paddle_vl.to_payload(page(result={"parsing_res_list": [
            {"block_label": "table", "block_content": broken},
        ]}))
        self.assertEqual(payload["items"][0]["qty"], "غير محدد")

    def test_a_blank_numeric_cell_becomes_absent_not_zero(self):
        blank = ITEM_TABLE.replace("<td>2</td>", "<td></td>")
        payload = paddle_vl.to_payload(page(result={"parsing_res_list": [
            {"block_label": "table", "block_content": blank},
        ]}))
        self.assertIsNone(payload["items"][0].get("qty"))

    def test_header_fields_are_pulled_from_the_page_text(self):
        payload = paddle_vl.to_payload(page())
        self.assertEqual(payload["header"].get("invoice_number"), "INV-2201")
        self.assertIn("الأفق", payload["header"].get("supplier", ""))

    def test_the_converted_payload_passes_the_extraction_gates(self):
        # The contract between the two modules, checked end to end.
        import ai_extract

        document, blocking, _advisory = ai_extract.validate(paddle_vl.to_payload(page()), set())
        self.assertEqual(blocking, [])
        self.assertEqual(len(document["items"]), 3)
        self.assertEqual(document["items"][0]["qty"], 2.0)

    def test_a_page_with_no_table_keeps_its_text(self):
        payload = paddle_vl.to_payload(page(result={"parsing_res_list": [
            {"block_label": "text", "block_content": "ملاحظة مكتوبة بخط اليد"},
        ]}))
        self.assertEqual(payload["items"], [])
        self.assertIn("ملاحظة مكتوبة بخط اليد", payload["notes"])

    def test_a_second_table_becomes_fields_rather_than_being_dropped(self):
        """A two-column side table is a list of fields, so it is read as one.

        It used to be pasted into a note as "الرقم الضريبي | 300000000" — one
        unreadable cell in the workbook, and a string the field patterns then
        had to guess their way back out of. The table already says which value
        belongs to which label; believing it is both simpler and right.
        """
        extra = "<table><tr><td>الرقم الضريبي</td><td>300000000</td></tr></table>"
        payload = paddle_vl.to_payload(page(result={"parsing_res_list": [
            {"block_label": "table", "block_content": ITEM_TABLE},
            {"block_label": "table", "block_content": extra},
        ]}))
        self.assertEqual(len(payload["items"]), 3)
        self.assertEqual(payload["header"].get("tax_number"), "300000000")
        # And nothing anywhere carries the two pasted together.
        for note in payload["notes"]:
            self.assertNotIn("|", note)

    def test_an_unknown_side_label_keeps_the_wording_the_document_used(self):
        """Every company prints different fields, so an unrecognised one is kept
        under its own label rather than discarded or flattened into text."""
        extra = "<table><tr><td>Delivery Note</td><td>DN-4471</td></tr></table>"
        payload = paddle_vl.to_payload(page(result={"parsing_res_list": [
            {"block_label": "table", "block_content": ITEM_TABLE},
            {"block_label": "table", "block_content": extra},
        ]}))
        self.assertEqual(payload["header"].get("Delivery Note"), "DN-4471")

    def test_pictures_and_seals_are_skipped(self):
        payload = paddle_vl.to_payload(page(result={"parsing_res_list": [
            {"block_label": "seal", "block_content": "ختم الشركة"},
            {"block_label": "text", "block_content": "نص حقيقي"},
        ]}))
        self.assertNotIn("ختم الشركة", payload["notes"])
        self.assertIn("نص حقيقي", payload["notes"])

    def test_markdown_is_used_when_no_blocks_are_reported(self):
        payload = paddle_vl.to_payload({"result": {}, "markdown": "سطر أول\nسطر ثانٍ"})
        self.assertIn("سطر أول", payload["notes"])

    def test_an_empty_page_does_not_raise(self):
        payload = paddle_vl.to_payload({"result": {}, "markdown": ""})
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["notes"], [])


class UntouchedImageTests(unittest.TestCase):
    """The model must see the captured pixels, at full resolution.

    Downscaling would be cheaper but discards exactly the small print — tax
    numbers, stamps, dense rows — this path exists to recover. These pin that
    no resampling creeps back in.
    """

    @staticmethod
    def page(height: int, width: int):
        import numpy as np

        return np.arange(height * width * 3, dtype=np.uint8).reshape(height, width, 3)

    def test_a_large_page_reaches_the_model_at_its_own_size(self):
        original = self.page(4000, 3000)
        self.assertEqual(paddle_vl._condition(original, "raw").shape, original.shape)

    def test_the_very_same_array_is_passed_through(self):
        original = self.page(120, 90)
        self.assertIs(paddle_vl._condition(original, "raw"), original)

    def test_no_variant_alters_the_pixels(self):
        import numpy as np

        original = self.page(200, 150)
        for variant in ("raw", "prepared", "upscaled", "anything"):
            with self.subTest(variant=variant):
                result = paddle_vl._condition(original, variant)
                self.assertEqual(result.shape, original.shape)
                self.assertTrue(np.array_equal(result, original))


class AvailabilityTests(unittest.TestCase):
    def test_the_engine_reports_itself_unavailable_when_switched_off(self):
        import os

        previous = os.environ.get("VERTEX_AI_EXTRACT")
        os.environ["VERTEX_AI_EXTRACT"] = "off"
        try:
            ok, detail = paddle_vl.available()
            self.assertFalse(ok)
            self.assertIn("off", detail)
        finally:
            if previous is None:
                os.environ.pop("VERTEX_AI_EXTRACT", None)
            else:
                os.environ["VERTEX_AI_EXTRACT"] = previous


if __name__ == "__main__":
    unittest.main()
