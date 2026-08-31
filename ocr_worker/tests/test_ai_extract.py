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
        self.assertTrue(any("arrived as text" in message for message in blocking))
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
        self.assertTrue(any("the two must match" in message for message in blocking))
        self.assertEqual(len(document["column_roles"]), len(document["columns"]))

    def test_empty_extraction_is_blocking(self):
        _document, blocking, _ = ai_extract.validate(
            payload(items=[], totals={}, notes=[]), set()
        )
        self.assertTrue(any("returned no items" in message for message in blocking))


class ArithmeticTests(unittest.TestCase):
    def test_a_row_no_single_slip_explains_is_flagged_on_every_cell(self):
        # 2 x 15.5 is nowhere near 999, and no one misread digit gets it there,
        # so all three cells are marked: any of them could be the wrong one.
        broken = payload(items=[
            {"description": "قلم", "qty": 2, "unit_price": 15.5, "line_total": 999.0},
        ], totals={})
        document, blocking, _ = ai_extract.validate(broken, set())
        self.assertTrue(any("not the printed line total" in message for message in blocking))
        item = document["items"][0]
        self.assertEqual(item["line_total"], 999.0, "an unprovable value was overwritten")
        self.assertTrue(item["review"]["qty"])
        self.assertTrue(item["review"]["unit_price"])
        self.assertTrue(item["review"]["line_total"])

    def test_a_misread_digit_the_arithmetic_can_prove_is_corrected(self):
        # 34 is one digit away from 31, and nothing else on the row is. The
        # correction is not a guess: it is the only reading that multiplies.
        broken = payload(items=[
            {"description": "قلم", "qty": 2, "unit_price": 15.5, "line_total": 34.0},
        ], totals={})
        document, blocking, _ = ai_extract.validate(broken, set())
        item = document["items"][0]
        self.assertEqual(item["line_total"], 31.0)
        self.assertTrue(item["review"]["line_total"], "a corrected cell must still be marked")
        self.assertIn("34", item["notes"]["line_total"], "the note must say what was read")
        self.assertTrue(any("corrected" in message for message in blocking))

    def test_the_customers_own_error_is_corrected(self):
        """A rate of ٢٥٠٠٠ transcribed as 2500, which is what they hit.

        The quantity and the taxable value pin the rate exactly, and 2500 is one
        dropped digit from 25000 — so the page is readable after all.
        """
        broken = payload(items=[
            {"description": "دبل إنسبيرون", "qty": 2, "unit_price": 2500.0,
             "line_total": 50000.0},
        ], totals={})
        document, _blocking, _advisory = ai_extract.validate(broken, set())
        self.assertEqual(document["items"][0]["unit_price"], 25000.0)

    def test_an_ambiguous_row_is_never_rewritten(self):
        # 3 x 4 = 120 could be a slip in any of the three. Silence beats a guess.
        broken = payload(items=[
            {"description": "قلم", "qty": 3, "unit_price": 4.0, "line_total": 120.0},
        ], totals={})
        document, _blocking, _advisory = ai_extract.validate(broken, set())
        item = document["items"][0]
        self.assertEqual((item["qty"], item["unit_price"], item["line_total"]), (3.0, 4.0, 120.0))

    def test_the_second_reader_settles_which_figure_was_misread(self):
        # The page carries 25000 and 50000; it does not carry 2500. That is what
        # says the rate is the misreading and not the taxable value.
        seen = ai_extract.page_numbers([word(text) for text in ("2", "25000", "50000")])
        broken = payload(items=[
            {"description": "دبل إنسبيرون", "qty": 2, "unit_price": 2500.0,
             "line_total": 50000.0},
        ], totals={})
        document, _blocking, _advisory = ai_extract.validate(broken, seen)
        self.assertEqual(document["items"][0]["unit_price"], 25000.0)
        self.assertEqual(document["items"][0]["line_total"], 50000.0)

    def test_items_that_do_not_sum_to_subtotal_flag_the_subtotal(self):
        broken = payload(totals={"subtotal": 99.0, "grand_total": 99.0})
        document, blocking, _ = ai_extract.validate(broken, set())
        self.assertTrue(any("The items add up to" in message for message in blocking))
        self.assertTrue(document["totals_review"]["subtotal"])

    def test_grand_total_that_ignores_tax_is_flagged(self):
        broken = payload(totals={"subtotal": 61.0, "tax_amount": 9.15, "grand_total": 61.0})
        document, blocking, _ = ai_extract.validate(broken, set())
        self.assertTrue(any("does not equal the total" in message for message in blocking))
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
                    any("does not equal the total" in message for message in blocking),
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
        self.assertTrue(any("is not in the OCR reading" in message for message in advisory))
        self.assertEqual(blocking, [])
        row = document["items"][1]
        self.assertTrue(row["review"]["unit_price"])
        # The row survives: deleting it would silently lose data the model may
        # have read from a region OCR missed.
        self.assertEqual(row["unit_price"], 99.99)
        self.assertTrue(row["review"]["unit_price"])
        self.assertIn("check it", row["notes"]["unit_price"])

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
        document, blocking, advisory = ai_extract.validate(payload(), set())
        self.assertFalse([note for note in advisory if "not in the OCR reading" in note])
        self.assertEqual(blocking, [])
        # But the page must not then be reported as one that passed the check.
        self.assertFalse(document["evidence_checked"])
        self.assertTrue(any("no independent reading" in note for note in advisory))


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
            {"description": "قلم", "qty": 2, "unit_price": 15.5, "line_total": 999.0},
        ], totals={})
        _document, blocking, _advisory = ai_extract.validate(broken, self.unseen)
        self.assertTrue(blocking, "a line total that does not multiply was accepted")

    def test_the_second_reading_can_be_switched_off(self):
        import os

        previous = os.environ.get("VERTEX_EVIDENCE_OCR")
        os.environ["VERTEX_EVIDENCE_OCR"] = "off"
        try:
            _document, _blocking, advisory = ai_extract.validate(payload(), self.unseen)
            # The page's own figures are no longer questioned. The one remaining
            # note says the check did not run, which is the point of saying it.
            self.assertFalse(
                [note for note in advisory if "not in the OCR reading" in note],
                "grounding ran while switched off",
            )
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


