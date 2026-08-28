"""Geometric reconstruction: words with coordinates -> document structure.

Takes the word boxes from `perceive` and recovers what the page actually is —
tables, label/value pairs, free text — using geometry alone. No model decides
what a value is here; that is the point. A local model may later *name* the
sections it finds, but it can never change a value.

The unit of output is a Region:

    {"kind": "table" | "key_value" | "text",
     "title": str,            # heading text found above the block, if any
     "direction": "ltr"|"rtl",
     "columns": [str, ...],   # header row when the block has one
     "rows": [[Cell, ...]],   # Cell = {"text", "conf", "alternatives", bbox}
     "bbox": (x0, y0, x1, y1)}
"""
from __future__ import annotations

import re
import statistics
from typing import Any, Iterable

ARABIC_LETTER = re.compile(r"[ؠ-يٮ-ۓۺ-ۿﭐ-ﴽﵐ-ﷻﹰ-ﻼ]")
LABEL_HINT = re.compile(
    r"(?:^|\s)(?:no\.?|number|date|name|total|amount|qty|price|tax|address|city|state|zip|"
    r"phone|email|code|type|status|class|from|to|ref|id)\b|:$|:\s*$",
    re.I,
)
NUMBERISH = re.compile(r"^[\s\d.,:%$€£﷼()+\-/]*\d[\s\d.,:%$€£﷼()+\-/]*$")
DATE_TIME = re.compile(
    r"^\d{1,2}:\d{2}(:\d{2})?$"                      # 14:20
    r"|^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$"            # 2022-05-10
    r"|^\d{1,2}[A-Za-z]{3}\d{2,4}$"                  # 26Jul2026
)
MONEY = re.compile(r"[$€£﷼]|^\d[\d,]*\.\d{2}$")


# --------------------------------------------------------------------------
# Lines
# --------------------------------------------------------------------------
def _median(values: Iterable[float], default: float = 1.0) -> float:
    data = [float(value) for value in values]
    return statistics.median(data) if data else default


