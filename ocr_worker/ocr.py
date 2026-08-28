"""Tesseract helpers and page rasterization."""
from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from common import CONFIDENCE_THRESHOLD
from local_ai import analyze_image as analyze_with_local_ai
from preprocess import detect_mode, drop_color_noise, enhance_gray, preprocess
from table_detect import assign_words_to_grid, cluster_words_to_table, detect_cells, detect_cells_from_projections


def setup_tesseract() -> Any:
    try:
        import pytesseract
        command = os.environ.get("TESSERACT_CMD")
        if command:
            pytesseract.pytesseract.tesseract_cmd = command
        pytesseract.get_tesseract_version()
        return pytesseract
    except Exception as error:
        raise RuntimeError(
            "محرك Tesseract المضمّن غير متاح. أعد تثبيت Excel Clear."
        ) from error


def image_pages(source: Path) -> Iterable[np.ndarray]:
    from PIL import Image
    if source.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(str(source))
        try:
            for index in range(len(document)):
                bitmap = document[index].render(scale=2.5)
                yield np.array(bitmap.to_pil().convert("RGB"))
        finally:
            document.close()
        return
    with Image.open(source) as image:
        frames = getattr(image, "n_frames", 1)
        for index in range(frames):
            image.seek(index)
            yield np.array(image.convert("RGB"))


def pdf_text_pages(source: Path) -> tuple[bool, list[str]]:
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(str(source))
    pages: list[str] = []
    characters = 0
    try:
        for index in range(len(document)):
            page = document[index].get_textpage()
            text = page.get_text_bounded() or ""
            pages.append(text)
            characters += len(text.strip())
    finally:
        document.close()
    return characters >= 80, pages


def _confidence(value: Any) -> float:
    text = str(value)
    if text.replace(".", "", 1).lstrip("-").isdigit():
        return float(text)
    return -1.0


def ocr_cell(pytesseract: Any, crop: np.ndarray, *, invert_dark_bg: bool = False, lang: str = "ara+eng") -> tuple[str, float]:
    work = crop
    if work.size:
        import cv2
        # PIL/OpenCV images entering this worker are normalised to RGB.
        gray = work if work.ndim == 2 else cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
        if invert_dark_bg or float(np.mean(gray)) < 125:
            work = 255 - gray
        else:
            work = gray
    data = pytesseract.image_to_data(
        work, lang=lang, config="--oem 1 --psm 7", output_type=pytesseract.Output.DICT
    )
    words, scores = [], []
    for index, raw in enumerate(data["text"]):
        word = str(raw or "").strip()
        score = _confidence(data["conf"][index])
        if word:
            words.append(word)
            if score >= 0:
                scores.append(score)
    return " ".join(words), round(sum(scores) / len(scores), 1) if scores else 0.0


def ocr_words(pytesseract: Any, image: np.ndarray, lang: str = "ara+eng") -> list[dict[str, Any]]:
    data = pytesseract.image_to_data(
        image, lang=lang, config="--oem 1 --psm 6", output_type=pytesseract.Output.DICT
    )
    words = []
    for index, raw in enumerate(data["text"]):
        word = str(raw or "").strip()
        if not word:
            continue
        words.append({
            "text": word,
            "conf": _confidence(data["conf"][index]),
            "left": int(data["left"][index]),
            "top": int(data["top"][index]),
            "width": int(data["width"][index]),
            "height": int(data["height"][index]),
        })
    return words


def choose_ocr_lang(sample_text: str, mode: str = "", words: list[dict[str, Any]] | None = None) -> str:
    """Pick Tesseract language from this page's glyphs, not from a prior document."""
    blob = sample_text or ""
    if words:
        confident = [word for word in words if float(word.get("conf") or 0) >= 50]
        picked = confident or words[:80]
        blob = " ".join(str(word.get("text") or "") for word in picked)
    arabic = sum(1 for ch in blob if "\u0600" <= ch <= "\u06FF")
    latin = sum(1 for ch in blob if ch.isascii() and ch.isalpha())
    digits = sum(1 for ch in blob if ch.isdigit())
    if latin >= 25 and arabic * 3 < latin:
        return "eng"
    if latin + digits >= 30 and arabic * 4 < latin + digits:
        return "eng"
    return "ara+eng"