def page(number: int, **overrides) -> dict:
    """One validated page, as ``analyze`` has it just before merging."""
    document, _blocking, _advisory = ai_extract.validate(payload(**overrides), set())
    document["page"] = number
    return document


class MergeTests(unittest.TestCase):
    """A four-page manifest is one shipment, not four.

    The join is refused rather than guessed at: merging two invoices that
    happened to share a template would sum one customer's goods into another's
    total, which is a worse failure than splitting a document that belonged
    together — a reviewer sees that at a glance.
    """

    def test_a_continuation_page_becomes_more_rows_of_the_same_document(self):
        first = page(1, header={"invoice_number": "INV-7", "consignee": "Northwind"})
        second = page(2, header={"invoice_number": "INV-7"}, items=[
            {"description": "دفتر", "qty": 1, "unit_price": 4.0, "line_total": 4.0},
        ], totals={})
        merged = ai_extract.merge_pages([first, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]["items"]), 3)
        self.assertEqual(merged[0]["pages"], [1, 2])

    def test_the_header_reaches_every_row_of_a_later_page(self):
        first = page(1, header={"invoice_number": "INV-7", "consignee": "Northwind"})
        second = page(2, header={"invoice_number": "INV-7"}, items=[
            {"description": "دفتر", "qty": 1, "unit_price": 4.0, "line_total": 4.0},
        ], totals={})
        merged = ai_extract.merge_pages([first, second])
        self.assertEqual(merged[0]["header"]["consignee"], "Northwind")
        self.assertEqual([item["_page"] for item in merged[0]["items"]], [1, 1, 2])

    def test_two_different_orders_stay_two_documents(self):
        first = page(1, header={"invoice_number": "INV-7"})
        second = page(2, header={"invoice_number": "INV-8"})
        merged = ai_extract.merge_pages([first, second])
        self.assertEqual(len(merged), 2)

    def test_a_page_naming_a_different_consignee_starts_a_new_document(self):
        # Neither page carries a number, so the parties are what separates them.
        first = page(1, header={"consignee": "Northwind"})
        second = page(2, header={"consignee": "Contoso"})
        merged = ai_extract.merge_pages([first, second])
        self.assertEqual(len(merged), 2)

    def test_an_unheaded_continuation_page_joins_the_page_before_it(self):
        # The commonest shape: page 2 of a manifest is the grid and nothing else.
        first = page(1, header={"consignee": "Northwind"})
        second = page(2, header={}, totals={}, items=[
            {"description": "دفتر", "qty": 1, "unit_price": 4.0, "line_total": 4.0},
        ])
        merged = ai_extract.merge_pages([first, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["header"]["consignee"], "Northwind")

    def test_a_page_with_a_different_table_starts_a_new_document(self):
        first = page(1, header={})
        second = page(2, header={}, columns=["البند", "المبلغ"],
                      column_roles=["description", "line_total"], totals={}, items=[
                          {"description": "شحن", "line_total": 4.0},
                      ])
        merged = ai_extract.merge_pages([first, second])
        self.assertEqual(len(merged), 2)

    def test_the_last_page_to_state_a_total_is_the_one_that_means_it(self):
        # An earlier page carries a running figure; the last carries the amount
        # due, and that is the one the arithmetic must be checked against.
        first = page(1, header={"invoice_number": "INV-7"},
                     totals={"subtotal": 61.0, "grand_total": 61.0})
        second = page(2, header={"invoice_number": "INV-7"}, items=[
            {"description": "دفتر", "qty": 1, "unit_price": 4.0, "line_total": 4.0},
        ], totals={"subtotal": 65.0, "grand_total": 65.0})
        merged = ai_extract.merge_pages([first, second])
        self.assertEqual(merged[0]["totals"]["grand_total"], 65.0)

    def test_the_arithmetic_is_rechecked_against_the_whole_document(self):
        # Page 1's items do not add up to a subtotal that covers both pages, and
        # that page-level complaint must not survive into the merged sheet.
        first = page(1, header={"invoice_number": "INV-7"},
                     totals={"subtotal": 65.0, "grand_total": 65.0})
        self.assertTrue(first["totals_review"].get("subtotal"))
        second = page(2, header={"invoice_number": "INV-7"}, items=[
            {"description": "دفتر", "qty": 1, "unit_price": 4.0, "line_total": 4.0},
        ], totals={"subtotal": 65.0, "grand_total": 65.0})
        merged = ai_extract.merge_pages([first, second])
        self.assertEqual(merged[0]["totals_review"], {})

    def test_a_single_page_document_is_left_exactly_as_it_was(self):
        only = page(1)
        merged = ai_extract.merge_pages([only])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["pages"], [1])


class PageTextTests(unittest.TestCase):
    def test_the_loose_page_text_never_reaches_the_document(self):
        document, _blocking, _advisory = ai_extract.validate(
            payload(notes=["YOUR LOGO", "Shipping Manifest"]), set()
        )
        self.assertNotIn("notes", document)

    def test_a_page_of_fields_alone_is_not_treated_as_an_empty_read(self):
        _document, blocking, _advisory = ai_extract.validate(
            payload(items=[], totals={}, header={"consignee": "Northwind"}), set()
        )
        self.assertFalse(any("returned no items" in message for message in blocking))


if __name__ == "__main__":
    unittest.main()