def build_lines(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group words into visual lines by vertical overlap."""
    if not words:
        return []
    heights = [word["y1"] - word["y0"] for word in words if word["y1"] > word["y0"]]
    tolerance = max(4.0, _median(heights, 12.0) * 0.55)

    ordered = sorted(words, key=lambda word: ((word["y0"] + word["y1"]) / 2.0, word["x0"]))
    lines: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    centre = 0.0
    for word in ordered:
        word_centre = (word["y0"] + word["y1"]) / 2.0
        if current and abs(word_centre - centre) > tolerance:
            lines.append(sorted(current, key=lambda item: item["x0"]))
            current = []
        current.append(word)
        centre = sum((item["y0"] + item["y1"]) / 2.0 for item in current) / len(current)
    if current:
        lines.append(sorted(current, key=lambda item: item["x0"]))
    return lines


def _line_box(line: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    return (
        min(word["x0"] for word in line), min(word["y0"] for word in line),
        max(word["x1"] for word in line), max(word["y1"] for word in line),
    )


# --------------------------------------------------------------------------
# Cells within a line
# --------------------------------------------------------------------------
def _split_cells(line: list[dict[str, Any]], gap: float) -> list[list[dict[str, Any]]]:
    """Split one line into cells wherever the horizontal gap exceeds `gap`."""
    cells: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_right: float | None = None
    for word in line:
        if previous_right is not None and word["x0"] - previous_right > gap:
            cells.append(current)
            current = []
        current.append(word)
        previous_right = max(previous_right or word["x1"], word["x1"])
    if current:
        cells.append(current)
    return cells


def _cell_gap(words: list[dict[str, Any]]) -> float:
    """Gap width that separates columns rather than words inside a phrase.

    Derived from the page's own typography: a column break is much wider than
    a word space, so the distribution of intra-line gaps is bimodal and the
    upper region marks column boundaries.
    """
    heights = [word["y1"] - word["y0"] for word in words if word["y1"] > word["y0"]]
    return max(10.0, _median(heights, 12.0) * 1.4)


# --------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------
def _spans(cells: list[list[dict[str, Any]]]) -> list[tuple[float, float]]:
    return [
        (min(word["x0"] for word in cell), max(word["x1"] for word in cell))
        for cell in cells if cell
    ]


def _aligned(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    """Do two lines occupy at least two of the same columns?

    Overlap, not start position: a header like "Unit Price" is left-aligned
    while the amounts beneath it are right-aligned, so their start positions
    can differ by more than a column width while plainly sharing a column.
    """
    hits = 0
    for a0, a1 in a:
        width_a = max(1.0, a1 - a0)
        for b0, b1 in b:
            overlap = min(a1, b1) - max(a0, b0)
            if overlap > 0 and overlap >= 0.3 * min(width_a, max(1.0, b1 - b0)):
                hits += 1
                break
    return hits >= 2


def group_blocks(
    lines: list[list[dict[str, Any]]],
    gap: float,
) -> list[list[list[dict[str, Any]]]]:
    """Group consecutive lines that share a column structure.

    Each line is compared against the one directly above it rather than against
    an accumulated footprint of the whole block: a union grows with every row
    added and eventually overlaps anything, which silently swallows a totals
    block into the line-items table above it.
    """
    blocks: list[list[list[dict[str, Any]]]] = []
    current: list[list[dict[str, Any]]] = []
    previous_spans: list[tuple[float, float]] = []
    previous_bottom: float | None = None
    line_height = _median([_line_box(line)[3] - _line_box(line)[1] for line in lines], 14.0)

    for line in lines:
        spans = _spans(_split_cells(line, gap))
        _, top, _, bottom = _line_box(line)
        far = previous_bottom is not None and (top - previous_bottom) > line_height * 2.2

        if not current:
            current = [line]
        elif far or not _aligned(spans, previous_spans):
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
        previous_spans, previous_bottom = spans, bottom
    if current:
        blocks.append(current)
    return blocks


# --------------------------------------------------------------------------
# Column model within a block
# --------------------------------------------------------------------------
def _column_edges(block: list[list[dict[str, Any]]], gap: float) -> list[tuple[float, float]]:
    """Column x-ranges for a block, from vertical whitespace that runs through it.

    A column boundary is a band of x where *no* line in the block has ink. That
    is what the eye uses to see columns, and it works even when the boundary is
    narrow: on a tight invoice header "Description" and "Quantity" are separated
    by less than one word space, but that sliver of white runs down every row,
    so it is unmistakable in aggregate even though no single line reveals it.

    Individual *words* are the input rather than pre-split cells, so this does
    not inherit the cell-splitting threshold's blind spot.
    """
    spans = [(word["x0"], word["x1"]) for line in block for word in line]
    if not spans:
        return []

    left_edge = min(span[0] for span in spans)
    right_edge = max(span[1] for span in spans)
    if right_edge <= left_edge:
        return [(left_edge, right_edge)]

    # 2px buckets keep this cheap while staying finer than any real gap.
    step = 2.0
    width = int((right_edge - left_edge) / step) + 1
    occupied = bytearray(width)
    for start, end in spans:
        first = max(0, int((start - left_edge) / step))
        last = min(width - 1, int((end - left_edge) / step))
        for index in range(first, last + 1):
            occupied[index] = 1

    columns: list[tuple[float, float]] = []
    run_start: int | None = None
    empty_run = 0
    # Deliberately small. Detection boxes are expanded to cover full glyphs, so
    # words inside one phrase very nearly touch, while a real column boundary
    # still leaves a visible sliver. A larger threshold merges the columns of a
    # dense spreadsheet screenshot into one.
    height = _median([word["y1"] - word["y0"] for line in block for word in line], 12.0)
    minimum_gap = max(2, int(max(3.0, height * 0.18) / step))
    for index in range(width):
        if occupied[index]:
            if run_start is None:
                run_start = index
            empty_run = 0
        else:
            empty_run += 1
            if run_start is not None and empty_run >= minimum_gap:
                columns.append((left_edge + run_start * step, left_edge + (index - empty_run + 1) * step))
                run_start = None
    if run_start is not None:
        columns.append((left_edge + run_start * step, right_edge))
    return columns or [(left_edge, right_edge)]


def detect_vertical_rules(image: Any) -> list[tuple[float, float, float]]:
    """Find printed vertical rules as (x, top, bottom).

    Ruled tables state their own column boundaries. Whitespace analysis cannot
    match that on a dense spreadsheet where a date almost touches the column
    beside it, so where the page draws lines, the lines win.
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    if image is None:
        return []
    array = np.asarray(image)
    if array.ndim == 3:
        gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    else:
        gray = array
    height, width = gray.shape[:2]
    if height < 40 or width < 40:
        return []
    binary = cv2.adaptiveThreshold(
        cv2.bitwise_not(gray), 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -2
    )
    # A rule is ink that survives erosion by a tall, one-pixel-wide element.
    length = max(12, height // 25)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, length))
    vertical = cv2.erode(binary, kernel)
    vertical = cv2.dilate(vertical, kernel)
    contours, _ = cv2.findContours(vertical, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rules: list[tuple[float, float, float]] = []
    for contour in contours:
        x, y, box_width, box_height = cv2.boundingRect(contour)
        if box_height >= length and box_width <= max(4, width // 200):
            rules.append((x + box_width / 2.0, float(y), float(y + box_height)))
    rules.sort()
    return rules


def _rule_cuts(
    rules: list[tuple[float, float, float]],
    block_words: list[dict[str, Any]],
    top: float,
    bottom: float,
    left: float,
    right: float,
) -> list[float]:
    """Column boundaries stated by printed rules crossing this block."""
    span = max(1.0, bottom - top)
    cuts: list[float] = []
    for x, y0, y1 in rules:
        if not (left < x < right):
            continue
        # A stray mark is not a column boundary; a rule runs the block's height.
        if min(y1, bottom) - max(y0, top) < span * 0.6:
            continue
        # A printed rule never runs through printed text. If it does, it is a
        # glyph stroke or a table border misread, and cutting there splits a word.
        if any(word["x0"] + 2 < x < word["x1"] - 2 for word in block_words):
            continue
        cuts.append(x)
    return cuts


def _combine_columns(
    whitespace: list[tuple[float, float]],
    rule_cuts: list[float],
    left: float,
    right: float,
) -> list[tuple[float, float]]:
    """Merge both kinds of evidence for where the columns are.

    A printed rule and a column of whitespace are each positive evidence of a
    boundary, and neither is evidence against one. Trusting only rules merged
    two columns of an invoice whose faint borders were half-detected; trusting
    only whitespace merged the tight columns of a spreadsheet screenshot. Their
    union gets both right.
    """
    cuts: list[float] = list(rule_cuts)
    for index in range(len(whitespace) - 1):
        cuts.append((whitespace[index][1] + whitespace[index + 1][0]) / 2.0)
    cuts = sorted(value for value in cuts if left < value < right)

    merged: list[float] = []
    for value in cuts:
        if not merged or value - merged[-1] > 6:
            merged.append(value)
    edges = [left] + merged + [right]
    return [
        (edges[index], edges[index + 1])
        for index in range(len(edges) - 1)
        if edges[index + 1] - edges[index] > 4
    ]


def _column_of(cell: list[dict[str, Any]], columns: list[tuple[float, float]]) -> int:
    left = min(word["x0"] for word in cell)
    right = max(word["x1"] for word in cell)
    centre = (left + right) / 2.0
    best, best_overlap = 0, -1.0
    for index, (start, end) in enumerate(columns):
        overlap = min(right, end) - max(left, start)
        if overlap > best_overlap:
            best, best_overlap = index, overlap
        if start <= centre <= end and overlap > 0:
            return index
    return best


def _cell(words: list[dict[str, Any]], direction: str) -> dict[str, Any]:
    ordered = sorted(words, key=lambda word: word["x0"])
    if direction == "rtl":
        # Arabic runs right-to-left, so the rightmost token comes first. Latin
        # fragments embedded in an Arabic line keep their own order.
        ordered = sorted(words, key=lambda word: -word["x0"])
    # Always space-separated. Box geometry cannot decide this: the detector is
    # configured to expand boxes so they cover full glyphs, which shrinks the
    # gap between two separate words to nearly nothing — joining on that basis
    # produced "SearchDownloads" and "CreditCard".
    text = " ".join(str(word.get("text") or "").strip() for word in ordered)
    text = re.sub(r"\s+", " ", text).strip()
    confidences = [float(word.get("conf") or 0.0) for word in words]
    alternatives: list[dict[str, Any]] = []
    if len(words) == 1:
        alternatives = list(words[0].get("alternatives") or [])
    return {
        "text": text,
        "conf": round(min(confidences), 1) if confidences else 0.0,
        "alternatives": alternatives,
        "x0": min(word["x0"] for word in words), "y0": min(word["y0"] for word in words),
        "x1": max(word["x1"] for word in words), "y1": max(word["y1"] for word in words),
    }


def _empty_cell() -> dict[str, Any]:
    return {"text": "", "conf": 100.0, "alternatives": [], "x0": 0.0, "y0": 0.0, "x1": 0.0, "y1": 0.0}


def _direction(words: Iterable[dict[str, Any]]) -> str:
    arabic = latin = 0
    for word in words:
        text = str(word.get("text") or "")
        arabic += len(ARABIC_LETTER.findall(text))
        latin += sum(1 for character in text if character.isascii() and character.isalpha())
    return "rtl" if arabic > latin * 0.8 and arabic > 0 else "ltr"


# --------------------------------------------------------------------------
# Region assembly
# --------------------------------------------------------------------------
def _looks_like_header(row: list[dict[str, Any]], remaining: int) -> bool:
    """A header row labels its columns: words only, never data.

    Strict on purpose. Promoting a data row to a header silently deletes it
    from the output and mislabels every row beneath it — on the airline ticket
    a row of departure times ("14:20 | 17:15 | 15:50") was being turned into
    column names, which is worse than having no header at all.
    """
    filled = [cell for cell in row if cell["text"].strip()]
    if len(filled) < 2 or remaining < 1:
        return False
    for cell in filled:
        text = cell["text"].strip()
        if len(text) > 40:
            return False
        if NUMBERISH.match(text) or DATE_TIME.match(text) or MONEY.search(text):
            return False
    lettered = sum(1 for cell in filled if any(character.isalpha() for character in cell["text"]))
    return lettered >= max(2, len(filled) // 2)


def _classify(rows: list[list[dict[str, Any]]], width: int) -> str:
    if width <= 1:
        return "text"
    if width == 2:
        # Two aligned columns are a table only when the left side stops looking
        # like a list of labels (repeating, short, often ending in a colon).
        labels = [row[0]["text"].strip() for row in rows if row[0]["text"].strip()]
        if labels and len(set(labels)) == len(labels) and len(rows) <= 12:
            return "key_value"
        if len(rows) >= 4 and sum(1 for row in rows if NUMBERISH.match(row[1]["text"].strip())) >= len(rows) * 0.6:
            return "table"
        return "key_value"
    return "table"


def _is_noise(word: dict[str, Any]) -> bool:
    """A single unreadable punctuation mark is a detector artefact, not data.

    Left in place these split tables in two, because a stray one-character line
    shares no columns with the rows around it.
    """
    text = str(word.get("text") or "").strip()
    return (
        len(text) <= 2
        and float(word.get("conf") or 0.0) < 30.0
        and not any(character.isalnum() for character in text)
    )


def build_document(words: list[dict[str, Any]], page: int = 1, image: Any = None) -> dict[str, Any]:
    """Reconstruct one page into typed regions.

    `image` is optional; when supplied, printed rules are used as column
    boundaries wherever the page draws them.
    """
    warnings: list[str] = []
    words = [word for word in words if str(word.get("text") or "").strip()]
    dropped = [word for word in words if _is_noise(word)]
    if dropped:
        words = [word for word in words if not _is_noise(word)]
        warnings.append(f"layout:noise-dropped:{len(dropped)}")
    if not words:
        return {"regions": [], "direction": "ltr", "warnings": ["layout:empty"], "page": page}

    gap = _cell_gap(words)
    lines = build_lines(words)
    blocks = group_blocks(lines, gap)

    rules = detect_vertical_rules(image) if image is not None else []
    if rules:
        warnings.append(f"layout:rules:{len(rules)}")

    regions: list[dict[str, Any]] = []
    for block in blocks:
        block_words = [word for line in block for word in line]
        columns = _column_edges(block, gap)
        if not columns:
            continue
        if rules:
            left = min(word["x0"] for word in block_words)
            right = max(word["x1"] for word in block_words)
            cuts = _rule_cuts(
                rules, block_words,
                min(word["y0"] for word in block_words),
                max(word["y1"] for word in block_words),
                left, right,
            )
            if cuts:
                columns = _combine_columns(columns, cuts, left, right) or columns
        direction = _direction(word for line in block for word in line)
        rows: list[list[dict[str, Any]]] = []
        for line in block:
            slots: dict[int, list[dict[str, Any]]] = {}
            for word in line:
                index = _column_of([word], columns)
                slots.setdefault(index, []).append(word)
            row = [
                _cell(slots[index], direction) if index in slots else _empty_cell()
                for index in range(len(columns))
            ]
            if any(cell["text"].strip() for cell in row):
                rows.append(row)
        if not rows:
            continue
        # A column no row ever fills is an artefact of the boundary search, and
        # in Excel it reads as a gap the customer has to scroll past.
        used = {
            index
            for row in rows
            for index, cell in enumerate(row)
            if cell["text"].strip()
        }
        if used and len(used) < len(columns):
            keep = sorted(used)
            rows = [[row[index] for index in keep if index < len(row)] for row in rows]
            columns = [columns[index] for index in keep]

        if direction == "rtl":
            # Column A must be the rightmost column so an RTL sheet view shows
            # the document in its original reading order.
            rows = [list(reversed(row)) for row in rows]

        width = len(columns)
        kind = _classify(rows, width)
        header: list[str] = []
        if kind == "table" and _looks_like_header(rows[0], len(rows) - 1):
            header = [cell["text"] for cell in rows[0]]
            rows = rows[1:]
            if not rows:
                kind = "text"
                rows = [[_cell_from_text(value) for value in header]]
                header = []

        # Measured from the block's words: reconstructed header cells carry no
        # geometry of their own.
        x0 = min(word["x0"] for word in block_words)
        y0 = min(word["y0"] for word in block_words)
        x1 = max(word["x1"] for word in block_words)
        y1 = max(word["y1"] for word in block_words)
        regions.append({
            "kind": kind,
            "title": "",
            "direction": direction,
            "columns": header,
            "rows": rows,
            "bbox": (x0, y0, x1, y1),
            "page": page,
        })

    _attach_titles(regions)
    document = {
        "regions": regions,
        "direction": _direction(words),
        "warnings": warnings,
        "page": page,
    }
    return document


def _cell_from_text(text: str) -> dict[str, Any]:
    cell = _empty_cell()
    cell["text"] = text
    return cell


def _attach_titles(regions: list[dict[str, Any]]) -> None:
    """Promote a lone short text line directly above a block into its title."""
    for index, region in enumerate(regions):
        if region["kind"] != "text" or len(region["rows"]) != 1:
            continue
        text = " ".join(cell["text"] for cell in region["rows"][0]).strip()
        if not text or len(text) > 60:
            continue
        following = regions[index + 1] if index + 1 < len(regions) else None
        if following and not following["title"] and following["kind"] in {"table", "key_value"}:
            gap = following["bbox"][1] - region["bbox"][3]
            height = max(1.0, region["bbox"][3] - region["bbox"][1])
            if 0 <= gap <= height * 2.5:
                following["title"] = text
                region["consumed"] = True
    regions[:] = [region for region in regions if not region.get("consumed")]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def render_layout_text(document: dict[str, Any], limit: int = 120) -> str:
    """Human-readable dump of the reconstructed structure, for diagnosis.

    Shows what the geometry stage decided before any of it reaches Excel, which
    is the fastest way to tell a misread cell from a mis-grouped column when a
    document comes out wrong. Every cell carries an address (`R2C3`).
    """
    out: list[str] = []
    for index, region in enumerate(document.get("regions") or [], start=1):
        header = region.get("columns") or []
        out.append(f"[region {index}] kind={region['kind']} dir={region['direction']}"
                   + (f" title={region['title']!r}" if region.get("title") else ""))
        if header:
            out.append("  columns: " + " | ".join(header))
        for row_index, row in enumerate(region.get("rows") or [], start=1):
            if row_index > limit:
                out.append(f"  ... ({len(region['rows']) - limit} more rows)")
                break
            cells = " | ".join(
                f"R{row_index}C{cell_index}={cell['text']}"
                for cell_index, cell in enumerate(row, start=1)
                if cell["text"].strip()
            )
            out.append("  " + cells)
    return "\n".join(out)


def render_for_classification(document: dict[str, Any], indexes: list[int]) -> str:
    """A compact sketch of each region, for choosing a section category.

    Deliberately tiny. Feeding the full layout to a 3B model on CPU cost about
    a minute per page — most of the total runtime — to produce nothing but a
    heading. Naming a block needs its shape and a couple of labels, not its
    contents.
    """
    out: list[str] = []
    for index in indexes:
        regions = document.get("regions") or []
        if index >= len(regions):
            continue
        region = regions[index]
        rows = region.get("rows") or []
        columns = region.get("columns") or []
        sample: list[str] = list(columns[:6])
        if not sample:
            for row in rows[:2]:
                sample.extend(cell["text"][:24] for cell in row[:4] if cell["text"].strip())
        out.append(
            f"region {index + 1}: kind={region['kind']} rows={len(rows)} "
            + "labels=" + " / ".join(sample)[:160]
        )
    return "\n".join(out)


def count_cells(document: dict[str, Any]) -> int:
    return sum(
        1
        for region in document.get("regions") or []
        for row in region.get("rows") or []
        for cell in row
        if cell["text"].strip()
    )


def mean_confidence(document: dict[str, Any]) -> float:
    values = [
        float(cell["conf"])
        for region in document.get("regions") or []
        for row in region.get("rows") or []
        for cell in row
        if cell["text"].strip()
    ]
    return round(sum(values) / len(values), 1) if values else 0.0
