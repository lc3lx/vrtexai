"""Grid-line table detection and word-box column clustering."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

import numpy as np

Cell = tuple[int, int, int, int]
Table = list[list[str]]


def _line_masks(binary: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    import cv2
    work = binary
    if float(np.mean(work)) > 127:
        work = 255 - work
    height, width = work.shape[:2]
    horizontal = cv2.morphologyEx(
        work, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(width // 28, 18), 1))
    )
    vertical = cv2.morphologyEx(
        work, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(height // 28, 18)))
    )
    return horizontal, vertical


def _cluster_1d(positions: list[int], min_gap: int) -> list[int]:
    if not positions:
        return []
    ordered = sorted(positions)
    groups = [[ordered[0]]]
    for value in ordered[1:]:
        if value - groups[-1][-1] <= min_gap:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [int(round(sum(group) / len(group))) for group in groups]


def detect_cells_from_projections(gray: np.ndarray) -> list[list[Cell]]:
    """Build a regular cell grid from horizontal/vertical line projections."""
    import cv2
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape[:2]
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(blur, 35, 110)
    horizontal = cv2.morphologyEx(
        edges, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(width // 20, 24), 1))
    )
    vertical = cv2.morphologyEx(
        edges, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(height // 20, 24)))
    )
    h_proj = horizontal.mean(axis=1)
    body_top = int(height * 0.12)
    v_proj = vertical[body_top:].mean(axis=0) if height - body_top > 40 else vertical.mean(axis=0)
    h_thr = max(4.0, float(np.percentile(h_proj, 88)))
    v_thr = max(4.0, float(np.percentile(v_proj, 90)))
    y_lines = _cluster_1d([int(y) for y in np.where(h_proj >= h_thr)[0]], min_gap=max(8, height // 80))
    x_lines = _cluster_1d([int(x) for x in np.where(v_proj >= v_thr)[0]], min_gap=max(8, width // 80))
    if len(y_lines) < 3 or len(x_lines) < 3:
        return []
    if y_lines[0] > 8:
        y_lines = [0] + y_lines
    if y_lines[-1] < height - 8:
        y_lines = y_lines + [height - 1]
    if x_lines[0] > 8:
        x_lines = [0] + x_lines
    if x_lines[-1] < width - 8:
        x_lines = x_lines + [width - 1]
    y_lines = _regularize_line_positions(y_lines, axis_span=height)
    x_lines = _regularize_line_positions(x_lines, axis_span=width, prefer_dense=True)
    if len(y_lines) < 3 or len(x_lines) < 3:
        return []
    rows: list[list[Cell]] = []
    for row_index in range(len(y_lines) - 1):
        top = y_lines[row_index]
        bottom = y_lines[row_index + 1]
        if bottom - top < 12:
            continue
        row: list[Cell] = []
        for col_index in range(len(x_lines) - 1):
            left = x_lines[col_index]
            right = x_lines[col_index + 1]
            if right - left < 12:
                continue
            row.append((left, top, right - left, bottom - top))
        if len(row) >= 2:
            rows.append(row)
    if len(rows) < 2:
        return []
    # Drop unusually short rows created by double-detected gridlines.
    heights = [row[0][3] for row in rows]
    median_h = float(np.median(heights))
    rows = [row for row in rows if row[0][3] >= median_h * 0.55]
    if len(rows) < 2:
        return []
    target = Counter(len(row) for row in rows).most_common(1)[0][0]
    matched = [row for row in rows if len(row) == target]
    return select_uniform_grid_rows(matched)


def select_uniform_grid_rows(rows: list[list[Cell]]) -> list[list[Cell]]:
    """Drop tall header/footer boxes on form pages; keep full grids that are already even."""
    if len(rows) < 4:
        return rows
    heights = [max(12, row[0][3]) for row in rows]
    median_h = float(np.median(heights))
    if median_h <= 0:
        return rows
    if float(np.std(heights)) / median_h < 0.28:
        return rows
    similar = [0.5 * median_h <= height <= 1.8 * median_h for height in heights]
    best_start = best_len = run_start = run_len = 0
    for index, ok in enumerate(similar + [False]):
        if ok:
            if run_len == 0:
                run_start = index
            run_len += 1
            if run_len > best_len:
                best_start, best_len = run_start, run_len
        else:
            run_len = 0
    if best_len < max(3, len(rows) // 3):
        return rows
    body = rows[best_start: best_start + best_len]
    if best_start > 0:
        return [rows[0]] + body
    return body


def _regularize_line_positions(lines: list[int], axis_span: int, prefer_dense: bool = False) -> list[int]:
    """Keep line positions that follow a consistent spacing pattern."""
    lines = _cluster_1d(lines, min_gap=max(6, axis_span // 120))
    if len(lines) < 4:
        return lines
    gaps = [lines[i + 1] - lines[i] for i in range(len(lines) - 1)]
    positive = [gap for gap in gaps if gap > 4]
    if not positive:
        return lines
    median_gap = float(np.median(positive))
    min_real = max(12, axis_span // 80)
    lo = min(median_gap * (0.28 if prefer_dense else 0.40), float(min_real))
    kept = [lines[0]]
    for value in lines[1:]:
        gap = value - kept[-1]
        if gap < lo:
            kept[-1] = value
            continue
        kept.append(value)
    return _cluster_1d(kept, min_gap=max(6, axis_span // 120))


def detect_cells(binary: np.ndarray) -> list[list[Cell]]:
    import cv2
    height, width = binary.shape[:2]
    projected = detect_cells_from_projections(binary)
    if projected and len(projected) >= 4 and len(projected[0]) >= 4:
        return projected

    horizontal, vertical = _line_masks(binary)
    grid = cv2.add(horizontal, vertical)
    grid = cv2.dilate(grid, np.ones((2, 2), np.uint8), iterations=1)
    holes = cv2.bitwise_not(grid)
    count, _labels, stats, _ = cv2.connectedComponentsWithStats(holes, connectivity=4)
    boxes: list[Cell] = []
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if area < 90 or w < 18 or h < 12:
            continue
        if x <= 1 or y <= 1 or x + w >= width - 1 or y + h >= height - 1:
            if w > width * 0.85 or h > height * 0.85:
                continue
        if w > width * 0.92 and h > height * 0.92:
            continue
        if w > width * 0.42 and h < height * 0.08:
            continue
        if h > height * 0.08 and w > width * 0.08:
            continue
        boxes.append((int(x), int(y), int(w), int(h)))
    if len(boxes) < 4:
        return projected or []
    rows = _boxes_to_rows(boxes)
    normalized = normalize_cell_grid(rows, width)
    if projected and (len(projected) * len(projected[0])) > (len(normalized) * max((len(r) for r in normalized), default=0)):
        return projected
    return normalized


def _boxes_to_rows(boxes: list[Cell]) -> list[list[Cell]]:
    boxes = sorted(boxes, key=lambda box: (box[1], box[0]))
    rows: list[list[Cell]] = []
    for box in boxes:
        _x, y, _w, h = box
        placed = False
        for row in rows:
            _rx, ry, _rw, rh = row[0]
            if abs((y + h / 2) - (ry + rh / 2)) < max(h, rh) * 0.45:
                row.append(box)
                placed = True
                break
        if not placed:
            rows.append([box])
    return [sorted(row, key=lambda item: item[0]) for row in rows if len(row) >= 2]


def normalize_cell_grid(rows: list[list[Cell]], image_width: int) -> list[list[Cell]]:
    """Force every row onto the same column boundaries inferred from the modal grid."""
    usable = [row for row in rows if 3 <= len(row) <= 24]
    if not usable:
        return rows
    target = Counter(len(row) for row in usable).most_common(1)[0][0]
    seed_rows = [row for row in usable if len(row) == target]
    if not seed_rows:
        return rows
    lefts = [float(np.median([row[index][0] for row in seed_rows])) for index in range(target)]
    rights = [
        float(np.median([row[index][0] + row[index][2] for row in seed_rows]))
        for index in range(target)
    ]
    boundaries = [max(0.0, lefts[0] - 2)]
    for index in range(target - 1):
        boundaries.append((rights[index] + lefts[index + 1]) / 2.0)
    boundaries.append(min(float(image_width), rights[-1] + 2))

    normalized: list[list[Cell]] = []
    for row in rows:
        if not row:
            continue
        _x, y, _w, heights = zip(*[(box[0], box[1], box[2], box[3]) for box in row])
        top = int(np.median(y))
        height = max(12, int(np.median(heights)))
        rebuilt: list[Cell] = []
        for index in range(target):
            left = int(round(boundaries[index]))
            right = int(round(boundaries[index + 1]))
            rebuilt.append((left, top, max(12, right - left), height))
        normalized.append(rebuilt)
    return normalized


def cluster_words_to_table(words: Iterable[dict[str, Any]]) -> Table:
    items = []
    for word in words:
        text = str(word.get("text") or "").strip()
        if not text:
            continue
        parts = [part for part in re.split(r"[|/=]+", text) if part.strip()]
        if len(parts) <= 1:
            items.append(dict(word))
            continue
        width = max(int(word.get("width") or 10), 8)
        left = int(word["left"])
        step = width / len(parts)
        for offset, part in enumerate(parts):
            clone = dict(word)
            clone["text"] = part.strip()
            clone["left"] = int(left + offset * step)
            clone["width"] = max(8, int(step))
            items.append(clone)
    if not items:
        return []
    items.sort(key=lambda word: (int(word["top"]), int(word["left"])))
    lines: list[list[dict[str, Any]]] = []
    for word in items:
        height = max(int(word.get("height") or 12), 10)
        cy = int(word["top"]) + height / 2
        placed = False
        for line in lines:
            ref = line[0]
            ref_h = max(int(ref.get("height") or 12), 10)
            ref_cy = int(ref["top"]) + ref_h / 2
            if abs(cy - ref_cy) < max(height, ref_h) * 0.55:
                line.append(word)
                placed = True
                break
        if not placed:
            lines.append([word])

    centers = [int(word["left"]) + max(int(word.get("width") or 10), 8) / 2 for word in items]
    gap = max(18, int(np.median([int(word.get("width") or 20) for word in items]) * 1.35))
    columns = _cluster_positions([int(value) for value in centers], gap)
    if len(columns) < 2:
        return [[" ".join(str(word["text"]) for word in sorted(line, key=lambda item: int(item["left"])))] for line in lines]

    header_line = max(lines[:5], key=lambda line: len(line), default=lines[0])
    if len(header_line) >= max(3, len(columns) - 1):
        header_centers = sorted(
            int(word["left"]) + max(int(word.get("width") or 10), 8) / 2 for word in header_line
        )
        if len(header_centers) >= 2:
            columns = [float(value) for value in header_centers]

    table: Table = []
    for line in lines:
        row = [""] * len(columns)
        for word in sorted(line, key=lambda item: int(item["left"])):
            center = int(word["left"]) + max(int(word.get("width") or 10), 8) / 2
            index = int(np.argmin([abs(center - column) for column in columns]))
            row[index] = (row[index] + " " + str(word["text"])).strip()
        if any(row):
            table.append(row)
    return _pad_table_columns(table)


def _pad_table_columns(table: Table) -> Table:
    if not table:
        return table
    width = max(len(row) for row in table)
    return [list(row) + [""] * (width - len(row)) for row in table]


def _cluster_positions(values: list[int], gap: int) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    groups = [[ordered[0]]]
    for value in ordered[1:]:
        if value - groups[-1][-1] <= gap:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def assign_words_to_grid(
    words: Iterable[dict[str, Any]],
    grid: list[list[Cell]],
) -> tuple[Table, list[list[float]], list[dict[str, Any]]]:
    """Map OCR tokens into normalized grid cells; return unmapped tokens."""
    table: Table = [["" for _ in row] for row in grid]
    scores: list[list[list[float]]] = [[[] for _ in row] for row in grid]
    unmapped: list[dict[str, Any]] = []
    for word in words:
        text = str(word.get("text") or "").strip()
        if not text:
            continue
        left = int(word["left"])
        top = int(word["top"])
        width = max(int(word.get("width") or 8), 4)
        height = max(int(word.get("height") or 8), 4)
        cx = left + width / 2
        cy = top + height / 2
        best = None
        best_dist = None
        for row_index, row in enumerate(grid):
            for col_index, (x, y, w, h) in enumerate(row):
                if x - 3 <= cx <= x + w + 3 and y - 3 <= cy <= y + h + 3:
                    dist = abs(cx - (x + w / 2)) + abs(cy - (y + h / 2))
                    if best_dist is None or dist < best_dist:
                        best = (row_index, col_index)
                        best_dist = dist
        if best is None:
            unmapped.append(word)
            continue
        row_index, col_index = best
        table[row_index][col_index] = (table[row_index][col_index] + " " + text).strip()
        conf = float(word.get("conf") or -1)
        if conf >= 0:
            scores[row_index][col_index].append(conf)
    conf_table = [
        [round(sum(values) / len(values), 1) if values else 0.0 for values in row]
        for row in scores
    ]
    return table, conf_table, unmapped
