"""Regression tests for the geometric understanding pipeline.

These cover the decisions that were previously getting documents wrong, using
synthetic word boxes so they run without models or images:

* the dual-recognizer choice that used to lose whole numeric columns,
* column detection through a boundary narrower than a word space,
* arithmetic repair from a competing reading rather than from confidence,
* refusing to report errors when the assumed column roles do not hold.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import layout
import verify
from perceive import _choose


def word(text, x0, y0, x1, y1, conf=95.0, alternatives=None):
    return {
        "text": text, "x0": float(x0), "y0": float(y0), "x1": float(x1), "y1": float(y1),
        "conf": conf, "script": "latin", "alternatives": alternatives or [],
    }


class RecognizerChoiceTests(unittest.TestCase):
    def test_numeric_english_beats_confident_arabic(self):
        # The exact readings that made a quantity column disappear: the Arabic
        # model returns a confident Arabic word for a lone digit.
        text, _conf, script = _choose(("3", 0.22), ("يم", 0.81))
        self.assertEqual(text, "3")
        self.assertEqual(script, "latin")

    def test_real_arabic_is_kept(self):
        text, _conf, script = _choose(("Jlsi", 0.44), ("المسافر", 0.98))
        self.assertEqual(text, "المسافر")
        self.assertEqual(script, "ar")

    def test_arabic_indic_digits_are_not_evidence_of_arabic_text(self):
        # "No." misread as "٨٥" by the Arabic model must not win over "No.".
        text, _conf, _script = _choose(("No.", 0.72), ("٨٥", 0.84))
        self.assertEqual(text, "No.")

    def test_english_wins_latin_ties(self):
        text, _conf, _script = _choose(("Customer", 0.91), ("Customier", 0.93))
        self.assertEqual(text, "Customer")


class ColumnDetectionTests(unittest.TestCase):
    def test_boundary_narrower_than_a_word_space(self):
        """A 12px column gap separates columns even though it is very narrow.

        On the sample invoice "Description" and "Quantity" are closer together
        than a word space is wide. No single line reveals the boundary; only
        the sliver of white running down every row does.
        """
        words = []
        for index, top in enumerate((0, 40, 80, 120)):
            words += [
                word(f"Item {index}", 0, top, 100, top + 24),
                word("10", 112, top, 150, top + 24),
                # The detector returns a whole phrase as one box, so an internal
                # space never reaches this stage as a gap.
                word("$10.00", 200, top, 310, top + 24),
            ]
        document = layout.build_document(words)
        table = next(r for r in document["regions"] if len(r["rows"][0]) > 1)
        self.assertEqual(len(table["rows"][0]), 3)
        self.assertEqual([cell["text"] for cell in table["rows"][0]],
                         ["Item 0", "10", "$10.00"])

    def test_totals_block_is_not_swallowed_by_the_table_above_it(self):
        words = []
        for top in (0, 40, 80):
            words += [
                word("Item", 0, top, 90, top + 24),
                word("2", 200, top, 220, top + 24),
                word("$20.00", 300, top, 380, top + 24),
            ]
        words += [
            word("Subtotal", 100, 200, 190, 224),
            word("$60.00", 300, 200, 380, 224),
        ]
        document = layout.build_document(words)
        self.assertGreaterEqual(len(document["regions"]), 2)
        last = document["regions"][-1]
        self.assertEqual(last["rows"][0][0]["text"], "Subtotal")

    def test_empty_columns_are_dropped(self):
        words = [word("A", 0, 0, 30, 20), word("B", 300, 0, 330, 20)]
        document = layout.build_document(words)
        self.assertTrue(all(
            any(cell["text"].strip() for cell in row)
            for region in document["regions"] for row in region["rows"]
        ))


class HeaderDetectionTests(unittest.TestCase):
    def test_row_of_times_is_not_promoted_to_a_header(self):
        # Promoting it would delete the row and mislabel every row below.
        row = [
            {"text": "14:20"}, {"text": "17:15"}, {"text": "15:50"},
        ]
        self.assertFalse(layout._looks_like_header(row, remaining=3))

    def test_real_header_is_promoted(self):
        row = [{"text": "Description"}, {"text": "Quantity"}, {"text": "Unit Price"}]
        self.assertTrue(layout._looks_like_header(row, remaining=4))

    def test_header_needs_rows_beneath_it(self):
        row = [{"text": "Description"}, {"text": "Quantity"}]
        self.assertFalse(layout._looks_like_header(row, remaining=0))


class NumberParsingTests(unittest.TestCase):
    def test_formats_seen_in_the_sample_documents(self):
        self.assertEqual(verify.to_number("$1,234.56"), 1234.56)
        self.assertEqual(verify.to_number("١٢٣٤"), 1234.0)      # Arabic-Indic
        self.assertEqual(verify.to_number("١٢٫٥"), 12.5)        # Arabic decimal
        self.assertEqual(verify.to_number("6.90 SAR"), 6.90)
        self.assertEqual(verify.to_number("-5.00"), -5.0)
        self.assertIsNone(verify.to_number("abc"))
        self.assertIsNone(verify.to_number(""))


class ArithmeticTests(unittest.TestCase):
    def _table(self, quantity, alternatives):
        return {
            "kind": "table",
            "columns": ["Description", "Quantity", "Unit Price", "Total"],
            "rows": [[
                {"text": "Item 4", "conf": 90.0, "alternatives": []},
                {"text": quantity, "conf": 36.0, "alternatives": alternatives},
                {"text": "$12.00", "conf": 99.0, "alternatives": []},
                {"text": "$36.00", "conf": 99.0, "alternatives": []},
            ]],
        }

    def test_repair_uses_a_competing_reading_not_the_confident_one(self):
        document = {"regions": [self._table("13", [{"text": "3", "conf": 22.0}])]}
        verify.verify(document)
        cell = document["regions"][0]["rows"][0][1]
        self.assertEqual(cell["text"], "3")
        self.assertTrue(cell["review"])
        self.assertIn("صُحّح", cell["note"])

    def test_no_reading_reconciles_so_nothing_is_invented(self):
        document = {"regions": [self._table("13", [{"text": "17", "conf": 20.0}])]}
        verify.verify(document)
        cell = document["regions"][0]["rows"][0][1]
        self.assertEqual(cell["text"], "13")
        self.assertTrue(cell.get("review"))

    def test_silent_when_the_assumed_column_roles_do_not_hold(self):
        """A spreadsheet with many numeric columns must not report every row.

        Guessing three columns at random and flagging every row that fails is
        worse than saying nothing.
        """
        rows = [[
            {"text": "28-Oct-2022", "conf": 99.0, "alternatives": []},
            {"text": "Pencil", "conf": 99.0, "alternatives": []},
            {"text": "0.21", "conf": 99.0, "alternatives": []},
            {"text": "6.81", "conf": 99.0, "alternatives": []},
            {"text": "0.68", "conf": 99.0, "alternatives": []},
            {"text": "7.49", "conf": 99.0, "alternatives": []},
        ] for _ in range(6)]
        document = {"regions": [{"kind": "table", "columns": [], "rows": rows}]}
        notes = verify.verify(document)
        self.assertEqual(notes, [])
        self.assertFalse(any(cell.get("review") for row in rows for cell in row))

    def test_discovers_the_relation_a_table_actually_satisfies(self):
        rows = [[
            {"text": "Pencil", "conf": 99.0, "alternatives": []},
            {"text": "0.27", "conf": 99.0, "alternatives": []},
            {"text": "26", "conf": 99.0, "alternatives": []},
            {"text": "7.02", "conf": 99.0, "alternatives": []},
        ]] * 1
        roles = verify.resolve_roles([], rows)
        self.assertEqual(roles.get("line_total"), 3)
        self.assertEqual(roles.get("agreement"), 1.0)

    def test_a_totals_row_inside_the_table_is_not_counted_as_a_line(self):
        """A "Subtotal $350" row grouped with the items must not be summed.

        It used to be, making the line total come to 700 against a subtotal of
        350 and producing a confident, wrong warning on a correct invoice.
        """
        def cell(text):
            return {"text": text, "conf": 99.0, "alternatives": []}

        rows = [
            [cell("1"), cell("Goods"), cell("2"), cell("100.00"), cell("200.00")],
            [cell("2"), cell("Goods"), cell("1"), cell("150.00"), cell("150.00")],
            # Same columns, but no quantity and no unit price: a totals row.
            [cell(""), cell("Subtotal:"), cell(""), cell(""), cell("350.00")],
        ]
        document = {"regions": [
            {"kind": "table",
             "columns": ["Item #", "Description", "Quantity", "Unit Price", "Total"],
             "rows": rows},
        ]}
        notes = verify.verify(document)
        self.assertIn("مجموع البنود يطابق المجموع الفرعي", notes)

    def test_an_advance_between_subtotal_and_total_reconciles(self):
        def row(label, amount):
            return [{"text": label, "conf": 99.0, "alternatives": []},
                    {"text": amount, "conf": 99.0, "alternatives": []}]

        document = {"regions": [{"kind": "key_value", "columns": [], "rows": [
            row("Subtotal:", "$350.00"),
            row("Advance:", "-$50.00"),
            row("Tax (10%):", "$35.00"),
            row("Amount Due:", "$335.00"),
        ]}]}
        verify.verify(document)
        self.assertFalse(any(
            cell.get("review")
            for r in document["regions"][0]["rows"] for cell in r
        ))

    def test_totals_cross_check_flags_a_mismatch(self):
        document = {"regions": [
            {"kind": "table",
             "columns": ["Description", "Quantity", "Unit Price", "Total"],
             "rows": [[
                 {"text": "Item", "conf": 99.0, "alternatives": []},
                 {"text": "2", "conf": 99.0, "alternatives": []},
                 {"text": "10.00", "conf": 99.0, "alternatives": []},
                 {"text": "20.00", "conf": 99.0, "alternatives": []},
             ]]},
            {"kind": "key_value", "columns": [], "rows": [[
                {"text": "Subtotal", "conf": 99.0, "alternatives": []},
                {"text": "99.00", "conf": 99.0, "alternatives": []},
            ]]},
        ]}
        verify.verify(document)
        self.assertTrue(document["regions"][1]["rows"][0][1]["review"])


if __name__ == "__main__":
    unittest.main()
