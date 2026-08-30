"""Tests for reading a printed table without a template.

Each shape here is taken from a real document in ``data/``, because the failures
being guarded against were all "the headings are right and the values under them
are wrong" — which no synthetic four-column invoice ever reproduces. The three
that mattered:

* a Saudi thermal receipt whose totals are rows of the item grid with the label
  merged across three columns;
* an inventory sheet with two decoy "Quantity" columns and a "Product ID" that
  used to steal the description role from "Product Name";
* an item grid split into two tables, which used to lose half its columns.

Everything takes plain strings, so none of it needs a model or an image.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import table_shape as ts

# data/5785379544410820536_121.jpg — the totals are rows of the item table, with
# the label merged across the description, quantity and price columns.
RECEIPT = """<table>
<tr><td>Item</td><td>Qty</td><td>Price</td><td>Total</td></tr>
<tr><td>SAKAR GAS STONE 1,</td><td>10</td><td>34.78</td><td>347.83</td></tr>
<tr><td>JILBA.JAKAR 1PPR</td><td>9</td><td>19.13</td><td>172.17</td></tr>
<tr><td>NIPAL HADID 3/4</td><td>1</td><td>4.35</td><td>4.35</td></tr>
<tr><td colspan="3">Total</td><td>820.00</td></tr>
<tr><td colspan="3">VAT 15%</td><td>123.00</td></tr>
<tr><td colspan="3">Due</td><td>943.00</td></tr>
<tr><td colspan="3">Paid</td><td>943.00</td></tr>
</table>"""

# data/5787630987742220188_121.jpg — nine columns, and the quantity that
# multiplies the rate is the third one called "Quantity".
INVENTORY = """<table>
<tr><td>Product ID</td><td>Product Name</td><td>Inward Quantity</td><td>Outward Quantity</td>
    <td>Quantity In Stock</td><td>Rate</td><td>Amount</td><td>Reorder Level</td><td>Status</td></tr>
