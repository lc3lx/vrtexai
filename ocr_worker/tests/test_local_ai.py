"""Regression coverage for the embedded coloured-grid document analyser."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLE = ROOT.parent.parent / "data" / "4.png"
LOW_RES_INVOICE_SAMPLE = ROOT.parent.parent / "data" / "1.png"
FORM_SAMPLE = ROOT.parent.parent / "data" / "2.png"
BILINGUAL_INVOICE_SAMPLE = ROOT.parent.parent / "data" / "3.png"
SPREADSHEET_SAMPLE = ROOT.parent.parent / "data" / "5.png"
DIRECTORY_SAMPLE = ROOT.parent.parent / "data" / "6.png"
BORDERLESS_INVOICE_SAMPLE = ROOT.parent.parent / "data" / "8.png"
TESSERACT = ROOT.parent / "runtime" / "tesseract" / "tesseract.exe"


class LocalAIGridTests(unittest.TestCase):
    def test_safety_gate_rejects_partial_invoice_schema(self):
        from local_ai import LocalAIResult, _local_result_is_safe

        result = LocalAIResult(
            table=[
                ["", "", "الكمية", "", "المنتج"],
                ["171", "2", "", "MacBook", "Apple MacBook"],
            ],
            scores=[[90.0] * 5, [90.0] * 5],
        )
        self.assertFalse(_local_result_is_safe(result))

    def test_safety_gate_rejects_lost_decimal_invoice_total(self):
        from local_ai import LocalAIResult, _local_result_is_safe

        result = LocalAIResult(
            table=[
                ["Description", "Qty", "Rate", "Total"],
                ["Fridge 210", "12", "4998.00", "629480"],
            ],
            scores=[[90.0] * 4, [90.0] * 4],
        )
        self.assertFalse(_local_result_is_safe(result))

    def test_safety_gate_keeps_arithmetically_supported_invoice(self):
        from local_ai import LocalAIResult, _local_result_is_safe

        result = LocalAIResult(
            table=[
                ["Description", "Qty", "Rate", "Total"],
                ["Service A", "2", "10.00", "20.00"],
            ],
            scores=[[90.0] * 4, [90.0] * 4],
        )
        self.assertTrue(_local_result_is_safe(result))

    @unittest.skipUnless(LOW_RES_INVOICE_SAMPLE.exists() and TESSERACT.exists(), "Bundled OCR runtime is not available.")
    def test_low_resolution_invoice_keeps_real_columns_without_fake_invoice_fields(self):
        from PIL import Image
        import numpy as np
        from invoice import invoice_table_is_reliable
        from local_ai import analyze_image
        from ocr import setup_tesseract

        previous = os.environ.get("TESSERACT_CMD")
        os.environ["TESSERACT_CMD"] = str(TESSERACT)
        try:
            image = np.array(Image.open(LOW_RES_INVOICE_SAMPLE).convert("RGB"))
            result = analyze_image(image, setup_tesseract(), include_page_text=True)
        finally:
            if previous is None:
                os.environ.pop("TESSERACT_CMD", None)
            else:
                os.environ["TESSERACT_CMD"] = previous

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.kind, "generic_table")
        self.assertTrue(result.method.startswith("local-ai-generic-table:"))
        self.assertEqual(result.table[0], ["Total", "Unit Price", "Qty", "Description", "Product"])
        self.assertFalse(invoice_table_is_reliable(result.table))

    @unittest.skipUnless(FORM_SAMPLE.exists() and TESSERACT.exists(), "Bundled OCR runtime is not available.")
    def test_ruled_form_is_exported_as_field_value_pairs(self):
        from PIL import Image
        import numpy as np
        from local_ai import analyze_image
        from ocr import setup_tesseract

        previous = os.environ.get("TESSERACT_CMD")
        os.environ["TESSERACT_CMD"] = str(TESSERACT)
        try:
            image = np.array(Image.open(FORM_SAMPLE).convert("RGB"))
            result = analyze_image(image, setup_tesseract())
        finally:
            if previous is None:
                os.environ.pop("TESSERACT_CMD", None)
            else:
                os.environ["TESSERACT_CMD"] = previous

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.kind, "form")
        self.assertEqual(result.method, "local-ai-form:key-value")
        self.assertEqual(result.table[0], ["Field", "Value"])
        fields = dict(result.table[1:])
        self.assertEqual(fields["Mobile"], "0501234001")
        self.assertEqual(fields["Car Number"], "10001")
        self.assertEqual(fields["Car Name"], "KIA")

    @unittest.skipUnless(BILINGUAL_INVOICE_SAMPLE.exists() and TESSERACT.exists(), "Bundled OCR runtime is not available.")
    def test_bilingual_tax_invoice_merges_logical_rows_and_marks_reconstructed_totals_for_review(self):
        from PIL import Image
        import numpy as np
        from local_ai import analyze_image
        from ocr import setup_tesseract

        previous = os.environ.get("TESSERACT_CMD")
        os.environ["TESSERACT_CMD"] = str(TESSERACT)
        try:
            image = np.array(Image.open(BILINGUAL_INVOICE_SAMPLE).convert("RGB"))
            result = analyze_image(image, setup_tesseract(), include_page_text=True)
        finally:
            if previous is None:
                os.environ.pop("TESSERACT_CMD", None)
            else:
                os.environ["TESSERACT_CMD"] = previous

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.kind, "generic_table")
        self.assertTrue(result.method.startswith("local-ai-generic-table:"))
        self.assertEqual(result.table[0], ["Line No.", "Description", "Qty", "Rate", "Unit", "VAT %", "Taxable Value", "VAT", "Total incl. VAT"])
        self.assertEqual(result.table[1][0:4], ["1", "Fridge 210 Lts", "12", "4998.00"])
        self.assertEqual(result.table[1][6:9], ["59976.00", "2998.80", "62974.80"])
        self.assertEqual(result.table[2][0:4], ["2", "Air Conditioner", "15", "1187.40"])
        self.assertEqual(result.table[2][6:9], ["17811.00", "890.55", "18701.55"])
        self.assertNotIn("Invoice No: 997600", result.page_context)

    @unittest.skipUnless(SAMPLE.exists(), "The invoice regression sample is not available.")
    def test_coloured_grid_targets_item_table_not_whole_form(self):
        from PIL import Image
        import numpy as np
        from local_ai import _find_coloured_grids

        image = np.array(Image.open(SAMPLE).convert("RGB"))
        grids = _find_coloured_grids(image)
        item_grid = next(
            (
                grid
                for grid in grids
                if len(grid.x_lines) == 6 and grid.y_lines[0] >= 265 and grid.y_lines[0] <= 275
            ),
            None,
        )
        self.assertIsNotNone(item_grid)
        assert item_grid is not None
        self.assertEqual(len(item_grid.x_lines) - 1, 5)
        self.assertGreaterEqual(len(item_grid.y_lines) - 1, 4)

    @unittest.skipUnless(SAMPLE.exists() and TESSERACT.exists(), "Bundled OCR runtime is not available.")
    def test_sample_invoice_reads_separate_numeric_cells(self):
        from PIL import Image
        import numpy as np
        from local_ai import analyze_image
        from ocr import setup_tesseract

        previous = os.environ.get("TESSERACT_CMD")
        os.environ["TESSERACT_CMD"] = str(TESSERACT)
        try:
            image = np.array(Image.open(SAMPLE).convert("RGB"))
            result = analyze_image(image, setup_tesseract())
        finally:
            if previous is None:
                os.environ.pop("TESSERACT_CMD", None)
            else:
                os.environ["TESSERACT_CMD"] = previous

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(result.table), 4)
        self.assertEqual([row[0] for row in result.table[1:]], ["25000.00", "38000.00", "13750.00"])
        self.assertEqual([row[1] for row in result.table[1:]], ["25000.00", "2000.00", "55.00"])
        self.assertEqual([row[3] for row in result.table[1:]], ["1", "19", "250"])

    @unittest.skipUnless(SPREADSHEET_SAMPLE.exists() and TESSERACT.exists(), "Bundled OCR runtime is not available.")
    def test_dense_spreadsheet_uses_adaptive_numeric_cell_ocr(self):
        """Do not turn missing OCR dots into plausible but wrong amounts."""
        from PIL import Image
        import numpy as np
        from local_ai import analyze_image
        from ocr import setup_tesseract

        previous = os.environ.get("TESSERACT_CMD")
        os.environ["TESSERACT_CMD"] = str(TESSERACT)
        try:
            image = np.array(Image.open(SPREADSHEET_SAMPLE).convert("RGB"))
            result = analyze_image(image, setup_tesseract())
        finally:
            if previous is None:
                os.environ.pop("TESSERACT_CMD", None)
            else:
                os.environ["TESSERACT_CMD"] = previous

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.method, "local-ai-ruled-grid")
        self.assertGreaterEqual(len(result.table), 22)
        self.assertEqual(result.table[-1][1:5], ["ARIZONA", "AZ", "1973", "11598.26"])
        self.assertEqual(result.table[-1][5:9], ["4963.46", "1647.88", "4986.92", "27304.64"])
        self.assertEqual(result.table[-1][10:13], ["714.5", "4.1", "23196.52"])

    @unittest.skipUnless(DIRECTORY_SAMPLE.exists() and TESSERACT.exists(), "Bundled OCR runtime is not available.")
    def test_faint_excel_grid_reconstructs_columns_from_word_alignment(self):
        from PIL import Image
        import numpy as np
        from local_ai import analyze_image
        from ocr import setup_tesseract

        previous = os.environ.get("TESSERACT_CMD")
        os.environ["TESSERACT_CMD"] = str(TESSERACT)
        try:
            image = np.array(Image.open(DIRECTORY_SAMPLE).convert("RGB"))
            result = analyze_image(image, setup_tesseract())
        finally:
            if previous is None:
                os.environ.pop("TESSERACT_CMD", None)
            else:
                os.environ["TESSERACT_CMD"] = previous

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.method, "local-ai-word-layout")
        self.assertGreaterEqual(len(result.table), 30)
        self.assertGreaterEqual(len(result.table[0]), 8)
        self.assertTrue(result.table[1][2].replace(" ", "").isdigit())
        self.assertIn("@", result.table[1][3])

    @unittest.skipUnless(BORDERLESS_INVOICE_SAMPLE.exists() and TESSERACT.exists(), "Bundled OCR runtime is not available.")
    def test_borderless_invoice_word_layout_keeps_two_separate_items(self):
        from PIL import Image
        import numpy as np
        from invoice import parse_invoice_table
        from local_ai import analyze_image
        from ocr import setup_tesseract

        previous = os.environ.get("TESSERACT_CMD")
        os.environ["TESSERACT_CMD"] = str(TESSERACT)
        try:
            image = np.array(Image.open(BORDERLESS_INVOICE_SAMPLE).convert("RGB"))
            result = analyze_image(image, setup_tesseract(), include_page_text=True)
        finally:
            if previous is None:
                os.environ.pop("TESSERACT_CMD", None)
            else:
                os.environ["TESSERACT_CMD"] = previous

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.method, "local-ai-word-layout")
        self.assertEqual(result.table[1][:3], ["20.00", "10.00", "2"])
        self.assertEqual(result.table[2][:3], ["25.00", "25.00", "1"])
        self.assertIn("الخدمة B", result.table[2][3])
        self.assertIn("Invoice No: 123456", result.page_context)
        self.assertIn("Invoice Date: 2021-01-01", result.page_context)
        self.assertIn("Subtotal: 45.00", result.page_context)
        self.assertIn("Tax Amount: 3.60", result.page_context)
        self.assertIn("Grand Total: 39.60", result.page_context)
        parsed = parse_invoice_table(result.table, result.scores, [result.page_context])
        self.assertEqual(parsed["header"].get("invoice_number"), "123456")
        self.assertEqual(parsed["totals"].get("grand_total"), "39.60")
        self.assertFalse(parsed["items"][1]["review"])


if __name__ == "__main__":
    unittest.main()
