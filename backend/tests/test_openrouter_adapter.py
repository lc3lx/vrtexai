"""The adapter is the whole risk of switching readers.

Everything downstream — ``paddle_vl.to_payload``, the role resolver, the three
gates — was written against PaddleOCR-VL's block list. A hosted chat model
answers with HTML instead, so these pin that the rebuilt blocks are accepted by
the existing converter unchanged. If this file passes, no other module needs to
know the reader changed.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
WORKER = ROOT.parents[1] / "ExcelCleaner" / "ocr_worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from app.services.ai_provider import html_to_blocks  # noqa: E402

# Shaped after the real export invoice: header text, then a priced grid.
INVOICE_HTML = """<p>EXPORT INVOICE GOH258</p>
<p>M/S HOME DECOR — Invoice No. EXP-03/2026/27 — Date 30-05-2025</p>
<table>
  <tr><td>HS Code</td><td>Description</td><td>Ship Quantity</td><td>Rate Fob</td><td>Amount</td></tr>
  <tr><td>44219990</td><td>19" INCH BEAD NOEL</td><td>36</td><td>3.15</td><td>113.40</td></tr>
  <tr><td>44219990</td><td>24" INCH WOOD WHITE WASH</td><td>36</td><td>3.50</td><td>126.00</td></tr>
  <tr><td>44219990</td><td>12" INCH WHITE WASH JOY</td><td>390</td><td>2.45</td><td>955.50</td></tr>
</table>
<p>TOTAL INVOICE VALUE FOB (USD) 10,953.00</p>"""


class BlockRebuildTests(unittest.TestCase):
    def test_tables_and_text_are_separated_in_reading_order(self):
        blocks = html_to_blocks(INVOICE_HTML)["parsing_res_list"]
        labels = [block["block_label"] for block in blocks]
        self.assertEqual(labels.count("table"), 1)
        # Text before the grid must stay before it: the header fields are read
        # positionally and a reordered page loses the invoice number.
        self.assertEqual(labels[0], "text")
        self.assertIn("GOH258", blocks[0]["block_content"])
        self.assertEqual(labels[-1], "text")
        self.assertIn("10,953.00", blocks[-1]["block_content"])

    def test_the_table_block_keeps_its_html(self):
        blocks = html_to_blocks(INVOICE_HTML)["parsing_res_list"]
        table = next(b for b in blocks if b["block_label"] == "table")
        self.assertTrue(table["block_content"].lstrip().startswith("<table"))
        self.assertIn("44219990", table["block_content"])

    def test_markdown_fences_are_stripped(self):
        blocks = html_to_blocks("```html\n<p>Invoice 7</p>\n```")["parsing_res_list"]
        self.assertEqual(blocks[0]["block_content"], "Invoice 7")

    def test_a_page_with_no_table_still_yields_text(self):
        blocks = html_to_blocks("<p>Handwritten note</p>")["parsing_res_list"]
        self.assertEqual(blocks[0]["block_label"], "text")

    def test_an_empty_answer_produces_no_blocks(self):
        self.assertEqual(html_to_blocks("")["parsing_res_list"], [])


class ExistingConverterAcceptsThemTests(unittest.TestCase):
    """The contract: the untouched desktop converter must read these blocks."""

    def setUp(self):
        import paddle_vl

        self.payload = paddle_vl.to_payload(
            {"result": html_to_blocks(INVOICE_HTML), "markdown": INVOICE_HTML}
        )

    def test_the_item_grid_is_found(self):
        self.assertEqual(len(self.payload["items"]), 3)

    def test_roles_come_from_arithmetic_not_from_the_reader(self):
        # 36 x 3.15 = 113.40 holds, so the resolver can name the columns itself.
        # This is what survives a change of reader: the model never says which
        # column is a price, the multiplication does.
        roles = self.payload["column_roles"]
        self.assertIn("qty", roles)
        self.assertIn("unit_price", roles)
        self.assertIn("line_total", roles)

    def test_numeric_cells_arrive_as_numbers(self):
        first = self.payload["items"][0]
        self.assertEqual(first["qty"], 36.0)
        self.assertEqual(first["unit_price"], 3.15)
        self.assertEqual(first["line_total"], 113.40)

    def test_header_text_is_kept_for_field_extraction(self):
        self.assertTrue(any("GOH258" in note for note in self.payload["notes"]))

    def test_the_gates_accept_a_faithful_transcription(self):
        import ai_extract

        _document, blocking, _advisory = ai_extract.validate(self.payload, set())
        self.assertEqual(blocking, [])

    def test_the_gates_still_catch_an_invented_total(self):
        # The reason this whole design survives swapping readers: a model that
        # "helpfully" computed 36 x 3.15 as 999.00 is caught by arithmetic, not
        # by trusting whichever model produced it.
        bad = INVOICE_HTML.replace("<td>113.40</td>", "<td>999.00</td>")
        import ai_extract
        import paddle_vl

        payload = paddle_vl.to_payload({"result": html_to_blocks(bad), "markdown": bad})
        _document, blocking, _advisory = ai_extract.validate(payload, set())
        self.assertTrue(blocking, "an invented line total passed the arithmetic gate")


class ErrorClassificationTests(unittest.TestCase):
    """A passing failure must fall back; a permanent one must not.

    This distinction cost a real job: OpenRouter answered HTTP 200 with a 429
    inside the body, the code read only the status, and a rate limit was
    reported to the customer as a failed document instead of quietly falling
    back to the local reader.
    """

    def classify(self, error):
        from app.services.ai_provider import _classify_openrouter_error

        return _classify_openrouter_error(error)

    def test_upstream_rate_limit_falls_back(self):
        from app.services.ai_provider import AIUnavailable

        error = self.classify({"message": "Provider returned error", "code": 429})
        self.assertIsInstance(error, AIUnavailable)

    def test_out_of_credit_falls_back(self):
        from app.services.ai_provider import AIUnavailable

        self.assertIsInstance(self.classify({"message": "Insufficient credits", "code": 402}),
                              AIUnavailable)

    def test_upstream_server_error_falls_back(self):
        from app.services.ai_provider import AIUnavailable

        self.assertIsInstance(self.classify({"message": "bad gateway", "code": 502}),
                              AIUnavailable)

    def test_a_rejected_request_does_not_fall_back(self):
        # Retrying a malformed request elsewhere fails twice and takes twice as
        # long; it needs a person, not another model.
        from app.services.ai_provider import AIFailed

        self.assertIsInstance(self.classify({"message": "invalid image", "code": 400}),
                              AIFailed)

    def test_the_upstream_reason_survives_into_the_message(self):
        error = self.classify({
            "message": "Provider returned error", "code": 429,
            "metadata": {"raw": "gemma is temporarily rate-limited upstream"},
        })
        self.assertIn("rate-limited upstream", str(error))

    def test_a_bare_string_error_is_handled(self):
        from app.services.ai_provider import AIFailed

        self.assertIsInstance(self.classify("something went wrong"), AIFailed)


if __name__ == "__main__":
    unittest.main()