def _header_band_boxes(rgb: np.ndarray, column_count: int) -> list[tuple[int, int, int, int]] | None:
    """Detect a full-width colored header bar (any hue) and split it into columns."""
    import cv2
    if rgb.ndim != 3 or column_count < 2:
        return None
    scaled = cv2.resize(rgb, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    height, width = scaled.shape[:2]
    top_limit = max(40, int(height * 0.18))
    hsv = cv2.cvtColor(scaled[:top_limit], cv2.COLOR_RGB2HSV)
    colored = (hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 50)
    row_density = colored.mean(axis=1)
    # Full-width bar only; logos and stamps must not become a fake header.
    hot = np.where(row_density > 0.40)[0]
    if len(hot) < 4:
        return None
    top = int(hot[0])
    bottom = top
    for y in hot:
        if y - bottom <= 3:
            bottom = int(y)
        else:
            break
    band_h = bottom - top + 6
    if band_h < 12 or band_h > max(36, int(height * 0.08)):
        return None
    gray = cv2.cvtColor(scaled, cv2.COLOR_RGB2GRAY)
    band = gray[max(0, top - 2): min(height, bottom + 4), :]
    edges = cv2.Sobel(band, cv2.CV_32F, 1, 0, ksize=3)
    projection = np.abs(edges).mean(axis=0)
    threshold = float(np.percentile(projection, 93))
    cuts = [0]
    min_gap = max(24, width // (column_count * 3))
    for x in range(8, width - 8):
        if projection[x] >= threshold and (x - cuts[-1]) >= min_gap:
            cuts.append(x)
    cuts.append(width)
    if len(cuts) - 1 == column_count:
        return [(cuts[i], top, max(12, cuts[i + 1] - cuts[i]), band_h) for i in range(column_count)]
    step = width / column_count
    return [(int(i * step), top, max(12, int(step)), band_h) for i in range(column_count)]


def ocr_page_table(
    image: np.ndarray,
    include_page_text: bool = False,
) -> tuple[list[list[str]], list[list[float]], str] | tuple[list[list[str]], list[list[float]], str, str]:
    """Analyze this page only: find a grid if present, read cells, keep row/column order."""
    import cv2
    pytesseract = setup_tesseract()
    # First give coloured, ruled forms to the small on-device document
    # intelligence module.  It finds the item-table region before OCR, which
    # prevents client/detail boxes elsewhere on an invoice from being merged
    # into the line items.  All other pages continue through the established
    # generic OCR path below.
    local = analyze_with_local_ai(image, pytesseract, include_page_text=include_page_text)
    if local is not None:
        if include_page_text:
            return local.table, local.scores, local.method, local.page_context
        return local.table, local.scores, local.method
    cleaned = drop_color_noise(image)
    mode = detect_mode(cleaned)
    processed = preprocess(cleaned, mode)
    scale = 2.0
    scaled = cv2.resize(cleaned, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = enhance_gray(cv2.cvtColor(scaled, cv2.COLOR_RGB2GRAY))
    projected = detect_cells_from_projections(gray)
    has_regular_grid = bool(projected and len(projected) >= 4 and len(projected[0]) >= 4)
    if has_regular_grid:
        mode = "screenshot"
    cells = projected if has_regular_grid else detect_cells(processed)

    ocr_source = gray if (has_regular_grid or mode == "screenshot") else processed
    probe = ocr_words(pytesseract, ocr_source, lang="ara+eng")
    sample = " ".join(word["text"] for word in probe[:80])
    lang = choose_ocr_lang(sample, mode, words=probe)
    words = probe if lang == "ara+eng" else ocr_words(pytesseract, ocr_source, lang=lang)
    page_text = ""
    if include_page_text:
        # Keep raw page context separate from the table.  It is used only to
        # recover invoice labels/totals after the item grid has passed the
        # structural validation in invoice.py.
        page_text = pytesseract.image_to_string(
            ocr_source, lang=lang, config="--oem 1 --psm 3"
        )

    def result(
        table: list[list[str]], scores: list[list[float]]
    ) -> tuple[list[list[str]], list[list[float]], str] | tuple[list[list[str]], list[list[float]], str, str]:
        if include_page_text:
            return table, scores, mode, page_text
        return table, scores, mode

    def _row_mean(boxes: list) -> float:
        import cv2
        values = []
        for x, y, w, h in boxes[: min(8, len(boxes))]:
            crop = scaled[max(0, y): max(0, y + h), max(0, x): max(0, x + w)]
            if crop.size == 0:
                continue
            gray_crop = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
            values.append(float(np.mean(gray_crop)))
        return sum(values) / len(values) if values else 255.0

    def with_header(table: list[list[str]], scores: list[list[float]], grid: list | None = None) -> tuple[list[list[str]], list[list[float]]]:
        if not table:
            return table, scores
        use_grid = bool(grid) and _row_mean(grid[0]) < 130
        source_boxes = grid[0] if use_grid else _header_band_boxes(cleaned, len(table[0]))
        if not source_boxes:
            return table, scores
        header_texts, header_scores = [], []
        for x, y, w, h in source_boxes:
            pad = 2
            # Preserve both ends of every header cell.  Cropping a fixed 12%
            # from the right side cuts short RTL labels such as "الوصف".
            crop = scaled[max(0, y + pad): max(0, y + h - pad), max(0, x + pad): max(0, x + w - pad)]
            if crop.size == 0:
                header_texts.append("")
                header_scores.append(0.0)
                continue
            text, conf = ocr_cell(pytesseract, crop, invert_dark_bg=True, lang=lang)
            text = re.sub(r"[\u25b2\u25bc▼▲<>\[\]|]+", " ", text)
            header_texts.append(_tidy_ocr_text(text))
            header_scores.append(conf)
        if sum(1 for text in header_texts if text.strip()) < max(2, len(header_texts) // 2):
            return table, scores
        header_bottom = source_boxes[0][1] + source_boxes[0][3]
        body, body_scores = [], []
        for index, row in enumerate(table):
            if grid is not None and index < len(grid):
                row_top = grid[index][0][1]
                if row_top < header_bottom - 4:
                    continue
            elif index == 0 and header_row_like(row):
                continue
            body.append(row)
            body_scores.append(scores[index] if index < len(scores) else [0.0] * len(row))
        if body and header_row_like(body[0]):
            return body, body_scores
        return [header_texts] + body, [header_scores] + body_scores

    if cells:
        mapped, mapped_scores, unmapped = assign_words_to_grid(words, cells)
        filled_rows = sum(1 for row in mapped if any(cell.strip() for cell in row))
        use_mapped = filled_rows >= 2 and max(len(row) for row in mapped) >= 3
        # Dense numeric grids map cleanly from words; allow more unmapped noise.
        unmapped_limit = max(12, len(words) // 3) if len(cells[0]) >= 8 else max(8, len(words) // 4)
        coverage = filled_rows / max(len(mapped), 1)
        if use_mapped and len(unmapped) <= unmapped_limit and coverage >= 0.55:
            table, scores = _drop_empty_rows_cols(*with_header(mapped, mapped_scores, cells))
            table, scores = _postprocess_table(
                table, scores, image=image, cells=cells, pytesseract=pytesseract, lang=lang, mode=mode
            )
            if len(table) >= 2:
                return result(table, scores)

        table: list[list[str]] = []
        scores: list[list[float]] = []
        for row in cells:
            texts, confs = [], []
            for x, y, w, h in row:
                pad = 2
                crop = ocr_source[max(0, y + pad): max(0, y + h - pad), max(0, x + pad): max(0, x + w - pad)]
                if crop.size == 0:
                    texts.append("")
                    confs.append(0.0)
                    continue
                text, conf = ocr_cell(pytesseract, crop, lang=lang)
                texts.append(text)
                confs.append(conf)
            if any(texts):
                table.append(texts)
                scores.append(confs)
        if len(table) >= 2:
            table, scores = _drop_empty_rows_cols(*with_header(table, scores, cells))
            table, scores = _postprocess_table(
                table, scores, image=image, cells=cells, pytesseract=pytesseract, lang=lang, mode=mode
            )
            return result(table, scores)

    clustered = cluster_words_to_table(words)
    row_scores: list[list[float]] = []
    groups = _row_groups(words)
    for index, row in enumerate(clustered):
        values = [item["conf"] for item in groups[index] if item["conf"] >= 0] if index < len(groups) else []
        mean = round(sum(values) / len(values), 1) if values else 0.0
        row_scores.append([mean] * max(len(row), 1))
    if clustered and not row_scores:
        row_scores = [[CONFIDENCE_THRESHOLD] * len(row) for row in clustered]
    clustered, row_scores = _postprocess_table(clustered, row_scores, mode=mode)
    return result(clustered, row_scores)


_DIRTY_OCR = re.compile(r"[\$=:~«»\[\]|]")


def _tidy_ocr_text(text: str) -> str:
    cleaned = str(text or "").replace("|", " ").replace("=", " ").replace("~", " ")
    cleaned = cleaned.replace("«", " ").replace("»", " ")
    cleaned = " ".join(cleaned.split())
    return cleaned.strip(" -_,;:()")


def _looks_numeric(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if re.search(r"[A-Za-z]{2,}", value):
        return False
    if re.search(r"[\u0600-\u06FF]", value):
        return False
    if re.search(r"\d{1,4}[/-]\d{1,2}[/-]\d{2,4}", value):
        return False
    digits = sum(ch.isdigit() for ch in value)
    letters = sum(ch.isalpha() and ch not in "OoIlSs" for ch in value)
    return digits >= 1 and letters <= max(1, digits // 3)


def _is_dirty_ocr(text: str) -> bool:
    return bool(_DIRTY_OCR.search(str(text or "")))


def _digitish_len(text: str) -> int:
    return sum(ch.isdigit() or ch in "$SsOoIl" for ch in str(text or ""))


def _tidy_numeric_cell(text: str) -> str:
    cleaned = _tidy_ocr_text(text)
    if not cleaned or not _looks_numeric(cleaned):
        return cleaned
    compact = cleaned.replace(" ", "")
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", compact):
        return compact.replace(",", "")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", compact):
        return compact.replace(".", "").replace(",", ".")
    cleaned = (
        cleaned.replace("$", "5")
        .replace("S", "5")
        .replace("s", "5")
        .replace("O", "0")
        .replace("o", "0")
        .replace("I", "1")
        .replace("l", "1")
        .replace("]", "1")
        .replace("[", "1")
    )
    cleaned = cleaned.replace(":", ".")
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    if cleaned.count(".") > 1:
        parts = [part for part in cleaned.split(".") if part != ""]
        if len(parts) >= 2:
            cleaned = "".join(parts[:-1]) + "." + parts[-1]
        else:
            cleaned = "".join(parts)
    if cleaned.count("-") > 1 or (cleaned.find("-") > 0):
        cleaned = cleaned.replace("-", "")
    return cleaned


def _column_decimal_places(table: list[list[str]]) -> dict[int, int]:
    if len(table) < 4:
        return {}
    width = max(len(row) for row in table)
    start = 1 if table and header_row_like(table[0]) else 0
    places: dict[int, int] = {}
    for col in range(width):
        filled = []
        dotted = []
        for row in table[start:]:
            value = _tidy_numeric_cell(row[col]) if col < len(row) and _looks_numeric(row[col]) else (
                str(row[col]).strip() if col < len(row) else ""
            )
            if not value:
                continue
            filled.append(value)
            if re.fullmatch(r"-?\d+\.\d+", value):
                dotted.append(len(value.split(".")[-1]))
        if len(dotted) < max(3, int(len(filled) * 0.4)):
            continue
        place = Counter(dotted).most_common(1)[0][0]
        if 1 <= place <= 3:
            places[col] = place
    return places


def _restore_column_decimals(table: list[list[str]]) -> list[list[str]]:
    if len(table) < 4:
        return table
    width = max(len(row) for row in table)
    start = 1 if table and header_row_like(table[0]) else 0
    out = [list(row) + [""] * (width - len(row)) for row in table]
    places = _column_decimal_places(out)
    for col, place in places.items():
        for row_index in range(start, len(out)):
            value = str(out[row_index][col] or "").strip()
            if re.fullmatch(r"-?\d+", value) and len(value.lstrip("-")) > place:
                sign = "-" if value.startswith("-") else ""
                digits = value.lstrip("-")
                out[row_index][col] = sign + digits[:-place] + "." + digits[-place:]
    return out


def _choose_cell_text(mapped: str, refined: str, places: int | None) -> str:
    mapped_t = _tidy_numeric_cell(mapped) if _looks_numeric(mapped) else _tidy_ocr_text(mapped)
    refined_t = _tidy_numeric_cell(refined) if _looks_numeric(refined) else _tidy_ocr_text(refined)
    if not refined_t:
        return mapped
    if _digitish_len(refined) < _digitish_len(mapped):
        return mapped
    if places:
        pattern = re.compile(rf"^-?\d+\.\d{{{places}}}$")
        mapped_ok = bool(pattern.match(mapped_t))
        refined_ok = bool(pattern.match(refined_t))
        if refined_ok and not mapped_ok:
            return refined
        if mapped_ok and not refined_ok:
            return mapped
    if _is_dirty_ocr(mapped) and not _is_dirty_ocr(refined_t):
        return refined
    return mapped


def _fix_isolated_letter_digits(table: list[list[str]]) -> list[list[str]]:
    """Map lone O/I/S/$ glyphs in integer columns (row numbers, years)."""
    if len(table) < 4:
        return table
    width = max(len(row) for row in table)
    start = 1 if table and header_row_like(table[0]) else 0
    out = [list(row) + [""] * (width - len(row)) for row in table]
    glyphs = {"S": "5", "s": "5", "$": "5", "O": "0", "o": "0", "I": "1", "l": "1"}
    for col in range(width):
        values = [out[row][col].strip() for row in range(start, len(out))]
        filled = [value for value in values if value]
        ints = [value for value in filled if re.fullmatch(r"\d{1,4}", value)]
        if len(ints) < max(3, int(len(filled) * 0.7)):
            continue
        for row in range(start, len(out)):
            if out[row][col] in glyphs:
                out[row][col] = glyphs[out[row][col]]
    return out


def _refine_dirty_cells(
    pytesseract: Any,
    image: np.ndarray,
    cells: list,
    table: list[list[str]],
    lang: str,
) -> list[list[str]]:
    import cv2
    if not cells or not table:
        return table
    places = _column_decimal_places(table)
    dirty: list[tuple[int, int]] = []
    for row_index, row in enumerate(table):
        if row_index == 0 and header_row_like(row):
            continue
        if row_index >= len(cells):
            continue
        for col_index, value in enumerate(row):
            if col_index >= len(cells[row_index]):
                continue
            raw = str(value or "")
            if _is_dirty_ocr(raw) or (col_index in places and re.fullmatch(r"-?\d+", raw.strip())):
                dirty.append((row_index, col_index))
    if not dirty:
        return table
    scaled3 = cv2.resize(image, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray3 = cv2.cvtColor(scaled3, cv2.COLOR_RGB2GRAY) if scaled3.ndim == 3 else scaled3
    out = [list(row) for row in table]
    for row_index, col_index in dirty[:80]:
        x, y, w, h = cells[row_index][col_index]
        x3, y3, w3, h3 = int(x * 1.5), int(y * 1.5), max(8, int(w * 1.5)), max(8, int(h * 1.5))
        crop = gray3[max(0, y3 + 2): y3 + h3 - 2, max(0, x3 + 2): x3 + w3 - 2]
        if crop.size == 0:
            continue
        text, _conf = ocr_cell(pytesseract, crop, lang=lang)
        out[row_index][col_index] = _choose_cell_text(out[row_index][col_index], text, places.get(col_index))
    return out


def _postprocess_table(
    table: list[list[str]],
    scores: list[list[float]],
    *,
    image: np.ndarray | None = None,
    cells: list | None = None,
    pytesseract: Any = None,
    lang: str = "eng",
    mode: str = "",
) -> tuple[list[list[str]], list[list[float]]]:
    if not table:
        return table, scores
    if (
        cells
        and image is not None
        and pytesseract is not None
        and len(table) == len(cells)
        and len(table[0]) == len(cells[0])
    ):
        table = _refine_dirty_cells(pytesseract, image, cells, table, lang)
    cleaned: list[list[str]] = []
    for row in table:
        cleaned.append([
            _tidy_numeric_cell(cell) if _looks_numeric(cell) else _tidy_ocr_text(cell)
            for cell in row
        ])
    # Never insert a decimal point merely because neighbouring OCR values look
    # similar.  A plausible-but-wrong amount is more harmful than a raw value
    # that is explicitly marked for review.
    return _fix_isolated_letter_digits(cleaned), scores


def header_row_like(row: list[str]) -> bool:
    from clean import header_row_score
    return header_row_score(row) >= 2.0


def _drop_empty_rows_cols(
    table: list[list[str]],
    scores: list[list[float]],
) -> tuple[list[list[str]], list[list[float]]]:
    if not table:
        return table, scores
    width = max(len(row) for row in table)
    kept_rows: list[list[str]] = []
    kept_scores: list[list[float]] = []
    for index, row in enumerate(table):
        padded = list(row) + [""] * (width - len(row))
        if not any(str(cell).strip() for cell in padded):
            continue
        kept_rows.append(padded[:width])
        conf = list(scores[index]) + [0.0] * (width - len(scores[index])) if index < len(scores) else [0.0] * width
        kept_scores.append(conf[:width])
    if not kept_rows:
        return [], []
    keep_cols = [
        index for index in range(width)
        if any(str(row[index]).strip() for row in kept_rows)
    ]
    if not keep_cols:
        return kept_rows, kept_scores
    return (
        [[row[index] for index in keep_cols] for row in kept_rows],
        [[row[index] for index in keep_cols] for row in kept_scores],
    )


def _row_groups(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: int(item["top"])):
        height = max(int(word.get("height") or 12), 10)
        placed = False
        for group in groups:
            if abs(int(word["top"]) - int(group[0]["top"])) < height * 0.7:
                group.append(word)
                placed = True
                break
        if not placed:
            groups.append([word])
    return groups