<tr><td>MSG001</td><td>Item 1</td><td>150</td><td>125</td><td>25</td><td>1,500.00</td><td>37,500.00</td><td>25</td><td>Reorder</td></tr>
<tr><td>MSG002</td><td>Item 2</td><td>100</td><td>100</td><td>0</td><td>1,100.00</td><td>0.00</td><td>10</td><td>Out of Stock</td></tr>
<tr><td>MSG003</td><td>Item 3</td><td>200</td><td>100</td><td>100</td><td>1,550.00</td><td>155,000.00</td><td>50</td><td>Out of Stock</td></tr>
<tr><td>MSG004</td><td>Item 4</td><td>150</td><td>125</td><td>25</td><td>1,000.00</td><td>25,000.00</td><td>25</td><td>Reorder</td></tr>
</table>"""


def rows_of(grid: ts.Grid) -> list[list[str]]:
    return grid.text_rows()


class MergedCellTests(unittest.TestCase):
    """A merged cell used to slide every value after it one column left."""

    def test_a_column_span_keeps_the_later_values_under_their_headings(self):
        grid = ts.parse_html_table(RECEIPT)
        self.assertEqual(grid.width, 4)
        for row in grid.cells:
            self.assertEqual(len(row), 4)
        # The amount stays in the fourth column, where "Total" is printed.
        self.assertEqual(grid.cells[4][3].text, "820.00")

    def test_a_column_span_puts_its_text_in_one_cell_only(self):
        # Copying the label sideways would make a totals row look like a row of
        # three descriptions.
        grid = ts.parse_html_table(RECEIPT)
        self.assertEqual([cell.text for cell in grid.cells[4]], ["Total", "", "", "820.00"])

    def test_a_row_span_repeats_its_text_down_the_rows_it_covers(self):
        # The other direction means the opposite: a description merged over two
        # rows describes both, and the second row would otherwise have none.
        grid = ts.parse_html_table("""<table>
        <tr><td rowspan="2">Steel bracket</td><td>2</td><td>15.50</td></tr>
        <tr><td>3</td><td>10.00</td></tr>
        </table>""")
        self.assertEqual(rows_of(grid), [
            ["Steel bracket", "2", "15.50"],
            ["Steel bracket", "3", "10.00"],
        ])

    def test_a_short_row_is_padded_rather_than_shifted(self):
        grid = ts.parse_html_table(
            "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>1</td></tr></table>"
        )
        self.assertEqual(rows_of(grid), [["a", "b", "c"], ["1", "", ""]])

    def test_cells_left_unclosed_are_still_read(self):
        # Transcribing models drop the closing tag; the page is still readable.
        grid = ts.parse_html_table("<table><tr><td>a<td>b<td>c</tr></table>")
        self.assertEqual(rows_of(grid), [["a", "b", "c"]])

    def test_html_entities_are_decoded(self):
        grid = ts.parse_html_table(
            "<table><tr><td>Handbags &amp; Leather</td><td>Ladies&#x27; Garments</td></tr></table>"
        )
        self.assertEqual(rows_of(grid), [["Handbags & Leather", "Ladies' Garments"]])


class HeaderTests(unittest.TestCase):
    def test_a_two_line_heading_is_read_as_one_name(self):
        grid = ts.parse_html_table("""<table>
        <tr><th>Item</th><th>Inward</th><th>Unit</th><th>Total</th></tr>
        <tr><th></th><th>Quantity</th><th>Price</th><th>Value</th></tr>
        <tr><td>Bracket</td><td>2</td><td>15.50</td><td>31.00</td></tr>
        </table>""")
        headings, body = ts.split_header(grid)
        self.assertEqual(headings, ["Item", "Inward Quantity", "Unit Price", "Total Value"])
        self.assertEqual(len(body), 1)

    def test_a_group_heading_reaches_the_columns_it_covers(self):
        grid = ts.parse_html_table("""<table>
        <tr><th>Item</th><th colspan="2">Charges</th></tr>
        <tr><th></th><th>Freight</th><th>Insurance</th></tr>
        <tr><td>Crate</td><td>10.00</td><td>2.00</td></tr>
        </table>""")
        headings, _body = ts.split_header(grid)
        self.assertEqual(headings, ["Item", "Charges Freight", "Charges Insurance"])

    def test_two_columns_with_the_same_name_stay_two_columns(self):
        grid = ts.parse_html_table(
            "<table><tr><td>Amount</td><td>Amount</td></tr><tr><td>1</td><td>2</td></tr></table>"
        )
        headings, _body = ts.split_header(grid)
        self.assertEqual(headings, ["Amount", "Amount (2)"])

    def test_a_caption_above_the_grid_is_not_eaten_as_column_names(self):
        grid = ts.parse_html_table(
            "<table><tr><td>البنود</td></tr>"
            "<tr><td>قلم</td><td>2</td></tr></table>"
        )
        headings, body = ts.split_header(grid)
        self.assertEqual(headings, [])
        self.assertEqual(len(body), 2)

    def test_a_table_with_no_heading_row_keeps_all_its_rows(self):
        grid = ts.parse_html_table(
            "<table><tr><td>قلم</td><td>2</td><td>15.50</td></tr>"
            "<tr><td>دفتر</td><td>3</td><td>10.00</td></tr></table>"
        )
        headings, body = ts.split_header(grid)
        self.assertEqual(headings, [])
        self.assertEqual(len(body), 2)


class RowKindTests(unittest.TestCase):
    """A totals line inside the item grid is not a product."""

    def test_a_merged_totals_row_is_recognised(self):
        grid = ts.parse_html_table(RECEIPT)
        _headings, body = ts.split_header(grid)
        kinds = [ts.classify_row(row)[0] for row in body]
        self.assertEqual(kinds, [ts.ITEM] * 3 + [ts.TOTAL] * 4)

    def test_an_item_row_is_never_mistaken_for_a_total(self):
        row = ts.parse_html_table(
            "<table><tr><td>Total Station Tripod</td><td>2</td><td>15.50</td><td>31.00</td></tr></table>"
        ).cells[0]
        self.assertEqual(ts.classify_row(row)[0], ts.ITEM)

    def test_a_label_the_totals_vocabulary_does_not_claim_stays_an_item(self):
        # Two filled cells and one amount, but "Net Weight" is not a total.
        row = ts.parse_html_table(
            "<table><tr><td>Net Weight</td><td></td><td></td><td>12.40</td></tr></table>"
        ).cells[0]
        self.assertEqual(ts.classify_row(row)[0], ts.ITEM)

    def test_a_single_spanning_cell_is_a_section_heading(self):
        row = ts.parse_html_table(
            '<table><tr><td colspan="4">Electrical goods</td></tr></table>'
        ).cells[0]
        self.assertEqual(ts.classify_row(row)[0], ts.SECTION)


class TotalsTests(unittest.TestCase):
    def test_the_arithmetic_decides_which_printed_total_is_the_total(self):
        """"Total 820" then "VAT 123" then "Due 943".

        Both "Total" and "Due" name the grand total, and whichever was read last
        used to win by accident. 820 + 123 = 943 settles it.
        """
        grid = ts.parse_html_table(RECEIPT)
        _headings, body = ts.split_header(grid)
        stated = [(label, amount) for kind, label, amount in map(ts.classify_row, body)
                  if kind == ts.TOTAL]
        totals = ts.reconcile_totals(stated)
        self.assertEqual(totals["subtotal"], 820.00)
        self.assertEqual(totals["tax_amount"], 123.00)
        self.assertEqual(totals["grand_total"], 943.00)
        self.assertEqual(totals["amount_paid"], 943.00)

    def test_the_last_total_printed_wins_when_nothing_reconciles(self):
        totals = ts.reconcile_totals([("Subtotal", 100.0), ("Grand Total", 115.0)])
        self.assertEqual(totals["subtotal"], 100.0)
        self.assertEqual(totals["grand_total"], 115.0)

    def test_a_totals_box_of_its_own_is_not_read_as_an_item_table(self):
        box = ts.parse_html_table(
            "<table><tr><td>Subtotal</td><td>100.00</td></tr>"
            "<tr><td>VAT</td><td>15.00</td></tr>"
            "<tr><td>Total</td><td>115.00</td></tr></table>"
        )
        self.assertTrue(ts.is_totals_grid(box))
        items = ts.parse_html_table(RECEIPT)
        self.assertFalse(ts.is_totals_grid(items))


class AssembleTests(unittest.TestCase):
    def test_a_split_item_grid_is_put_back_together(self):
        """The failure that lost Price and Total from a customer's workbook.

        A layout detector splits a long grid, the old reader kept the larger
        half, and the rest was scattered into single cells and discarded.
        """
        first = ts.parse_html_table(
            "<table><tr><td>Item</td><td>Qty</td><td>Price</td><td>Total</td></tr>"
            "<tr><td>قلم</td><td>2</td><td>15.50</td><td>31.00</td></tr>"
            "<tr><td>دفتر</td><td>3</td><td>10.00</td><td>30.00</td></tr></table>"
        )
        second = ts.parse_html_table(
            "<table><tr><td>ممحاة</td><td>10</td><td>1.25</td><td>12.50</td></tr></table>"
        )
        items, _totals, others = ts.assemble([first, second])
        self.assertEqual(items.height, 4)
        self.assertEqual(items.cells[3][0].text, "ممحاة")
        self.assertEqual(others, [])

    def test_a_continuation_repeating_its_header_does_not_repeat_the_header_row(self):
        header = "<tr><td>Item</td><td>Qty</td></tr>"
        first = ts.parse_html_table(
            f"<table>{header}<tr><td>قلم</td><td>2</td></tr>"
            "<tr><td>دفتر</td><td>3</td></tr></table>"
        )
        second = ts.parse_html_table(f"<table>{header}<tr><td>ممحاة</td><td>10</td></tr></table>")
        items, _totals, _others = ts.assemble([first, second])
        self.assertEqual([row[0].text for row in items.cells],
                         ["Item", "قلم", "دفتر", "ممحاة"])

    def test_a_table_of_different_width_is_kept_apart(self):
        first = ts.parse_html_table(RECEIPT)
        second = ts.parse_html_table(
            "<table><tr><td>الرقم الضريبي</td><td>300000</td></tr></table>"
        )
        items, _totals, others = ts.assemble([first, second])
        self.assertEqual(items.width, 4)
        self.assertEqual(len(others), 1)

    def test_a_totals_box_is_routed_to_the_totals_not_the_items(self):
        items_html = ts.parse_html_table(
            "<table><tr><td>Item</td><td>Qty</td><td>Price</td><td>Total</td></tr>"
            "<tr><td>قلم</td><td>2</td><td>15.50</td><td>31.00</td></tr></table>"
        )
        box = ts.parse_html_table(
            "<table><tr><td>Subtotal</td><td>31.00</td></tr>"
            "<tr><td>Total</td><td>31.00</td></tr></table>"
        )
        items, totals, _others = ts.assemble([items_html, box])
        self.assertEqual(items.height, 2)
        self.assertEqual(len(totals), 1)
        self.assertEqual(ts.read_totals(totals), [("Subtotal", 31.0), ("Total", 31.0)])


class RoleTests(unittest.TestCase):
    """The heading, the content and the arithmetic, weighed against each other."""

    def _roles(self, html, **kwargs):
        grid = ts.parse_html_table(html)
        headings, body = ts.split_header(grid)
        rows = [[cell.text for cell in row] for row in body
                if ts.classify_row(row)[0] == ts.ITEM]
        found = ts.assign_roles(headings, rows, **kwargs)
        return {role: headings[index] for role, index in found.columns.items()}, found

    def test_the_description_is_the_name_not_the_identifier(self):
        # "Product ID" used to take the description role from "Product Name",
        # because the word "product" appeared in it first.
        roles, _found = self._roles(INVENTORY)
        self.assertEqual(roles["description"], "Product Name")
        self.assertEqual(roles["sku"], "Product ID")

    def test_the_quantity_is_the_one_that_actually_multiplies(self):
        # Three columns are called "Quantity". Only one of them times the rate
        # gives the amount, and that is the one the sheet needs.
        roles, found = self._roles(INVENTORY)
        self.assertEqual(roles["qty"], "Quantity In Stock")
        self.assertEqual(roles["unit_price"], "Rate")
        self.assertEqual(roles["line_total"], "Amount")
        self.assertEqual(found.agreement, 1.0)

    def test_a_column_of_identifiers_is_never_a_quantity(self):
        roles, _found = self._roles(
            "<table><tr><td>Description</td><td>AWB Number</td><td>Qty</td><td>Rate</td><td>Amount</td></tr>"
            "<tr><td>Crate</td><td>102441700123</td><td>2</td><td>15.50</td><td>31.00</td></tr>"
            "<tr><td>Pallet</td><td>102441700124</td><td>4</td><td>10.00</td><td>40.00</td></tr></table>"
        )
        self.assertEqual(roles["qty"], "Qty")
        # It may be recognised as an identifier — which is what keeps Excel from
        # turning it into 1.02442E+11 — but never as a figure to calculate with.
        for role in ("qty", "unit_price", "line_total"):
            self.assertNotEqual(roles.get(role), "AWB Number")

    def test_an_arabic_table_resolves_the_same_way(self):
        roles, _found = self._roles(
            "<table><tr><td>البيان</td><td>الكمية</td><td>سعر الوحدة</td><td>إجمالي السعر</td></tr>"
            "<tr><td>قلم</td><td>2</td><td>15.50</td><td>31.00</td></tr>"
            "<tr><td>دفتر</td><td>3</td><td>10.00</td><td>30.00</td></tr></table>"
        )
        self.assertEqual(roles["description"], "البيان")
        self.assertEqual(roles["qty"], "الكمية")
        self.assertEqual(roles["unit_price"], "سعر الوحدة")
        # "إجمالي السعر" has no definite article, which the old pattern needed —
        # so it was read as a unit price and the real total went unnamed.
        self.assertEqual(roles["line_total"], "إجمالي السعر")

    def test_a_services_invoice_with_nothing_to_multiply_still_finds_its_total(self):
        # No quantity, no unit price. The column is the line total because it
        # adds up to the subtotal printed on the page.
        roles, _found = self._roles(
            "<table><tr><td>Description</td><td>Amount</td></tr>"
            "<tr><td>Consultancy, March</td><td>1,200.00</td></tr>"
            "<tr><td>Site visit</td><td>300.00</td></tr></table>",
            totals={"subtotal": 1500.0},
        )
        self.assertEqual(roles["line_total"], "Amount")

    def test_unrelated_numeric_columns_get_no_invented_roles(self):
        # Nine numeric columns that relate to nothing must resolve to nothing,
        # rather than three of them being wired together at random.
        found = ts.assign_roles([], [["28-Oct-2022", "Pencil", "0.21", "6.81", "0.68", "7.49"]] * 6)
        self.assertNotIn("qty", found.columns)
        self.assertNotIn("line_total", found.columns)

    def test_headings_alone_are_enough_when_the_arithmetic_is_misread(self):
        # A column headed "Quantity" is still the quantity even when the scan
        # got the digits wrong — that is exactly what needs repairing.
        found = ts.assign_roles(
            ["Description", "Quantity", "Unit Price", "Amount"],
            [["Bracket", "2", "15.50", "99.00"], ["Plate", "3", "10.00", "88.00"]],
        )
        self.assertEqual(found.columns["qty"], 1)
        self.assertEqual(found.columns["line_total"], 3)
        self.assertLess(found.agreement, 0.5)

    def test_the_description_is_found_from_content_when_nothing_is_printed_above(self):
        found = ts.assign_roles([], [
            ["Steel bracket 40mm", "2", "15.50", "31.00"],
            ["Galvanised plate", "3", "10.00", "30.00"],
        ])
        self.assertEqual(found.columns["description"], 0)


class NumberTests(unittest.TestCase):
    def test_a_description_ending_in_a_digit_is_not_a_number(self):
        # "SAKAR GAS STONE 1," made the description column count as numeric,
        # which cost it the description role and gave it a numeric one.
        self.assertIsNone(ts.numeric_cell("SAKAR GAS STONE 1,"))
        self.assertIsNone(ts.numeric_cell("NIPAL HADID 3/4"))

    def test_a_plain_amount_is_a_number(self):
        self.assertEqual(ts.numeric_cell("1,500.00"), 1500.0)
        self.assertEqual(ts.numeric_cell("$37,500.00"), 37500.0)
        self.assertEqual(ts.numeric_cell("(25)"), 25.0)
        self.assertEqual(ts.numeric_cell("١٢٣"), 123.0)

    def test_a_date_is_not_an_amount(self):
        self.assertIsNone(ts.numeric_cell("2025/11/10"))
        self.assertIsNone(ts.numeric_cell("28-Oct-2022"))


if __name__ == "__main__":
    unittest.main()
