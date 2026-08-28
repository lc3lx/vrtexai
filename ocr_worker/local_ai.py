"""Small on-device document-intelligence module.

The module deliberately stays local: it uses the bundled OpenCV and
Tesseract runtime only.  It first finds a credible table region, then applies
an OCR ensemble only where it adds evidence.  That matters more than a fixed
``3x`` resize: screenshots, scans, faint Excel grids, and coloured invoice
headers need different treatment.

It is intentionally conservative.  A candidate must have repeated rows plus
credible headings before it is returned; otherwise the caller keeps the
established generic OCR fallback instead of inventing a table.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from clean import canonical_header, normalize_date
from preprocess import drop_color_noise, enhance_gray


Cell = tuple[int, int, int, int]
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_NUMERIC = re.compile(r"^-?\d+(?:[.,]\d{1,3})?$")
_DATE = re.compile(r"\b\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\b")
_CODE = re.compile(r"\b(?=[A-Z0-9_-]*[A-Z])(?=[A-Z0-9_-]*\d)[A-Z][A-Z0-9_-]{2,39}\b", re.I)
_KNOWN_FIELDS = {"description", "qty", "unit_price", "total", "sku"}
_NUMERIC_FIELDS = {"qty", "unit_price", "total"}
_STRUCTURED_FIELDS = _KNOWN_FIELDS | {"email", "phone", "city", "region", "name", "id_code", "status"}


@dataclass(frozen=True)
class LocalAIResult:
    """A table chosen by the local analyser, plus its OCR evidence."""

    table: list[list[str]]
    scores: list[list[float]]
    page_context: str = ""
    method: str = "local-ai-coloured-grid"
    kind: str = "invoice"


@dataclass(frozen=True)
class _GridCandidate:
    x_lines: tuple[int, ...]
    y_lines: tuple[int, ...]
    score: float


def _groups(values: np.ndarray | list[int], gap: int) -> list[list[int]]:
    groups: list[list[int]] = []
    for raw in values:
        value = int(raw)
        if not groups or value > groups[-1][-1] + gap:
            groups.append([value])
        else:
            groups[-1].append(value)
    return groups


def _colored_header_bands(image: np.ndarray) -> list[tuple[int, int, float]]:
    """Return substantial coloured horizontal bands, excluding the page edge."""
    import cv2

    if image.ndim != 3:
        return []
    height, _width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
    # Blue, green, and other saturated header bars are useful document
    # structure.  They must not be treated as watermark noise.
    coloured = (hsv[:, :, 1] > 55) & (hsv[:, :, 2] > 35)
    density = coloured.mean(axis=1)
    bands: list[tuple[int, int, float]] = []
    for group in _groups(np.where(density > 0.35)[0], gap=1):
        if not 6 <= len(group) <= max(42, int(height * 0.11)):
            continue
        if group[0] <= max(4, int(height * 0.04)):
            continue
        bands.append((group[0], group[-1], float(density[group].mean())))
    return bands


def _line_positions(image: np.ndarray, top: int) -> tuple[list[int], list[int]]:
    """Find line positions below one header band without using page-wide splits."""
    import cv2

    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 35, 110)
    roi = edges[top:]
    if roi.shape[0] < max(36, height // 10):
        return [], []

    vertical = cv2.morphologyEx(
        roi,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(12, height // 24))),
    )
    vertical_projection = vertical.mean(axis=0)
    vertical_threshold = max(7.0, float(np.percentile(vertical_projection, 88)))
    x_lines: list[int] = []
    for group in _groups(np.where(vertical_projection >= vertical_threshold)[0], gap=4):
        position = max(group, key=lambda x: float(vertical_projection[x]))
        # A separator needs to remain visible for a meaningful part of the
        # body, otherwise it is likely a logo or a form field above the table.
        if float((vertical[:, position] > 0).mean()) >= 0.28:
            x_lines.append(position)

    horizontal = cv2.morphologyEx(
        roi,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, width // 12), 1)),
    )
    horizontal_projection = horizontal.mean(axis=1)
    horizontal_threshold = max(7.0, float(np.percentile(horizontal_projection, 86)))
    y_lines = [
        top + int(round(sum(group) / len(group)))
        for group in _groups(np.where(horizontal_projection >= horizontal_threshold)[0], gap=4)
    ]
    return sorted(set(x_lines)), sorted(set(y_lines))


def _find_coloured_grids(image: np.ndarray) -> list[_GridCandidate]:
    """Return structurally credible coloured grids, best candidate first."""
    height, width = image.shape[:2]
    choices: list[_GridCandidate] = []
    for top, _bottom, density in _colored_header_bands(image):
        x_lines, all_y_lines = _line_positions(image, top)
        y_lines = [value for value in all_y_lines if value >= top - 3]
        if len(x_lines) < 5 or len(x_lines) > 12 or len(y_lines) < 4:
            continue
        if y_lines[0] > top + 4 or x_lines[-1] - x_lines[0] < width * 0.55:
            continue
        x_gaps = np.diff(x_lines)
        y_gaps = np.diff(y_lines)
        if not len(x_gaps) or min(x_gaps) < max(18, width // 50):
            continue
        plausible_rows = [
            int(gap)
            for gap in y_gaps[:8]
            if max(8, height // 80) <= gap <= max(60, int(height * 0.20))
        ]
        # Header + at least two body rows must be visible.
        if len(plausible_rows) < 3:
            continue
        # The final term makes the item table win over decorative/header bands
        # higher on a form, while the structural checks above prevent a footer
        # from becoming a table.
        score = (
            len(x_lines) * 3.0
            + min(len(y_lines), 8)
            + len(plausible_rows) * 3.0
            + density * 5.0
            + (top / max(height, 1)) * 20.0
        )
        choices.append(_GridCandidate(tuple(x_lines), tuple(y_lines), score))
    return sorted(choices, key=lambda choice: choice.score, reverse=True)


def _find_coloured_grid(image: np.ndarray) -> _GridCandidate | None:
    """Compatibility helper for callers/tests that need the top candidate."""
    choices = _find_coloured_grids(image)
    return choices[0] if choices else None


def _suggest_cell_scale(cell: Cell, padding: int = 0) -> float:
    """Choose a scale from the original cell height, not from page size."""
    _x, _y, _width, height = cell
    # Do not trim an already short final spreadsheet row: its decimal dot can
    # sit exactly one pixel above the bottom rule.
    inset = 0 if padding or min(_width, height) <= 20 else min(2, max(1, min(_width, height) // 18))
    usable_height = max(1, height - inset * 2)
    # 57px keeps thin spreadsheet digits separated while retaining enough
    # pixels for small invoice Arabic labels.
    return float(np.clip(57.0 / usable_height, 1.0, 3.5))


def _cell_image(
    image: np.ndarray,
    cell: Cell,
    scale: float | None = None,
    padding: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Return enhanced greyscale and Otsu views sized for the text in a cell.

    The prior fixed 3x enlargement made the thin spreadsheet glyphs in a
    40-pixel-high cell blur together, while a small invoice cell genuinely
    needed that enlargement.  Targeting a readable glyph/cell height gives us
    a local, deterministic equivalent of an OCR-resolution enhancement step.
    """
    import cv2

    x, y, width, height = cell
    # A one/two pixel ruled-cell border is useful for layout detection but
    # frequently becomes ``|`` / ``1`` during OCR.  Leave the halo requested
    # by a header reader intact; otherwise take the border out of the crop.
    inset = 0 if padding or min(width, height) <= 20 else min(2, max(1, min(width, height) // 18))
    crop = image[
        max(0, y - padding + inset): min(image.shape[0], y + height + padding - inset),
        max(0, x - padding + inset): min(image.shape[1], x + width + padding - inset),
    ]
    if crop.size == 0:
        return np.empty((0, 0), dtype=np.uint8), np.empty((0, 0), dtype=np.uint8)
    if scale is None:
        # 57px is a stable sweet spot for the bundled LSTM on the supplied
        # Arabic/English documents.  Cap it so a full spreadsheet does not
        # turn into hundreds of expensive giant OCR images.
        scale = _suggest_cell_scale(cell, padding=padding)
    enlarged = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(enlarged, cv2.COLOR_RGB2GRAY) if enlarged.ndim == 3 else enlarged
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    blur = cv2.GaussianBlur(enhanced, (0, 0), 0.8)
    enhanced = cv2.addWeighted(enhanced, 1.25, blur, -0.25, 0)
    _threshold, binary = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return enhanced, binary


def _ocr(pytesseract: Any, image: np.ndarray, *, lang: str, psm: int, numeric: bool = False) -> tuple[str, float]:
    if image.size == 0:
        return "", 0.0
    config = f"--oem 1 --psm {psm}"
    if numeric:
        config += " -c tessedit_char_whitelist=0123456789.,-"
    data = pytesseract.image_to_data(image, lang=lang, config=config, output_type=pytesseract.Output.DICT)
    words: list[str] = []
    confidences: list[float] = []
    for index, raw in enumerate(data.get("text", [])):
        word = str(raw or "").strip()
        if word:
            words.append(word)
        try:
            confidence = float(data["conf"][index])
        except (IndexError, TypeError, ValueError):
            confidence = -1.0
        if word and confidence >= 0:
            confidences.append(confidence)
    text = _tidy(" ".join(words))
    confidence = round(sum(confidences) / len(confidences), 1) if confidences else 0.0
    return text, confidence


def _ocr_fast_fallback(
    pytesseract: Any,
    image: np.ndarray,
    *,
    lang: str,
    psm: int,
    numeric: bool = False,
) -> tuple[str, float]:
    """Read an ambiguous cell with the bundled fast LSTM as a second vote.

    Release builds ship high-accuracy ``ara``/``eng`` data as the primary
    model and retain the compact original models under ``*_fast`` aliases.
    Numeric glyphs sometimes survive better in the smaller model; a missing
    alias in an older development runtime simply disables this extra vote.
    """
    fast_lang = "+".join(f"{part}_fast" for part in lang.split("+"))
    try:
        return _ocr(pytesseract, image, lang=fast_lang, psm=psm, numeric=numeric)
    except Exception:
        return "", 0.0


def _tidy(value: str) -> str:
    text = str(value or "").replace("|", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip(" -_,;:()[]{}")
    text = re.sub(r"^[.،؛]+|[.،؛]+$", "", text)
    text = re.sub(r"[\u064b-\u065f\u0670]+$", "", text)
    return text


def _numeric_value(value: str) -> str:
    text = _tidy(value).translate(_ARABIC_DIGITS).replace(" ", "")
    # OCR often keeps a border as a harmless punctuation token.  Never add a
    # decimal point: return only a number that was actually read.
    matches = re.findall(r"-?\d+(?:[.,]\d{1,3})?", text)
    if len(matches) == 1:
        return matches[0].replace(",", "")
    return text if _NUMERIC.fullmatch(text) else ""


def _money_value(value: str) -> str:
    """Normalise an actually-read grouped monetary value without guessing dots.

    Tesseract can faithfully see both separators in ``4.998.00`` while using
    the same dot for the thousands group and cents separator.  That is not a
    valid generic number, but in a known money cell it is unambiguous evidence
    for ``4998.00``.  A plain run of digits is deliberately left untouched.
    """
    text = _tidy(value).translate(_ARABIC_DIGITS).replace(" ", "")
    direct = _numeric_value(text)
    if _NUMERIC.fullmatch(direct):
        return direct
    grouped = re.fullmatch(r"(-?)(\d{1,3})[.,](\d{3})[.,](\d{2})", text)
    if grouped:
        return f"{grouped.group(1)}{grouped.group(2)}{grouped.group(3)}.{grouped.group(4)}"
    joined = re.fullmatch(r"(-?)(\d{1,3})[.,](\d{3})(\d{2})", text)
    if joined:
        return f"{joined.group(1)}{joined.group(2)}{joined.group(3)}.{joined.group(4)}"
    return ""


def _has_grouped_money_evidence(value: str) -> bool:
    text = _tidy(value).translate(_ARABIC_DIGITS).replace(" ", "")
    return bool(
        re.fullmatch(r"-?\d{1,3}[.,]\d{3}[.,]\d{2}", text)
        or re.fullmatch(r"-?\d{1,3}[.,]\d{5}", text)
    )


def _as_number(value: str) -> float | None:
    """Return an actually-read number, never a guessed or repaired one."""
    text = _numeric_value(value)
    if not text or not _NUMERIC.fullmatch(text):
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _header_looks_like_data(headers: list[str]) -> bool:
    """Reject a ruled-grid candidate that starts on its first data row.

    A common segmentation failure starts below a missing header rule.  The
    first product row is then treated as headings, which makes later values
    look plausibly structured while being shifted into the wrong columns.
    """
    filled = [_tidy(value) for value in headers if _tidy(value)]
    if len(filled) < 3:
        return True
    money_or_number = re.compile(
        r"(?i)^(?:(?:AED|SAR|USD|EUR)\s*)?-?\d+(?:[.,]\d{1,3})?%?$"
    )
    value_like = sum(bool(money_or_number.fullmatch(value.replace(" ", ""))) for value in filled)
    return value_like >= max(2, int(np.ceil(len(filled) * 0.35)))


def _looks_like_money_header(value: str) -> bool:
    """Recognise a monetary column even when OCR did not canonicalise it."""
    return bool(re.search(
        r"(?:taxable|vat|value|amount|total|price|rate|cost|aed|sar|usd|eur|"
        r"\u0627\u0644\u0645\u0628\u0644\u063a|\u0627\u0644\u0636\u0631\u064a\u0628|\u0627\u0644\u0642\u064a\u0645)",
        value,
        re.I,
    ))


def _infer_compact_invoice_headers(
    headers: list[str],
    fields: list[str],
    scores: list[float],
) -> tuple[list[str], list[str], list[float]]:
    """Recover the standard five-column invoice layout from partial headings.

    On low-resolution Arabic invoices the grey header band can lose the words
    for amount, price, and description while the physical columns remain very
    clear.  Inferring *only the header roles* from the proven 5-column order
    preserves the cell boundaries and lets the caller export a useful review
    table instead of falling back to a page-wide OCR blob.
    """
    if len(headers) != 5 or len(fields) != 5:
        return headers, fields, scores
    try:
        quantity_index = fields.index("qty")
    except ValueError:
        return headers, fields, scores
    if quantity_index != 2 or "description" not in fields:
        return headers, fields, scores
    inferred_headers = ["Total", "Unit Price", "Qty", "Description", "Product"]
    inferred_fields = ["total", "unit_price", "qty", "description", "product_name"]
    inferred_scores = [max(float(score or 0), 68.0) for score in scores]
    return inferred_headers, inferred_fields, inferred_scores


def _has_complete_invoice_schema(fields: list[str]) -> bool:
    return {"description", "qty", "unit_price", "total"}.issubset(fields)


def _invoice_rows_are_arithmetically_credible(table: list[list[str]], fields: list[str]) -> bool:
    """Allow local invoice semantics only when its line items corroborate them.

    A genuine invoice may include a modest tax or discount in its final total,
    but an OCR decimal loss turns a line total into 10x or 100x the independently
    read quantity × rate.  In that case returning a generic reviewable table is
    safer than exporting an apparently valid but wrong invoice.
    """
    if not _has_complete_invoice_schema(fields):
        return True
    indices = {field: fields.index(field) for field in {"description", "qty", "unit_price", "total"}}
    credible_items = 0
    for row in table[1:]:
        values = {
            field: _tidy(row[index]) if index < len(row) else ""
            for field, index in indices.items()
        }
        if not any(values.values()):
            continue
        description = values["description"]
        joined = " ".join(values.values())
        # A labelled subtotal / VAT / total line is document context, not an
        # item that has to satisfy quantity × rate arithmetic.
        if re.search(r"(?:\b(?:sub\s*total|grand\s*total|vat|tax)\b|المجموع|الضريبة)", joined, re.I):
            continue
        quantity = _as_number(values["qty"])
        price = _as_number(values["unit_price"])
        total = _as_number(values["total"])
        if not description or quantity is None or price is None or total is None:
            return False
        if quantity <= 0 or price < 0 or total < 0:
            return False
        expected = quantity * price
        if expected == 0:
            if total > 0.02:
                return False
        else:
            ratio = total / expected
            # Covers normal tax/discount cases while rejecting missing-decimal
            # OCR such as 62,948.0 read as 629480.
            if not 0.50 <= ratio <= 1.50:
                return False
        credible_items += 1
    return credible_items > 0


def _local_result_is_safe(result: LocalAIResult) -> bool:
    """Apply the final no-fabrication gate shared by every local analyser."""
    if len(result.table) < 2 or not result.table[0]:
        return False
    fields = [canonical_header(value) for value in result.table[0]]
    item_fields = set(fields) & {"description", "qty", "unit_price", "total"}
    # A candidate that claims most of an invoice schema but loses one field is
    # especially dangerous: semantic export would silently shift its values.
    if (
        len(item_fields) >= 3
        or {"description", "qty"}.issubset(item_fields)
    ) and not _has_complete_invoice_schema(fields):
        return False
    return _invoice_rows_are_arithmetically_credible(result.table, fields)


def _local_result_is_structurally_useful(result: LocalAIResult) -> bool:
    """Whether a local result is safe to export as a *generic* Excel sheet.

    This intentionally has a lower bar than semantic invoice creation. A
    visible table with uncertain money values is still useful when preserved
    in its own columns and highlighted for review; pretending it is no table
    at all sends it through the much weaker full-page fallback.
    """
    if len(result.table) < 2 or not result.table[0]:
        return False
    headers = [str(value or "").strip() for value in result.table[0]]
    if sum(bool(value) for value in headers) < 2:
        return False
    return any(any(str(value or "").strip() for value in row) for row in result.table[1:])


def _remove_unsupported_generic_numbers(result: LocalAIResult) -> LocalAIResult:
    """Leave impossible low-resolution money cells blank instead of plausible.

    This path is used only for an unsafe table that will be exported for
    review, never for a validated invoice or arithmetic reconstruction. A
    blank is more honest than a zero/one-digit amount produced with no OCR
    confidence, and the raw source remains available in the context sheet.
    """
    table = [list(row) for row in result.table]
    scores = [list(row) for row in result.scores]
    if not table:
        return result
    fields = [canonical_header(value) for value in table[0]]
    for row_index in range(1, len(table)):
        for column, field in enumerate(fields):
            if column >= len(table[row_index]):
                continue
            numeric = field in _NUMERIC_FIELDS or _looks_like_money_header(table[0][column])
            confidence = (
                float(scores[row_index][column] or 0)
                if row_index < len(scores) and column < len(scores[row_index])
                else 0.0
            )
            if numeric and str(table[row_index][column]).strip() and confidence < 35.0:
                table[row_index][column] = ""
    return replace(result, table=table, scores=scores)


def _row_has_text_ink(image: np.ndarray, cells: list[Cell]) -> bool:
    """Avoid OCR hallucinations from blank ruled rows below an item table."""
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    for x, y, width, height in cells:
        margin = min(3, max(1, width // 8), max(1, height // 4))
        crop = gray[
            y + margin: min(gray.shape[0], y + height - margin),
            x + margin: min(gray.shape[1], x + width - margin),
        ]
        if crop.size and float((crop < 180).mean()) > 0.004:
            return True
    return False


def _candidate_score(text: str, confidence: float, *, field: str = "", header: bool = False) -> float:
    if not text:
        return -1000.0
    score = max(0.0, confidence)
    if field in _NUMERIC_FIELDS:
        return score + (100.0 if _NUMERIC.fullmatch(_numeric_value(text)) else -80.0)
    if header:
        mapped = canonical_header(text)
        if mapped in _KNOWN_FIELDS | {"unit_price", "total", "qty", "description"}:
            score += 100.0
        letters = sum(character.isalpha() or "\u0600" <= character <= "\u06ff" for character in text)
        score += min(letters, 18)
    else:
        arabic = sum("\u0600" <= character <= "\u06ff" for character in text)
        letters = sum(character.isalpha() for character in text)
        score += min(arabic + letters, 24) * 0.7
        score -= min(text.count("=") + text.count("~") + text.count("[") + text.count("]"), 4) * 10
    return score


def _read_header_cell(
    pytesseract: Any,
    cell: Cell,
    image: np.ndarray,
    *,
    language: str = "ara+eng",
) -> tuple[str, float]:
    # Projection lines can land on the inside edge of a two-pixel header rule.
    # Keep a one-pixel halo here so a tiny RTL label is not clipped.
    # Header bands are short by design; retaining the established 3x view
    # avoids a fractional resampling phase that can erase a tiny RTL label.
    gray, binary = _cell_image(image, cell, scale=3.0, padding=1)
    candidates = [
        _ocr(pytesseract, binary, lang=language, psm=11),
        _ocr_fast_fallback(pytesseract, binary, lang=language, psm=11),
    ]
    best = max(candidates, key=lambda item: _candidate_score(*item, header=True))
    # PSM 11 is best for sparse white writing on coloured bands.  A compact
    # header such as "سعر الوحدة" sometimes needs a normal single-line pass.
    if canonical_header(best[0]) not in _KNOWN_FIELDS:
        candidates.extend([
            _ocr(pytesseract, gray, lang=language, psm=11),
            _ocr(pytesseract, binary, lang=language, psm=6),
            _ocr_fast_fallback(pytesseract, gray, lang=language, psm=11),
            _ocr_fast_fallback(pytesseract, binary, lang=language, psm=6),
        ])
        best = max(candidates, key=lambda item: _candidate_score(*item, header=True))
    return best


def _read_body_cell(
    pytesseract: Any,
    cell: Cell,
    image: np.ndarray,
    field: str,
    *,
    arabic_page: bool,
    numeric_override: bool = False,
    prefer_decimal: bool = False,
) -> tuple[str, float]:
    gray, binary = _cell_image(image, cell)
    numeric = field in _NUMERIC_FIELDS or numeric_override
    language = "eng" if numeric else ("ara" if arabic_page else "eng")
    # Otsu is the primary numeric view.  It preserves decimal dots in thin
    # spreadsheet cells better than the old unthresholded fixed-3x pass.
    # The high-accuracy LSTM is the primary text reader.  For a tightly
    # cropped digit cell the compact LSTM remains the primary vote: its
    # smaller receptive field preserves thin spreadsheet dots and digits.
    primary = (
        _ocr_fast_fallback(pytesseract, binary, lang=language, psm=7, numeric=True)
        if numeric
        else _ocr(pytesseract, binary, lang=language, psm=6)
    )
    if numeric and not primary[0]:
        primary = _ocr(pytesseract, binary, lang=language, psm=7, numeric=True)
    if numeric:
        value = _money_value(primary[0]) if prefer_decimal else _numeric_value(primary[0])
        if (
            _NUMERIC.fullmatch(value)
            and primary[1] >= 78.0
            and (not prefer_decimal or "." in value or "," in value)
        ):
            return value, primary[1]
        # A valid number is not proof that its digits are right.  Compare an
        # independent greyscale pass before accepting it, and retain the real
        # OCR confidence so ambiguous values remain reviewable.
        fallback = _ocr_fast_fallback(pytesseract, gray, lang=language, psm=7, numeric=True)
        if not fallback[0]:
            fallback = _ocr(pytesseract, gray, lang=language, psm=7, numeric=True)
        candidates = [
            primary,
            fallback,
            _ocr_fast_fallback(pytesseract, binary, lang=language, psm=7, numeric=True),
            _ocr_fast_fallback(pytesseract, gray, lang=language, psm=7, numeric=True),
            _ocr(pytesseract, binary, lang=language, psm=7, numeric=True),
            _ocr(pytesseract, gray, lang=language, psm=7, numeric=True),
        ]
        if not _NUMERIC.fullmatch(value) or primary[1] < 70.0:
            candidates.append(_ocr(pytesseract, binary, lang=language, psm=6, numeric=True))
        if primary[1] < 55.0:
            # Tiny digits occasionally land exactly on a resampling phase that
            # turns 5 into 8.  A nearby scale is an independent image view,
            # not a guessed digit substitution.
            _alternate_gray, alternate_binary = _cell_image(
                image, cell, scale=max(1.0, _suggest_cell_scale(cell) * 0.91)
            )
            candidates.append(_ocr(pytesseract, alternate_binary, lang=language, psm=7, numeric=True))
            if field == "qty":
                _large_gray, large_binary = _cell_image(
                    image, cell, scale=min(4.0, _suggest_cell_scale(cell) * 1.35)
                )
                candidates.append(_ocr(pytesseract, large_binary, lang=language, psm=7, numeric=True))

        # A small padded ensemble preserves paired thousands/cents separators
        # that fall on different resampling phases.  It runs only for an
        # already-ambiguous money cell; normal high-confidence cells return
        # above without this extra CPU work.
        if prefer_decimal:
            for scale in (2.5, 3.5):
                probe_gray, probe_binary = _cell_image(image, cell, scale=scale, padding=1)
                candidates.extend([
                    _ocr(pytesseract, probe_gray, lang=language, psm=7, numeric=True),
                    _ocr(pytesseract, probe_binary, lang=language, psm=7, numeric=True),
                    _ocr_fast_fallback(pytesseract, probe_gray, lang=language, psm=7, numeric=True),
                    _ocr_fast_fallback(pytesseract, probe_binary, lang=language, psm=7, numeric=True),
                ])

        normalised = [
            _money_value(item[0]) if prefer_decimal else _numeric_value(item[0])
            for item in candidates
        ]

        def numeric_score(item: tuple[str, float]) -> float:
            candidate_value = _money_value(item[0]) if prefer_decimal else _numeric_value(item[0])
            if not _NUMERIC.fullmatch(candidate_value):
                return -80.0 + max(0.0, item[1])
            score = 100.0 + max(0.0, item[1])
            if prefer_decimal and "." in candidate_value:
                score += 25.0
                if _has_grouped_money_evidence(item[0]):
                    score += 20.0
                # Agreement across independently resized/cropped views is
                # stronger than a single optimistic Tesseract confidence.
                support = sum(value == candidate_value for value in normalised)
                score += 50.0 * max(0, support - 1)
            return score
        best = max(enumerate(candidates), key=lambda item: (numeric_score(item[1]), item[1][1], item[0]))[1]
        value = _money_value(best[0]) if prefer_decimal else _numeric_value(best[0])
        return (value, best[1]) if _NUMERIC.fullmatch(value) else (_tidy(best[0]), best[1])

    # Thresholding restores tiny Arabic strokes in low-resolution screenshots.
    # If it has little evidence, compare it with an unthresholded pass.
    if primary[1] >= 62.0 and len(primary[0]) >= 2:
        return primary
    fallback = _ocr(pytesseract, gray, lang=language, psm=6)
    return max((primary, fallback), key=lambda item: _candidate_score(*item))


def _page_context(image: np.ndarray, pytesseract: Any, fields: list[str]) -> str:
    """Keep raw OCR context and add only high-evidence invoice identifiers."""
    import cv2

    # For page-level labels, the established watermark cleanup exposes text in
    # the coloured title area well.  The table reader above always uses the
    # untouched image so its blue header and grid remain available as layout
    # evidence.
    cleaned = drop_color_noise(image)
    scaled = cv2.resize(cleaned, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(scaled, cv2.COLOR_RGB2GRAY) if scaled.ndim == 3 else scaled
    gray = enhance_gray(gray)
    raw = pytesseract.image_to_string(gray, lang="ara+eng", config="--oem 1 --psm 3").strip()
    invoice_like = {"description", "qty", "unit_price", "total"}.issubset(set(fields))
    spatial = _spatial_invoice_context(image, pytesseract) if invoice_like else []
    if not raw and not spatial:
        return ""
    # A date and alphanumeric identifier on the same OCR line is a reliable
    # invoice-header pattern.  This preserves useful header fields without
    # mapping arbitrary page text to a supplier/client.
    additions: list[str] = []
    if invoice_like:
        for line in raw.splitlines():
            date = _DATE.search(line)
            code = _CODE.search(line)
            if date and code:
                additions.append(f"Invoice No: {code.group(0).upper()}")
                additions.append(f"Invoice Date: {normalize_date(date.group(0))}")
                break
    return "\n".join([*spatial, raw, *additions])


def _analyze_coloured_grid(image: np.ndarray, pytesseract: Any, *, include_page_text: bool = False) -> LocalAIResult | None:
    """Analyse a coloured, ruled item grid using the local adaptive module.

    Returns ``None`` for ordinary pages so the established generic OCR path
    remains responsible for unstructured documents and white spreadsheets.
    """
    if image.ndim != 3 or min(image.shape[:2]) < 120:
        return None
    # A form can have several coloured bands.  Structure alone cannot decide
    # whether one is a customer-detail label or the item-table header, so use
    # the locally OCRed header evidence to select it.  Limit probing to the
    # strongest candidates to keep the fast path practical on a CPU.
    for grid in _find_coloured_grids(image)[:5]:
        x_lines, y_lines = grid.x_lines, grid.y_lines
        header_cells = [
            (x_lines[index], y_lines[0], x_lines[index + 1] - x_lines[index], y_lines[1] - y_lines[0])
            for index in range(len(x_lines) - 1)
        ]
        header_values, header_scores = zip(*[_read_header_cell(pytesseract, cell, image) for cell in header_cells])
        fields = [canonical_header(value) for value in header_values]
        if sum(field in _KNOWN_FIELDS for field in fields) < 3:
            continue
        arabic_page = sum("\u0600" <= character <= "\u06ff" for value in header_values for character in value) >= 2

        table: list[list[str]] = [list(header_values)]
        scores: list[list[float]] = [list(header_scores)]
        empty_rows = 0
        # Protect against a damaged form producing hundreds of faint horizontal
        # lines.  A real item table rarely needs more than 60 body rows per page.
        for row_index in range(1, min(len(y_lines) - 1, 61)):
            top, bottom = y_lines[row_index], y_lines[row_index + 1]
            if bottom - top < 8:
                continue
            cells = [
                (x_lines[column], top, x_lines[column + 1] - x_lines[column], bottom - top)
                for column in range(len(x_lines) - 1)
            ]
            if not _row_has_text_ink(image, cells):
                break
            values_and_scores = [
                _read_body_cell(
                    pytesseract,
                    cell,
                    image,
                    fields[column],
                    arabic_page=arabic_page,
                    prefer_decimal=fields[column] in {"unit_price", "total"},
                )
                for column, cell in enumerate(cells)
            ]
            values = [pair[0] for pair in values_and_scores]
            confidences = [pair[1] for pair in values_and_scores]
            # When a short quantity digit is clipped by a ruled cell, the
            # independently read price and amount can prove the intended whole
            # quantity.  Apply only that exact arithmetic reconciliation; the
            # confidence remains low so the exported item is still reviewable.
            if {"qty", "unit_price", "total"}.issubset(fields):
                from invoice import reconcile_quantity_from_total
                qty_index = fields.index("qty")
                price_index = fields.index("unit_price")
                total_index = fields.index("total")
                corrected, changed = reconcile_quantity_from_total(
                    values[qty_index], values[price_index], values[total_index]
                )
                if changed:
                    values[qty_index] = corrected
            if not any(value.strip() for value in values):
                empty_rows += 1
                if empty_rows >= 1:
                    break
                continue
            empty_rows = 0
            table.append(values)
            scores.append(confidences)

        if len(table) >= 2:
            context = _page_context(image, pytesseract, fields) if include_page_text else ""
            return LocalAIResult(table=table, scores=scores, page_context=context)
    return None


# ---------------------------------------------------------------------------
# General local layout analyser
# ---------------------------------------------------------------------------

def _merge_line_segments(segments: list[tuple[int, int, int]], gap: int = 4) -> list[tuple[int, int, int]]:
    """Merge duplicate Hough detections of the same ruled line."""
    if not segments:
        return []
    groups: list[list[tuple[int, int, int]]] = []
    for segment in sorted(segments, key=lambda item: item[0]):
        if not groups or segment[0] > groups[-1][-1][0] + gap:
            groups.append([segment])
        else:
            groups[-1].append(segment)
    merged: list[tuple[int, int, int]] = []
    for group in groups:
        positions = [item[0] for item in group]
        starts = [item[1] for item in group]
        ends = [item[2] for item in group]
        merged.append((int(round(float(np.median(positions)))), min(starts), max(ends)))
    return merged


def _find_ruled_grids(image: np.ndarray) -> list[_GridCandidate]:
    """Locate local table regions from line *segments*, not page projections.

    A page-wide projection turns a form's client boxes into a fake item table.
    Hough segments retain their vertical extent, so a set of separators that
    overlap in one lower-page region can be scored separately from the rest of
    the invoice.  It also handles monochrome forms whose headers are grey or
    white rather than coloured.
    """
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    height, width = gray.shape[:2]
    if min(height, width) < 100:
        return []
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 35, 110)
    raw = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=max(36, min(height, width) // 3),
        minLineLength=max(40, width // 10),
        maxLineGap=max(5, min(height, width) // 100),
    )
    if raw is None:
        return []
    vertical: list[tuple[int, int, int]] = []
    horizontal: list[tuple[int, int, int]] = []
    for x1, y1, x2, y2 in raw.reshape(-1, 4):
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        if dx <= 4 and dy >= max(35, height // 12):
            vertical.append((int(round((x1 + x2) / 2)), min(y1, y2), max(y1, y2)))
        elif dy <= 4 and dx >= max(60, width // 8):
            horizontal.append((int(round((y1 + y2) / 2)), min(x1, x2), max(x1, x2)))
    vertical = _merge_line_segments(vertical)
    horizontal = _merge_line_segments(horizontal)
    if len(vertical) < 5:
        return []

    candidates: list[_GridCandidate] = []
    seen: set[tuple[int, int, tuple[int, ...]]] = set()
    for _x, start, end in vertical:
        anchor_span = max(1, end - start)
        overlap_lines = [
            line for line in vertical
            if min(end, line[2]) - max(start, line[1]) >= max(32, int(min(anchor_span, line[2] - line[1]) * 0.50))
        ]
        if len(overlap_lines) < 5:
            continue
        top = int(round(float(np.median([line[1] for line in overlap_lines]))))
        bottom = int(round(float(np.median([line[2] for line in overlap_lines]))))
        span = bottom - top
        if span < max(55, int(height * 0.10)):
            continue
        supporting = [
            line for line in overlap_lines
            if min(bottom, line[2]) - max(top, line[1]) >= span * 0.58
        ]
        x_lines = tuple(sorted({line[0] for line in supporting}))
        # A full-page Excel screenshot often has a clipped outer rule.  Its
        # first visible separator is the row-number boundary, not the table's
        # left edge.  Recover that edge only for an actual full-page grid; do
        # not create a fake extra column on a centred invoice table.
        if top <= 6 and bottom >= height - 6:
            expanded = list(x_lines)
            if expanded[0] > width * 0.015:
                expanded.insert(0, 0)
            if expanded[-1] < width * 0.985:
                expanded.append(width - 1)
            x_lines = tuple(expanded)
        if len(x_lines) < 5 or x_lines[-1] - x_lines[0] < width * 0.42:
            continue
        left, right = x_lines[0], x_lines[-1]
        h_lines = [
            line[0]
            for line in horizontal
            if top - 6 <= line[0] <= bottom + 6
            and min(right, line[2]) - max(left, line[1]) >= (right - left) * 0.52
        ]
        # Store the y bounds in the candidate just like the coloured-grid
        # representation.  Extra horizontal lines are added by the row builder
        # below; keeping only the bounds here prevents line-less item rows from
        # disappearing.
        key = (round(top / 8), round(bottom / 8), x_lines)
        if key in seen:
            continue
        seen.add(key)
        score = (
            len(x_lines) * 11.0
            + (right - left) / max(width, 1) * 18.0
            + span / max(height, 1) * 24.0
            + len(h_lines) * 2.0
        )
        candidates.append(_GridCandidate(x_lines, tuple(sorted({top, bottom, *h_lines})), score))
    return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)


def _page_words(pytesseract: Any, image: np.ndarray, *, psm: int = 6) -> list[dict[str, Any]]:
    """Read page words once, at a sane local OCR resolution and with boxes."""
    import cv2

    # Keep this modest: per-cell adaptive scaling happens only after there is
    # layout evidence.  A two-times page pass gives reliable word positions for
    # screenshots without the latency of a full-page 4x OCR pass.
    scaled = cv2.resize(image, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(scaled, cv2.COLOR_RGB2GRAY) if scaled.ndim == 3 else scaled
    # Keep the page view close to the source.  CLAHE is excellent on isolated
    # cells, but on pale spreadsheet rows it amplifies the green watermark and
    # causes Tesseract to join neighbouring records.  The cell reader applies
    # local contrast enhancement later when it is actually needed.
    data = pytesseract.image_to_data(
        gray, lang="ara+eng", config=f"--oem 1 --psm {psm}", output_type=pytesseract.Output.DICT
    )
    words: list[dict[str, Any]] = []
    for index, raw in enumerate(data.get("text", [])):
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][index])
        except (IndexError, TypeError, ValueError):
            confidence = -1.0
        words.append({
            "text": text,
            "conf": confidence,
            "left": int(round(int(data["left"][index]) / 2.0)),
            "top": int(round(int(data["top"][index]) / 2.0)),
            "width": max(1, int(round(int(data["width"][index]) / 2.0))),
            "height": max(1, int(round(int(data["height"][index]) / 2.0))),
        })
    return words


def _arabic_layout_key(value: str) -> str:
    """Small normalisation used only for locating labels, never cell data."""
    return (
        str(value or "").casefold()
        .replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )


def _row_numbers(row: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    values: list[tuple[str, dict[str, Any]]] = []
    for word in row:
        value = _numeric_value(str(word.get("text") or ""))
        if value and _NUMERIC.fullmatch(value):
            values.append((value, word))
    return values


def _value_below_label(
    rows: list[list[dict[str, Any]]],
    row_index: int,
    x: float,
    *,
    date: bool = False,
) -> str:
    label_y = float(np.mean([_word_center(word)[1] for word in rows[row_index]]))
    best: tuple[float, str] | None = None
    for candidate_row in rows[row_index + 1:]:
        candidate_y = float(np.mean([_word_center(word)[1] for word in candidate_row]))
        if candidate_y - label_y > 78:
            break
        for word in candidate_row:
            raw = _tidy(str(word.get("text") or ""))
            value = ""
            if date:
                match = _DATE.search(raw)
                if match:
                    value = normalize_date(match.group(0))
            else:
                value = _numeric_value(raw)
            if not value:
                continue
            distance = abs(_word_center(word)[0] - x) + (candidate_y - label_y) * 0.35
            if best is None or distance < best[0]:
                best = (distance, value)
    return best[1] if best else ""


def _spatial_invoice_context(image: np.ndarray, pytesseract: Any) -> list[str]:
    """Associate invoice labels with nearby values using their page positions.

    Many Arabic templates put labels on one row and values directly below them.
    Plain OCR text loses that relationship, so line-regex extraction previously
    paired labels with other labels.  This produces only high-evidence English
    context lines that the normal invoice parser already understands.
    """
    rows = _word_rows(_page_words(pytesseract, image, psm=6))
    if not rows:
        return []
    invoice_no = invoice_date = due_date = ""
    subtotal = after_discount = grand_total = tax_rate = ""
    currency = ""
    for row_index, row in enumerate(rows):
        text = _arabic_layout_key(" ".join(str(word.get("text") or "") for word in row))
        centers = [_word_center(word) for word in row]
        if not currency and re.search(r"(?:\bsar\b|\u0631\s*[.\u066b]?\s*\u0633)", text, re.I):
            currency = "SAR"
        if "\u0627\u0644\u0627\u0633\u062a\u062d\u0642" in text or re.search(r"\bdue\s*date\b", text, re.I):
            x = float(np.mean([center[0] for center in centers]))
            due_date = due_date or _value_below_label(rows, row_index, x, date=True)
        if "\u062a\u0627\u0631\u064a\u062e" in text and "\u0641\u0627\u062a\u0648\u0631" in text:
            # The date label can share its row with a due-date label.  Use the
            # rightmost invoice-word cluster in RTL templates.
            invoice_words = [word for word in row if "\u0641\u0627\u062a\u0648\u0631" in _arabic_layout_key(str(word.get("text") or ""))]
            if invoice_words:
                x = max(_word_center(word)[0] for word in invoice_words)
                invoice_date = invoice_date or _value_below_label(rows, row_index, x, date=True)
        # A bare ``#`` also appears in item-table headings, where the value
        # underneath is commonly a quantity or taxable amount.  It is an
        # invoice identifier cue only when it shares a row with an invoice
        # label (as in the borderless Arabic template).
        if not invoice_no and "#" in text and (
            "\u0641\u0627\u062a\u0648\u0631" in text or "invoice" in text
        ):
            marker_words = [word for word in row if "#" in str(word.get("text") or "")]
            if marker_words:
                candidate = _value_below_label(rows, row_index, _word_center(marker_words[0])[0])
                if re.fullmatch(r"\d{4,}", candidate or ""):
                    invoice_no = candidate
        numbers = _row_numbers(row)
        amount = numbers[0][0] if numbers else ""
        if amount and "\u0641\u0631\u0639" in text:
            subtotal = amount
        elif amount and "\u0628\u0639\u062f" in text and "\u062e\u0635\u0645" in text:
            after_discount = amount
        elif amount and "\u0643\u0644\u064a" in text:
            grand_total = amount
        elif amount and "\u0636\u0631\u064a\u0628" in text and "%" in text:
            tax_rate = amount
    additions: list[str] = []
    if invoice_no:
        additions.append(f"Invoice No: {invoice_no}")
    if invoice_date:
        additions.append(f"Invoice Date: {invoice_date}")
    if due_date:
        additions.append(f"Due Date: {due_date}")
    if subtotal:
        additions.append(f"Subtotal: {subtotal}")
    if after_discount and grand_total:
        before_tax = _as_number(after_discount)
        final = _as_number(grand_total)
        if before_tax is not None and final is not None and final >= before_tax:
            additions.append(f"Tax Amount: {final - before_tax:.2f}")
    if grand_total:
        additions.append(f"Grand Total: {grand_total}")
    if currency:
        additions.append(f"Currency: {currency}")
    if tax_rate:
        additions.append(f"Tax Rate: {tax_rate}%")
    return additions


def _word_center(word: dict[str, Any]) -> tuple[float, float]:
    return (
        float(word["left"]) + max(1, int(word.get("width") or 1)) / 2.0,
        float(word["top"]) + max(1, int(word.get("height") or 1)) / 2.0,
    )


def _word_rows(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Cluster OCR boxes into visual rows while preserving RTL/Latin tokens."""
    if not words:
        return []
    heights = [max(6, int(word.get("height") or 6)) for word in words]
    # A translucent watermark can create many tall OCR boxes.  Use the lower
    # text-height quartile rather than the median so those boxes do not merge
    # two adjacent spreadsheet records into a single visual row.
    tolerance = max(6.0, float(np.percentile(heights, 25)) * 0.70)
    rows: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: _word_center(item)[1]):
        _x, center_y = _word_center(word)
        if rows:
            previous_y = float(np.mean([_word_center(item)[1] for item in rows[-1]]))
            if abs(center_y - previous_y) <= tolerance:
                rows[-1].append(word)
                continue
        rows.append([word])
    return rows


def _join_words(words: list[dict[str, Any]]) -> str:
    """Join a cell's OCR boxes in the visual direction of its writing system."""
    if not words:
        return ""
    raw = "".join(str(word.get("text") or "") for word in words)
    arabic = sum("\u0600" <= char <= "\u06ff" for char in raw)
    latin = sum(char.isascii() and char.isalpha() for char in raw)
    ordered = sorted(words, key=lambda item: int(item["left"]), reverse=arabic > latin)
    return _tidy(" ".join(str(word.get("text") or "") for word in ordered))


def _row_word_groups(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Merge adjacent OCR boxes which belong to the same visual cell."""
    if not words:
        return []
    ordered = sorted(words, key=lambda item: int(item["left"]))
    heights = [max(6, int(word.get("height") or 6)) for word in ordered]
    gap_limit = max(12, int(np.median(heights) * 1.35))
    groups: list[list[dict[str, Any]]] = [[ordered[0]]]
    right = int(ordered[0]["left"]) + int(ordered[0].get("width") or 1)
    for word in ordered[1:]:
        left = int(word["left"])
        if left <= right + gap_limit:
            groups[-1].append(word)
        else:
            groups.append([word])
        right = max(right, left + int(word.get("width") or 1))
    # Adjacent spreadsheet cells can have no visible gap: a long phone number
    # ends immediately before an email address.  They are semantically
    # different fields, so never merge them just because the gridline is faint.
    split: list[list[dict[str, Any]]] = []
    for group in groups:
        phone = [word for word in group if re.fullmatch(r"\+?\d[\d -]{7,}", str(word.get("text") or ""))]
        email = [word for word in group if "@" in str(word.get("text") or "")]
        if phone and email:
            phone_ids = {id(word) for word in phone}
            email_ids = {id(word) for word in email}
            before = [word for word in group if id(word) in phone_ids]
            after = [word for word in group if id(word) in email_ids]
            other = [word for word in group if id(word) not in phone_ids | email_ids]
            split.extend([before, after])
            if other:
                split.append(other)
        else:
            split.append(group)
    return split


def _group_center(group: list[dict[str, Any]]) -> float:
    left = min(int(word["left"]) for word in group)
    right = max(int(word["left"]) + int(word.get("width") or 1) for word in group)
    return (left + right) / 2.0


def _cell_confidence(words: list[dict[str, Any]]) -> float:
    values = [float(word.get("conf") or -1) for word in words if float(word.get("conf") or -1) >= 0]
    return round(sum(values) / len(values), 1) if values else 0.0


def _map_words_to_grid(
    words: list[dict[str, Any]], grid: list[list[Cell]]
) -> tuple[list[list[str]], list[list[float]]]:
    """Map page-level OCR words to grid cells, retaining actual confidences."""
    buckets: list[list[list[dict[str, Any]]]] = [[[] for _ in row] for row in grid]
    for word in words:
        center_x, center_y = _word_center(word)
        for row_index, row in enumerate(grid):
            if not row:
                continue
            _x, top, _w, height = row[0]
            if not top - 2 <= center_y <= top + height + 2:
                continue
            for col_index, (left, _y, width, _h) in enumerate(row):
                if left - 2 <= center_x <= left + width + 2:
                    buckets[row_index][col_index].append(word)
                    break
            break
    table = [[_join_words(cell) for cell in row] for row in buckets]
    scores = [[_cell_confidence(cell) for cell in row] for row in buckets]
    return table, scores


def _numeric_column(field: str, values: list[str]) -> bool:
    if field in _NUMERIC_FIELDS:
        return True
    filled = [value for value in values if str(value).strip()]
    if len(filled) < 2:
        return False
    numeric = sum(bool(_NUMERIC.fullmatch(_numeric_value(value))) for value in filled)
    return numeric >= max(2, int(len(filled) * 0.70))


def _needs_numeric_reread(raw: str, confidence: float, *, prefer_decimal: bool, median_digits: float) -> bool:
    """Spend a cell OCR pass only when the page-level number looks unsafe."""
    value = _numeric_value(raw)
    if not _NUMERIC.fullmatch(value):
        return True
    digits = len(value.replace("-", "").replace(".", "").replace(",", ""))
    if median_digits and abs(digits - median_digits) >= 2:
        return True
    if prefer_decimal and "." not in value and "," not in value:
        return True
    # A page-level word box can look confident while one digit is wrong (for
    # example 4063.46 instead of 4963.46). Monetary cells below this higher
    # confidence threshold receive the independent cell-level ensemble.
    if prefer_decimal:
        return confidence < 85.0
    return confidence < 52.0


def _semantic_headers_from_columns(headers: list[str], body: list[list[str]]) -> list[str]:
    """Replace only provably garbled labels with a field inferred from values."""
    if not headers or not body:
        return headers
    out = list(headers)
    width = len(out)
    for column in range(width):
        values = [str(row[column]).strip() for row in body if column < len(row) and str(row[column]).strip()]
        if len(values) < 2:
            continue
        field = canonical_header(out[column])
        email_hits = sum("@" in value and "." in value.rsplit("@", 1)[-1] for value in values)
        phone_hits = sum(bool(re.fullmatch(r"\+?\d[\d -]{7,}", value)) for value in values)
        if email_hits >= max(2, int(len(values) * 0.65)) and field != "email":
            out[column] = "email"
        elif phone_hits >= max(2, int(len(values) * 0.65)) and field != "phone":
            out[column] = "phone"
    # The first narrow gutter of a spreadsheet is often unlabeled or OCRed as
    # a decoration.  Consecutive integers make its meaning unambiguous.
    first = [str(row[0]).strip() for row in body if row and str(row[0]).strip()]
    if len(first) >= 3 and all(re.fullmatch(r"\d+", value) for value in first):
        numbers = [int(value) for value in first]
        if sum(numbers[index + 1] - numbers[index] == 1 for index in range(len(numbers) - 1)) >= max(3, int((len(numbers) - 1) * 0.80)):
            out[0] = "row_number"
    return out


def _explicit_invoice_amount_columns(headers: list[str]) -> dict[str, int]:
    """Map explicit tax-invoice headings without guessing from cell position."""
    fields = [canonical_header(value) for value in headers]
    roles: dict[str, int] = {}
    for index, (header, field) in enumerate(zip(headers, fields)):
        key = str(header or "").casefold()
        if field == "qty" and "qty" not in roles:
            roles["qty"] = index
        elif field == "unit_price" and "unit_price" not in roles:
            roles["unit_price"] = index
        if "%" in key and (
            re.search(r"(?:vat|tax|ضريب)", key, re.I)
            or "unit_price" in roles
        ):
            roles.setdefault("tax_rate", index)
        elif re.search(r"taxable", key, re.I):
            roles.setdefault("taxable", index)
        elif re.search(r"\bvat\b|ضريب", key, re.I) and "%" not in key:
            roles.setdefault("vat", index)
        if re.search(r"(?:\btotal\b|الإجمالي|المبلغ)", key, re.I):
            roles.setdefault("total", index)
    return roles


def _reconcile_explicit_invoice_amounts(
    table: list[list[str]],
    scores: list[list[float]],
) -> bool:
    """Calculate corroborated tax columns for a clearly-labelled invoice.

    This is not decimal guessing. It is enabled only when the image explicitly
    contains Qty, Rate, Tax %, Taxable, VAT and Total columns, and the first
    three values are independently OCRed. Reconstructed cells retain a low
    confidence score, which keeps them yellow in the generic review workbook.
    """
    if len(table) < 2:
        return False
    roles = _explicit_invoice_amount_columns(table[0])
    required = {"qty", "unit_price", "tax_rate", "taxable", "vat", "total"}
    if not required.issubset(roles):
        return False
    changed = False
    for row_index, row in enumerate(table[1:], start=1):
        if max(roles.values()) >= len(row):
            continue
        quantity = _as_number(row[roles["qty"]])
        rate = _as_number(row[roles["unit_price"]])
        tax_rate = _as_number(row[roles["tax_rate"]])
        if quantity is None or rate is None or tax_rate is None:
            continue
        if not (quantity > 0 and rate > 0 and 0 <= tax_rate <= 25):
            continue
        calculated = {
            "taxable": round(quantity * rate, 2),
            "vat": round(quantity * rate * tax_rate / 100.0, 2),
        }
        calculated["total"] = round(calculated["taxable"] + calculated["vat"], 2)
        for role, expected in calculated.items():
            column = roles[role]
            observed = _as_number(row[column])
            # The calculation and image evidence must agree to the cent. A
            # wider tolerance would preserve the common OCR 2998.60/2998.80
            # one-pixel error as though it were a verified monetary amount.
            if observed is not None and abs(observed - expected) <= 0.05:
                continue
            row[column] = f"{expected:.2f}"
            if row_index < len(scores) and column < len(scores[row_index]):
                scores[row_index][column] = min(float(scores[row_index][column] or 0), 55.0)
            changed = True
    return changed


def _normalise_explicit_invoice_headers(headers: list[str], body: list[list[str]]) -> list[str]:
    """Give a fully evidenced tax-invoice grid stable Excel column names."""
    roles = _explicit_invoice_amount_columns(headers)
    required = {"qty", "unit_price", "tax_rate", "taxable", "vat", "total"}
    if not required.issubset(roles):
        return headers
    out = list(headers)
    labels = {
        "qty": "Qty",
        "unit_price": "Rate",
        "tax_rate": "VAT %",
        "taxable": "Taxable Value",
        "vat": "VAT",
        "total": "Total incl. VAT",
    }
    for role, label in labels.items():
        out[roles[role]] = label
    for index, header in enumerate(out):
        if canonical_header(headers[index]) == "description":
            out[index] = "Description"
    if roles["unit_price"] + 1 < roles["tax_rate"]:
        middle = roles["unit_price"] + 1
        if middle < len(out) and middle not in roles.values():
            out[middle] = "Unit"
    if body and all(row and re.fullmatch(r"\d+", str(row[0]).strip()) for row in body):
        out[0] = "Line No."
    return out


def _repair_sequential_first_column(table: list[list[str]], scores: list[list[float]]) -> None:
    """Correct an isolated clipped row number from the surrounding sequence."""
    if len(table) < 6:
        return
    values = [str(row[0]).strip() if row else "" for row in table[1:]]
    if not all(re.fullmatch(r"\d+", value) for value in values):
        return
    start = int(values[0])
    expected = [start + index for index in range(len(values))]
    matches = sum(int(value) == target for value, target in zip(values, expected))
    if matches < max(4, int(len(values) * 0.80)):
        return
    for offset, target in enumerate(expected, start=1):
        if int(str(table[offset][0])) == target:
            continue
        table[offset][0] = str(target)
        if offset < len(scores) and scores[offset]:
            # It is a verified structural correction, but retain the review
            # signal rather than pretending the clipped OCR glyph was certain.
            scores[offset][0] = min(float(scores[offset][0] or 0), 55.0)


def _serial_item_body_bounds(
    words: list[dict[str, Any]],
    left: int,
    right: int,
    header_bottom: int,
    bottom: int,
) -> list[int]:
    """Return logical item-row endings from a narrow serial-number gutter.

    Bilingual invoices sometimes place Arabic text directly beneath the English
    line of the same item.  Treating every OCR baseline as a new Excel row
    makes a clean two-item invoice look like four broken items.  Consecutive,
    high-confidence serials in the left gutter are stronger evidence than the
    baseline spacing, so they define the logical row boundaries.
    """
    candidates: list[tuple[int, float, float]] = []
    for word in words:
        center_x, center_y = _word_center(word)
        if not (left - 3 <= center_x <= right + 3 and header_bottom + 2 < center_y < bottom - 2):
            continue
        value = _numeric_value(str(word.get("text") or ""))
        if not re.fullmatch(r"\d{1,4}", value):
            continue
        confidence = float(word.get("conf") or 0)
        if confidence < 60.0:
            continue
        candidates.append((int(value), center_y, confidence))
    candidates.sort(key=lambda item: item[1])
    best: list[tuple[int, float, float]] = []
    for start, candidate in enumerate(candidates):
        sequence = [candidate]
        expected = candidate[0] + 1
        for following in candidates[start + 1:]:
            if following[1] - sequence[-1][1] < 8:
                continue
            if following[0] == expected:
                sequence.append(following)
                expected += 1
        if len(sequence) > len(best) or (
            len(sequence) == len(best)
            and sequence
            and best
            and sum(item[2] for item in sequence) > sum(item[2] for item in best)
        ):
            best = sequence
    if len(best) < 2:
        return []
    gaps = [best[index + 1][1] - best[index][1] for index in range(len(best) - 1)]
    expected_gap = float(np.median(gaps)) if gaps else 24.0
    endings: list[int] = []
    for index, (_number, center_y, _confidence) in enumerate(best):
        if index + 1 < len(best):
            end = int(round((center_y + best[index + 1][1]) / 2.0))
        else:
            end = min(bottom, int(round(center_y + expected_gap / 2.0)))
        if end > header_bottom + 7 and (not endings or end - endings[-1] >= 8):
            endings.append(end)
    return endings if len(endings) >= 2 else []


def _cells_from_grid_lines(x_lines: list[int], y_lines: list[int]) -> list[list[Cell]]:
    """Build non-degenerate cells from already validated row/column rules."""
    rows: list[list[Cell]] = []
    for row_index in range(len(y_lines) - 1):
        row_top, row_bottom = y_lines[row_index], y_lines[row_index + 1]
        if row_bottom - row_top < 8:
            continue
        rows.append([
            (x_lines[column], row_top, x_lines[column + 1] - x_lines[column], row_bottom - row_top)
            for column in range(len(x_lines) - 1)
            if x_lines[column + 1] - x_lines[column] >= 10
        ])
    return [row for row in rows if len(row) >= 3]


def _serial_baseline_for_row(words: list[dict[str, Any]], row: list[Cell]) -> float | None:
    """Find the primary text baseline of an item row from its serial gutter."""
    if not row:
        return None
    x, y, width, height = row[0]
    choices: list[tuple[float, float]] = []
    for word in words:
        center_x, center_y = _word_center(word)
        if not (x - 3 <= center_x <= x + width + 3 and y - 2 <= center_y <= y + height + 2):
            continue
        value = _numeric_value(str(word.get("text") or ""))
        if re.fullmatch(r"\d{1,4}", value) and float(word.get("conf") or 0) >= 60.0:
            choices.append((float(word.get("conf") or 0), center_y))
    return max(choices, default=(0.0, 0.0))[1] or None


def _cell_at_primary_baseline(cell: Cell, baseline_y: float | None) -> Cell:
    """Crop one logical bilingual row to its main Latin/numeric baseline."""
    if baseline_y is None:
        return cell
    x, y, width, height = cell
    # Bias the crop upward: in bilingual invoices the second-script
    # continuation begins just below the primary English/numeric baseline.
    top = max(y, int(round(baseline_y - 12)))
    bottom = min(y + height, int(round(baseline_y + 8)))
    return (x, top, width, max(1, bottom - top))


def _numeric_word_at_primary_baseline(
    words: list[dict[str, Any]],
    cell: Cell,
    baseline_y: float | None,
    *,
    prefer_decimal: bool,
) -> tuple[str, float] | None:
    """Reuse a high-confidence page word before re-OCRing a merged cell."""
    if baseline_y is None:
        return None
    x, y, width, height = cell
    choices: list[tuple[float, str, float]] = []
    for word in words:
        center_x, center_y = _word_center(word)
        if not (x - 2 <= center_x <= x + width + 2 and y - 2 <= center_y <= y + height + 2):
            continue
        if abs(center_y - baseline_y) > 8:
            continue
        value = _numeric_value(str(word.get("text") or ""))
        confidence = float(word.get("conf") or 0)
        if not _NUMERIC.fullmatch(value) or confidence < 60.0:
            continue
        if prefer_decimal and len(value.replace("-", "").replace(".", "").replace(",", "")) < 3:
            # A lone trailing fragment such as ``40`` from ``1,187.40`` is
            # not an independent monetary value. Let the cell ensemble read
            # the whole primary baseline instead.
            continue
        score = confidence + (8.0 if prefer_decimal and ("." in value or "," in value) else 0.0)
        choices.append((score, value, confidence))
    if not choices:
        return None
    _score, value, confidence = max(choices, key=lambda item: (item[0], item[1]))
    return value, confidence


def _rows_for_ruled_grid(
    candidate: _GridCandidate,
    words: list[dict[str, Any]],
) -> list[list[Cell]]:
    """Build rows from real rules when present, otherwise from repeated words."""
    x_lines = list(candidate.x_lines)
    if len(x_lines) < 3 or len(candidate.y_lines) < 2:
        return []
    # Hough can preserve a very narrow strip on either outer border as a fake
    # column.  Removing it before cell OCR prevents the real first/last
    # columns of a dense invoice from being shifted by one cell.
    if len(x_lines) >= 7:
        outer_limit = max(12, int(round((x_lines[-1] - x_lines[0]) * 0.025)))
        if x_lines[1] - x_lines[0] <= outer_limit:
            x_lines.pop(0)
        if len(x_lines) >= 7 and x_lines[-1] - x_lines[-2] <= outer_limit:
            x_lines.pop(-2)
    top, bottom = min(candidate.y_lines), max(candidate.y_lines)
    horizontal = sorted({line for line in candidate.y_lines if top <= line <= bottom})
    # Hough finds each one-pixel rule twice.  The candidate builder has already
    # merged most of them, but keep one reasonable representative here too.
    compact: list[int] = []
    for line in horizontal:
        if not compact or line - compact[-1] >= 7:
            compact.append(line)
        else:
            compact[-1] = int(round((compact[-1] + line) / 2.0))
    if not compact or compact[0] > top + 6:
        compact.insert(0, top)
    if compact[-1] < bottom - 6:
        compact.append(bottom)
    # A dense spreadsheet has genuine horizontal rules for every row.  Keep
    # them.  In a conventional invoice only the header and outer border are
    # ruled, so infer item row bounds from its aligned text instead.
    gaps = [compact[index + 1] - compact[index] for index in range(len(compact) - 1)]
    positive_gaps = [gap for gap in gaps if gap >= 7]
    median_gap = float(np.median(positive_gaps)) if positive_gaps else 0.0
    regular_gaps = sum(0.55 * median_gap <= gap <= 1.8 * median_gap for gap in positive_gaps) if median_gap else 0
    dense_rules = (
        len(compact) >= 6
        and len(positive_gaps) >= 5
        and regular_gaps >= max(4, int(len(positive_gaps) * 0.65))
        and max(positive_gaps) <= max(50.0, median_gap * 2.5)
    )
    if dense_rules:
        y_lines = compact
    else:
        header_bottom = next((line for line in compact[1:] if line > top + 7), min(bottom, top + 28))
        interior = [
            word for word in words
            if x_lines[0] - 3 <= _word_center(word)[0] <= x_lines[-1] + 3
            and header_bottom + 2 < _word_center(word)[1] < bottom - 2
        ]
        text_rows = _word_rows(interior)
        centers = [float(np.mean([_word_center(word)[1] for word in row])) for row in text_rows if row]
        if not centers:
            return []
        serial_endings = []
        # This is deliberately limited to a multi-column invoice with a
        # narrow first gutter. Ordinary tables use their OCR baselines below.
        if len(x_lines) >= 8 and x_lines[1] - x_lines[0] <= max(28, int((x_lines[-1] - x_lines[0]) * 0.06)):
            serial_endings = _serial_item_body_bounds(
                interior, x_lines[0], x_lines[1], header_bottom, bottom
            )
        if serial_endings:
            return _cells_from_grid_lines(x_lines, [top, header_bottom, *serial_endings])
        text_gaps = [
            centers[index + 1] - centers[index]
            for index in range(len(centers) - 1)
            if 5 < centers[index + 1] - centers[index] < 80
        ]
        expected_gap = float(np.median(text_gaps)) if text_gaps else 24.0
        coherent_centers: list[float] = []
        for center in centers:
            if coherent_centers and center - coherent_centers[-1] > max(44.0, expected_gap * 2.8):
                # Totals/footer text after an item grid has a much larger gap;
                # it belongs in page context, not as a fabricated line item.
                break
            coherent_centers.append(center)
        centers = coherent_centers
        body_bottom = min(bottom, int(round(centers[-1] + expected_gap / 2.0)))
        y_lines = [top, header_bottom]
        previous = header_bottom
        for index, center in enumerate(centers):
            if center <= header_bottom + 1:
                continue
            next_center = centers[index + 1] if index + 1 < len(centers) else body_bottom
            boundary = int(round((center + next_center) / 2.0))
            if boundary - previous >= 8 and boundary < body_bottom - 4:
                y_lines.append(boundary)
                previous = boundary
        if body_bottom - previous >= 8:
            y_lines.append(body_bottom)
    return _cells_from_grid_lines(x_lines, y_lines)


def _analyze_ruled_grid(image: np.ndarray, pytesseract: Any, *, include_page_text: bool) -> LocalAIResult | None:
    words = _page_words(pytesseract, image, psm=6)
    for candidate in _find_ruled_grids(image)[:4]:
        grid = _rows_for_ruled_grid(candidate, words)
        if len(grid) < 3 or len(grid[0]) < 4:
            continue
        page_blob = "".join(str(word.get("text") or "") for word in words)
        arabic_glyphs = sum("\u0600" <= character <= "\u06ff" for character in page_blob)
        latin_glyphs = sum(character.isascii() and character.isalpha() for character in page_blob)
        header_language = "eng" if latin_glyphs >= 20 and latin_glyphs > arabic_glyphs * 3 else "ara+eng"
        header_values_and_scores = [
            _read_header_cell(pytesseract, cell, image, language=header_language) for cell in grid[0]
        ]
        header_values = [item[0] for item in header_values_and_scores]
        header_scores = [item[1] for item in header_values_and_scores]
        fields = [canonical_header(value) for value in header_values]
        header_values, fields, header_scores = _infer_compact_invoice_headers(
            header_values, fields, header_scores
        )
        known = sum(field in _STRUCTURED_FIELDS for field in fields)
        compact_headers = sum(bool(value.strip()) for value in header_values)
        # Unknown English spreadsheet headings (STATE / P_CAP / …) are valid
        # only on a sufficiently dense, regular grid.  A sparse form must show
        # field evidence before we export it as a table.
        dense_grid = len(grid) >= 7 and len(grid[0]) >= 5
        if _header_looks_like_data(header_values):
            continue
        if not dense_grid and known < 3:
            continue
        if dense_grid and known < 2 and compact_headers < max(3, len(grid[0]) // 2):
            continue
        item_fields = set(fields) & {"description", "qty", "unit_price", "total"}
        # Do not promote a partial invoice grid.  It is safer to let the
        # generic path retain raw text for review than to assign a description
        # or price to the wrong physical column.
        if len(item_fields) >= 3 and not _has_complete_invoice_schema(fields):
            continue
        mapped, mapped_scores = _map_words_to_grid(words, grid)
        # A narrow, otherwise blank first header over consecutive integers is
        # the row-number gutter of a spreadsheet, not an unnamed data field.
        if not header_values[0].strip():
            first_values = [row[0] for row in mapped[1:] if row and row[0].strip()]
            if len(first_values) >= 3 and sum(bool(re.fullmatch(r"\d+", value)) for value in first_values) >= len(first_values) * 0.8:
                header_values[0] = "row_number"
                header_scores[0] = 100.0
                fields[0] = "row_number"
        numeric_columns = {
            index for index, field in enumerate(fields)
            if (
                _numeric_column(field, [row[index] for row in mapped[1:] if index < len(row)])
                or _looks_like_money_header(header_values[index])
            )
        }
        decimal_columns = {
            index for index in numeric_columns
            if any(
                "." in _numeric_value(row[index]) or "," in _numeric_value(row[index])
                for row in mapped[1:] if index < len(row)
            )
        }
        # Common abbreviated spreadsheet headings whose samples use decimal
        # values even when the first OCR pass loses every decimal point.
        decimal_columns.update(
            index for index, value in enumerate(header_values)
            if _looks_like_money_header(value)
            or re.search(r"(?:p_?cap|hwy|water|util|pc|emp|unemp|capital)", value, re.I)
        )
        digit_medians: dict[int, float] = {}
        for index in numeric_columns:
            lengths = [
                len(_numeric_value(row[index]).replace("-", "").replace(".", "").replace(",", ""))
                for row in mapped[1:] if index < len(row) and _NUMERIC.fullmatch(_numeric_value(row[index]))
            ]
            if lengths:
                digit_medians[index] = float(np.median(lengths))
        arabic_page = sum("\u0600" <= character <= "\u06ff" for value in header_values for character in value) >= 2
        table: list[list[str]] = [header_values]
        scores: list[list[float]] = [header_scores]
        empty_rows = 0
        for row_index, row in enumerate(grid[1:], start=1):
            original = mapped[row_index] if row_index < len(mapped) else [""] * len(row)
            original_scores = mapped_scores[row_index] if row_index < len(mapped_scores) else [0.0] * len(row)
            if not any(str(value).strip() for value in original) and not _row_has_text_ink(image, row):
                empty_rows += 1
                if empty_rows >= 1:
                    break
                continue
            empty_rows = 0
            values: list[str] = []
            confidences: list[float] = []
            # A serial-defined logical row can contain an Arabic continuation
            # underneath the English item line. Read money cells only on the
            # primary baseline so the second script cannot turn ``12`` into
            # ``1211`` or join two unrelated amounts.
            baseline_y = _serial_baseline_for_row(words, row) if not dense_grid and len(row) >= 8 else None
            for column, cell in enumerate(row):
                raw = original[column] if column < len(original) else ""
                raw_confidence = original_scores[column] if column < len(original_scores) else 0.0
                if column in numeric_columns:
                    numeric_cell = _cell_at_primary_baseline(cell, baseline_y)
                    primary_word = _numeric_word_at_primary_baseline(
                        words,
                        cell,
                        baseline_y,
                        prefer_decimal=column in decimal_columns,
                    )
                    if primary_word is not None:
                        text, confidence = primary_word
                    elif _needs_numeric_reread(
                        raw,
                        raw_confidence,
                        prefer_decimal=column in decimal_columns,
                        median_digits=digit_medians.get(column, 0.0),
                    ):
                        text, confidence = _read_body_cell(
                            pytesseract,
                            numeric_cell,
                            image,
                            fields[column],
                            arabic_page=False,
                            numeric_override=True,
                            prefer_decimal=column in decimal_columns,
                        )
                        # If a re-read has no evidence, preserve the page pass
                        # rather than turning a real value into a blank cell.
                        if not text and raw:
                            text, confidence = raw, raw_confidence
                    else:
                        text, confidence = _numeric_value(raw), raw_confidence
                elif raw.strip() and raw_confidence >= 46.0:
                    text, confidence = raw, raw_confidence
                else:
                    text, confidence = _read_body_cell(
                        pytesseract, cell, image, fields[column], arabic_page=arabic_page
                    )
                    if not text and raw:
                        text, confidence = raw, raw_confidence
                values.append(_tidy(text))
                confidences.append(confidence)
            if any(value.strip() for value in values):
                table.append(values)
                scores.append(confidences)
        if len(table) >= 3:
            _repair_sequential_first_column(table, scores)
            table[0] = _semantic_headers_from_columns(table[0], table[1:])
            table[0] = _normalise_explicit_invoice_headers(table[0], table[1:])
            reconstructed_amounts = _reconcile_explicit_invoice_amounts(table, scores)
            context_fields = [canonical_header(value) for value in table[0]]
            context = _page_context(image, pytesseract, context_fields) if include_page_text else ""
            return LocalAIResult(
                table=table,
                scores=scores,
                page_context=context,
                method="local-ai-ruled-grid",
                # Reconstructed tax amounts are valuable, but remain a
                # reviewable generic table until a human accepts them.
                kind="generic_table" if reconstructed_amounts else "invoice",
            )
    return None


def _word_layout_table(
    words: list[dict[str, Any]],
) -> tuple[list[list[str]], list[list[float]], list[float], list[list[list[dict[str, Any]]]]] | None:
    """Recover a borderless spreadsheet/invoice from repeated aligned words."""
    rows = _word_rows(words)
    best: tuple[
        float,
        list[list[str]],
        list[list[float]],
        list[float],
        list[list[list[dict[str, Any]]]],
    ] | None = None
    row_centers = [float(np.mean([_word_center(word)[1] for word in row])) for row in rows]
    for header_index, header_row in enumerate(rows[:-2]):
        groups = _row_word_groups(header_row)
        if len(groups) < 3:
            continue
        headers = [_join_words(group) for group in groups]
        fields = [canonical_header(value) for value in headers]
        known = sum(field in _STRUCTURED_FIELDS for field in fields)
        item_fields = sum(field in _NUMERIC_FIELDS | {"description"} for field in fields)
        if known < 2:
            continue
        centers = [_group_center(group) for group in groups]
        deltas = [
            row_centers[index + 1] - row_centers[index]
            for index in range(header_index, min(len(rows) - 1, header_index + 7))
            if 5 < row_centers[index + 1] - row_centers[index] < 80
        ]
        expected_gap = float(np.median(deltas)) if deltas else 24.0
        body_groups: list[list[list[dict[str, Any]]]] = []
        previous_y = row_centers[header_index]
        for row_index in range(header_index + 1, min(len(rows), header_index + 61)):
            gap = row_centers[row_index] - previous_y
            if gap > max(42.0, expected_gap * 2.8):
                break
            grouped = _row_word_groups(rows[row_index])
            if len(grouped) >= max(2, min(3, len(groups) // 2)):
                body_groups.append(grouped)
            previous_y = row_centers[row_index]
        if len(body_groups) < 2:
            continue
        table = [headers]
        scores = [[_cell_confidence(group) for group in groups]]
        body_buckets: list[list[list[dict[str, Any]]]] = []
        for grouped in body_groups:
            buckets: list[list[dict[str, Any]]] = [[] for _ in centers]
            for group in grouped:
                column = int(np.argmin([abs(_group_center(group) - center) for center in centers]))
                buckets[column].extend(group)
            values = [_join_words(bucket) for bucket in buckets]
            for column, field in enumerate(fields):
                if field in _NUMERIC_FIELDS and column < len(values):
                    numeric = _numeric_value(values[column])
                    if _NUMERIC.fullmatch(numeric):
                        values[column] = numeric
            table.append(values)
            scores.append([_cell_confidence(bucket) for bucket in buckets])
            body_buckets.append(buckets)
        score = known * 5.0 + item_fields * 15.0 + len(body_groups) * 2.0 + len(groups)
        if best is None or score > best[0]:
            best = (score, table, scores, centers, body_buckets)
    if best is None:
        return None
    table, scores, centers, body_buckets = best[1], best[2], best[3], best[4]
    _repair_sequential_first_column(table, scores)
    table[0] = _semantic_headers_from_columns(table[0], table[1:])
    return table, scores, centers, body_buckets


_FORM_SPECS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("Phone", ("phone",), "phone"),
    ("Fax", ("fax",), "phone"),
    ("P.O. Box", ("pobox",), "text"),
    ("Tax Registration No.", ("taxreg", "taxregistration"), "number"),
    ("Mobile", ("mobile",), "phone"),
    ("Car Number", ("carnumber",), "number"),
    ("Car Name", ("carname",), "text"),
    ("Contract No.", ("contractno",), "text"),
    ("Contract Out Date", ("contractoutdate",), "date"),
    ("Contract In Date", ("contractindate",), "date"),
    ("Rent Day Rate", ("rentdayrate",), "number"),
)


def _latin_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _form_value_from_row(
    row: list[dict[str, Any]],
    label_key: str,
    value_kind: str,
) -> tuple[str, float] | None:
    """Read the value immediately following a left-to-right form label."""
    ordered = sorted(row, key=lambda word: int(word["left"]))
    keys = [_latin_key(str(word.get("text") or "")) for word in ordered]
    joined = "".join(keys)
    position = joined.find(label_key)
    if position < 0:
        return None
    consumed = 0
    end_index = -1
    for index, key in enumerate(keys):
        consumed += len(key)
        if consumed >= position + len(label_key):
            end_index = index
            break
    if end_index < 0:
        return None
    candidates = ordered[end_index + 1:]
    if not candidates:
        return None
    if value_kind == "date":
        for word in candidates:
            value = _tidy(str(word.get("text") or ""))
            if _DATE.search(value):
                return normalize_date(_DATE.search(value).group(0)), float(word.get("conf") or 0)
        return None
    if value_kind in {"number", "phone"}:
        pattern = r"\+?[\d][\d./-]{2,}" if value_kind == "phone" else r"[A-Z]*\d[\w./-]*"
        for word in candidates:
            value = _tidy(str(word.get("text") or ""))
            match = re.search(pattern, value, re.I)
            if match:
                return match.group(0), float(word.get("conf") or 0)
        return None
    # Text values such as "KIA" follow the label in a separate form cell.
    for word in candidates:
        value = _tidy(str(word.get("text") or ""))
        if value and _latin_key(value) not in {"office", "fax", "address", "attention"}:
            return value, float(word.get("conf") or 0)
    return None


def _analyze_form_layout(image: np.ndarray, pytesseract: Any, *, include_page_text: bool) -> LocalAIResult | None:
    """Extract ruled key/value forms which do not contain item line tables."""
    words = _page_words(pytesseract, image, psm=6)
    fields: list[tuple[str, str, float]] = []
    seen: set[str] = set()
    for row in _word_rows(words):
        # The English half of bilingual forms has more reliable labels and
        # avoids writing the same physical field twice.
        if not any(str(word.get("text") or "").isascii() for word in row):
            continue
        for label, label_keys, value_kind in _FORM_SPECS:
            if label in seen:
                continue
            value: tuple[str, float] | None = None
            for label_key in label_keys:
                value = _form_value_from_row(row, label_key, value_kind)
                if value is not None:
                    break
            if value is None or not value[0].strip():
                continue
            fields.append((label, value[0], value[1]))
            seen.add(label)
    if len(fields) < 3:
        return None
    table = [["Field", "Value"], *[[label, value] for label, value, _score in fields]]
    scores = [[100.0, 100.0], *[[100.0, score] for _label, _value, score in fields]]
    context = _page_context(image, pytesseract, []) if include_page_text else ""
    return LocalAIResult(
        table=table,
        scores=scores,
        page_context=context,
        method="local-ai-form:key-value",
        kind="form",
    )


def _refine_word_layout_descriptions(
    image: np.ndarray,
    pytesseract: Any,
    table: list[list[str]],
    scores: list[list[float]],
    centers: list[float],
    body_buckets: list[list[list[dict[str, Any]]]],
) -> None:
    """Re-read only weak free-text cells in an otherwise proven word layout."""
    if not table or not centers or not body_buckets:
        return
    fields = [canonical_header(value) for value in table[0]]
    try:
        description_column = fields.index("description")
    except ValueError:
        return
    if description_column >= len(centers):
        return
    height, width = image.shape[:2]
    left = 0 if description_column == 0 else int(round((centers[description_column - 1] + centers[description_column]) / 2.0))
    if description_column + 1 < len(centers):
        right = int(round((centers[description_column] + centers[description_column + 1]) / 2.0))
    else:
        previous_gap = centers[description_column] - centers[description_column - 1] if description_column else width / 2
        right = int(round(centers[description_column] + previous_gap * 0.65))
    left, right = max(0, left), min(width, max(left + 12, right))
    for body_index, buckets in enumerate(body_buckets, start=1):
        if body_index >= len(table) or description_column >= len(buckets):
            continue
        old_score = (
            float(scores[body_index][description_column] or 0)
            if body_index < len(scores) and description_column < len(scores[body_index])
            else 0.0
        )
        if old_score >= 70.0:
            continue
        row_words = [word for column in buckets for word in column]
        if not row_words:
            continue
        top = max(0, min(int(word["top"]) for word in row_words) - 7)
        bottom = min(height, max(int(word["top"]) + int(word["height"]) for word in row_words) + 7)
        if bottom - top < 8:
            continue
        cell = (left, top, right - left, bottom - top)
        gray, binary = _cell_image(image, cell, scale=2.5, padding=1)
        candidates = [
            _ocr(pytesseract, gray, lang="ara+eng", psm=6),
            _ocr(pytesseract, gray, lang="ara+eng", psm=11),
            _ocr(pytesseract, binary, lang="ara+eng", psm=6),
            _ocr(pytesseract, binary, lang="ara+eng", psm=11),
        ]
        text, confidence = max(candidates, key=lambda item: _candidate_score(*item))
        text = _tidy(text)
        if text and confidence >= max(70.0, old_score + 15.0):
            table[body_index][description_column] = text
            if body_index < len(scores) and description_column < len(scores[body_index]):
                scores[body_index][description_column] = confidence


def _analyze_word_layout(image: np.ndarray, pytesseract: Any, *, include_page_text: bool) -> LocalAIResult | None:
    # PSM 6 preserves spreadsheet rows far better than sparse-text PSM 11 on
    # faint Excel grids, while still reads the clean borderless invoice layout.
    words = _page_words(pytesseract, image, psm=6)
    result = _word_layout_table(words)
    if result is None:
        return None
    table, scores, centers, body_buckets = result
    _refine_word_layout_descriptions(image, pytesseract, table, scores, centers, body_buckets)
    fields = [canonical_header(value) for value in table[0]] if table else []
    context = _page_context(image, pytesseract, fields) if include_page_text else ""
    return LocalAIResult(table=table, scores=scores, page_context=context, method="local-ai-word-layout")


def analyze_image(image: np.ndarray, pytesseract: Any, *, include_page_text: bool = False) -> LocalAIResult | None:
    """Run the local layout/OCR ensemble before the generic compatibility path.

    The order is intentional: coloured invoice grids are the strongest cue;
    ruled monochrome grids come next; and word alignment is the conservative
    fallback for faint Excel lines or borderless templates.
    """
    if image.ndim != 3 or min(image.shape[:2]) < 120:
        return None
    analysers = (_analyze_coloured_grid, _analyze_ruled_grid, _analyze_word_layout, _analyze_form_layout)
    for analyser in analysers:
        result = analyser(image, pytesseract, include_page_text=include_page_text)
        if result is None:
            continue
        if result.kind != "generic_table" and _local_result_is_safe(result):
            return result
        if result.kind == "form":
            return result
        if _local_result_is_structurally_useful(result):
            # Preserve real table geometry but make its downstream intent
            # explicit. The exporter will produce a reviewable generic sheet
            # rather than guessing Header/Items/Totals from unsafe values.
            if result.kind != "generic_table":
                result = _remove_unsupported_generic_numbers(result)
            return replace(
                result,
                method=f"local-ai-generic-table:{result.method}",
                kind="generic_table",
            )
    return None
