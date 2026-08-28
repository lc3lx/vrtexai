"""Tests for the gates that stand between the vision model and the workbook.

These are the failures that make an AI-led extraction dangerous rather than
merely wrong: a number formatted as text, a row whose arithmetic does not hold,
and a figure that is printed nowhere on the page. All three are exercised with
fixed payloads, so nothing here needs Ollama, a model, or an image.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai_extract


def word(text: str) -> dict:
    return {"text": text, "x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0, "conf": 95.0}


def payload(**overrides) -> dict:
    base = {
        "document_type": "invoice",
        "direction": "rtl",
        "currency": "SAR",
        "columns": ["الوصف", "الكمية", "سعر الوحدة", "الإجمالي"],
        "column_roles": ["description", "qty", "unit_price", "line_total"],
        "items": [
            {"description": "قلم", "qty": 2, "unit_price": 15.5, "line_total": 31.0},
            {"description": "دفتر", "qty": 3, "unit_price": 10.0, "line_total": 30.0},
        ],
        "totals": {"subtotal": 61.0, "tax_rate": 0.15, "tax_amount": 9.15, "grand_total": 70.15},
    }
    base.update(overrides)
    return base


PAGE_WORDS = [
    word(text)
    for text in ("قلم", "2", "15.50", "31.00", "دفتر", "3", "10.00", "30.00", "61.00", "9.15", "70.15")
]


class ShapeTests(unittest.TestCase):
    def test_clean_payload_passes_every_gate(self):
        seen = ai_extract.page_numbers(PAGE_WORDS)
        _document, blocking, _advisory = ai_extract.validate(payload(), seen)
        self.assertEqual(blocking, [])

    def test_numeric_string_is_reported_and_converted(self):
        # The single most common model slip: "1,234.50" instead of 1234.5.
        broken = payload(items=[
            {"description": "قلم", "qty": "2", "unit_price": "15.50", "line_total": "31.00"},
        ])
        document, blocking, _ = ai_extract.validate(broken, set())
        self.assertTrue(any("نصاً" in message for message in blocking))
        # It is still converted, so a third failed attempt does not lose the row.
        item = document["items"][0]
        self.assertEqual(item["qty"], 2.0)
        self.assertEqual(item["unit_price"], 15.5)

    def test_unknown_role_is_reported_and_downgraded(self):
        broken = payload(column_roles=["description", "qty", "unit_price", "grand_total"])
        document, blocking, _ = ai_extract.validate(broken, set())
        self.assertTrue(any("column_roles" in message for message in blocking))
        self.assertEqual(document["column_roles"][3], "other")

    def test_role_and_column_length_mismatch_is_reported(self):
        broken = payload(column_roles=["description", "qty"])
        document, blocking, _ = ai_extract.validate(broken, set())
        self.assertTrue(any("يتساوى الطولان" in message for message in blocking))
        self.assertEqual(len(document["column_roles"]), len(document["columns"]))

    def test_empty_extraction_is_blocking(self):
        _document, blocking, _ = ai_extract.validate(
            payload(items=[], totals={}, notes=[]), set()
        )
        self.assertTrue(any("لم يُرجع النموذج أي بنود" in message for message in blocking))


class ArithmeticTests(unittest.TestCase):
    def test_row_that_does_not_multiply_is_flagged_on_the_cells(self):
        broken = payload(items=[
            {"description": "قلم", "qty": 2, "unit_price": 15.5, "line_total": 34.0},
        ], totals={})
        document, blocking, _ = ai_extract.validate(broken, set())
        self.assertTrue(any("لا يساوي الإجمالي المكتوب" in message for message in blocking))
        item = document["items"][0]
        # All three cells are marked, because any one of them could be the misread.
        self.assertTrue(item["review"]["qty"])
        self.assertTrue(item["review"]["unit_price"])
        self.assertTrue(item["review"]["line_total"])

    def test_items_that_do_not_sum_to_subtotal_flag_the_subtotal(self):
        broken = payload(totals={"subtotal": 99.0, "grand_total": 99.0})
        document, blocking, _ = ai_extract.validate(broken, set())
        self.assertTrue(any("مجموع البنود" in message for message in blocking))
        self.assertTrue(document["totals_review"]["subtotal"])

    def test_grand_total_that_ignores_tax_is_flagged(self):
        broken = payload(totals={"subtotal": 61.0, "tax_amount": 9.15, "grand_total": 61.0})
        document, blocking, _ = ai_extract.validate(broken, set())
        self.assertTrue(any("لا يساوي الإجمالي" in message for message in blocking))
        self.assertTrue(document["totals_review"]["grand_total"])

    def test_discount_reconciles_with_either_sign(self):
        # An invoice may print a discount as -50 or as 50 under a "خصم" label.
        for discount in (50.0, -50.0):
            with self.subTest(discount=discount):
                clean = payload(totals={
                    "subtotal": 61.0, "tax_amount": 9.15,
                    "discount": discount, "grand_total": 20.15,
                })
                _document, blocking, _ = ai_extract.validate(clean, set())
                self.assertFalse(
                    any("لا يساوي الإجمالي" in message for message in blocking),
                    blocking,
                )


class GroundingTests(unittest.TestCase):
    def test_number_absent_from_the_page_is_flagged_but_kept(self):
        invented = payload(items=[
            {"description": "قلم", "qty": 2, "unit_price": 15.5, "line_total": 31.0},
            {"description": "بند مخترع", "qty": 4, "unit_price": 99.99, "line_total": 399.96},
        ], totals={})
        seen = ai_extract.page_numbers(PAGE_WORDS)
        document, blocking, advisory = ai_extract.validate(invented, seen)
        # Reported, but as advice. The second reader is the weaker engine, and a
        # figure it missed is evidence about that reader, not proof the value is
        # wrong — so it annotates and never condemns the document.
        self.assertTrue(any("غير موجود في قراءة OCR" in message for message in advisory))
        self.assertEqual(blocking, [])
        row = document["items"][1]
        self.assertTrue(row["review"]["unit_price"])
        # The row survives: deleting it would silently lose data the model may
        # have read from a region OCR missed.
        self.assertEqual(row["unit_price"], 99.99)
        self.assertTrue(row["review"]["unit_price"])
        self.assertIn("راجعه", row["notes"]["unit_price"])

    def test_thousands_separator_does_not_count_as_invented(self):
        seen = ai_extract.page_numbers([word("1,234.50")])
        self.assertTrue(ai_extract._grounded(1234.5, seen))

    def test_ocr_dropping_the_decimal_point_does_not_count_as_invented(self):
        # PaddleOCR routinely reads "31.00" as "3100"; the digits are still there.
        seen = ai_extract.page_numbers([word("3100")])
        self.assertTrue(ai_extract._grounded(31.0, seen))

    def test_arabic_indic_digits_are_matched(self):
        seen = ai_extract.page_numbers([word("٣١٫٠٠")])
        self.assertTrue(ai_extract._grounded(31.0, seen))

    def test_competing_ocr_reading_also_grounds_a_value(self):
        # The cell the two recognizers disagreed about is exactly where the
        # vision model is most likely to be the correct one.
        cell = word("81.00")
        cell["alternatives"] = [{"text": "31.00", "conf": 60.0}]
        seen = ai_extract.page_numbers([cell])
        self.assertTrue(ai_extract._grounded(31.0, seen))

    def test_grounding_is_skipped_when_ocr_read_nothing(self):
        # With no OCR evidence at all, every number would look invented.
        _document, blocking, advisory = ai_extract.validate(payload(), set())
        self.assertEqual(advisory, [])
        self.assertEqual(blocking, [])


class EvidenceIsAdvisoryTests(unittest.TestCase):
    """The weaker reader corroborates; it never condemns.

    A cloud model resolves small print the local engine cannot. Counting the
    local engine's misses as errors flagged correct figures and made a good
    extraction look poor — which is the opposite of what the gate is for.
    """

    def setUp(self):
        self.unseen = ai_extract.page_numbers([
            {"text": "قلم", "conf": 99.0}, {"text": "2", "conf": 99.0},
        ])

    def test_a_number_the_second_reader_missed_does_not_fail_the_document(self):
        _document, blocking, advisory = ai_extract.validate(payload(), self.unseen)
        self.assertEqual(blocking, [], "an uncorroborated value blocked the document")
        self.assertTrue(advisory, "the miss should still be reported")

    def test_it_is_still_flagged_for_the_reviewer(self):
        document, _blocking, _advisory = ai_extract.validate(payload(), self.unseen)
        self.assertTrue(any(item["review"] for item in document["items"]))

    def test_the_value_itself_survives(self):
        document, _blocking, _advisory = ai_extract.validate(payload(), self.unseen)
        self.assertEqual(document["items"][0]["line_total"], 31.0)

    def test_arithmetic_still_blocks(self):
        # The gate that proves rather than corroborates keeps its teeth.
        broken = payload(items=[
            {"description": "قلم", "qty": 2, "unit_price": 15.5, "line_total": 34.0},
        ], totals={})
        _document, blocking, _advisory = ai_extract.validate(broken, self.unseen)
        self.assertTrue(blocking, "a line total that does not multiply was accepted")

    def test_the_second_reading_can_be_switched_off(self):
        import os

        previous = os.environ.get("VERTEX_EVIDENCE_OCR")
        os.environ["VERTEX_EVIDENCE_OCR"] = "off"
        try:
            _document, _blocking, advisory = ai_extract.validate(payload(), self.unseen)
            self.assertEqual(advisory, [], "grounding ran while switched off")
        finally:
            if previous is None:
                os.environ.pop("VERTEX_EVIDENCE_OCR", None)
            else:
                os.environ["VERTEX_EVIDENCE_OCR"] = previous


class ReadAttemptTests(unittest.TestCase):
    """Retries are for a failed *read*, never for a disputed *reading*.

    The page reaches the model untouched, so a second read of the same pixels
    returns the same answer. Spending another full inference to re-learn that
    would cost minutes and change nothing, so a disputed value is kept and
    flagged instead.
    """

    def setUp(self):
        self.reads = 0
        self._read_page = ai_extract.paddle_vl.read_page

    def tearDown(self):
        ai_extract.paddle_vl.read_page = self._read_page

    def _install(self, replies: list):
        queue = list(replies)

        def fake(image, *args, **kwargs):
            self.reads += 1
            reply = queue.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        ai_extract.paddle_vl.read_page = fake

    def test_a_clean_reading_is_read_once(self):
        self._install([payload()])
        document, notes = ai_extract.read_page_document(object(), PAGE_WORDS)
        self.assertEqual(self.reads, 1)
        self.assertEqual(len(document["items"]), 2)
        self.assertTrue(any("clean" in note for note in notes))

    def test_a_disputed_reading_is_kept_and_flagged_without_re_reading(self):
        broken = payload(items=[
            {"description": "قلم", "qty": 2, "unit_price": 15.5, "line_total": 34.0},
        ], totals={})
        self._install([broken])
        document, notes = ai_extract.read_page_document(object(), PAGE_WORDS)
        self.assertEqual(self.reads, 1)
        self.assertEqual(len(document["items"]), 1)
        self.assertTrue(document["items"][0]["review"]["line_total"])
        self.assertTrue(any("accepted-with-review" in note for note in notes))

    def test_the_image_is_passed_through_untouched(self):
        seen: list = []
        ai_extract.paddle_vl.read_page = lambda image, *a, **k: seen.append(image) or payload()
        page_image = object()
        ai_extract.read_page_document(page_image, PAGE_WORDS)
        self.assertIs(seen[0], page_image)

    def test_a_crashed_worker_is_retried(self):
        self._install([RuntimeError("توقّف محرك PaddleOCR-VL"), payload()])
        document, _notes = ai_extract.read_page_document(object(), PAGE_WORDS)
        self.assertEqual(self.reads, 2)
        self.assertEqual(len(document["items"]), 2)

    def test_every_read_failing_raises_so_the_caller_can_fall_back(self):
        self._install([RuntimeError("فشل محرك PaddleOCR-VL")] * ai_extract.MAX_READ_ATTEMPTS)
        with self.assertRaises(RuntimeError):
            ai_extract.read_page_document(object(), PAGE_WORDS)
        self.assertEqual(self.reads, ai_extract.MAX_READ_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
