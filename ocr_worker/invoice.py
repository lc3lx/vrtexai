"""Invoice header / line-item / totals parsing and arithmetic checks."""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from clean import canonical_header, clean_text, find_header_row, normalize_date, normalize_number
from common import CONFIDENCE_THRESHOLD, INVOICE_PATTERNS

ITEM_HEADERS = {
    "description", "qty", "unit_price", "total", "sku",
    "الوصف", "الكمية", "سعر الوحدة", "المبلغ", "المنتج", "البيان", "معدل", "معرف المنتج",
}


def extract_invoice_fields(lines: Iterable[str]) -> dict[str, str]:
    text = "\n".join(lines)
    fields: dict[str, str] = {}
    for name, pattern in INVOICE_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            continue
        if name == "currency":
            fields[name] = match.group(0).upper() if match.group(0).isascii() else match.group(0)
            continue
        value = clean_text(match.group(1), name)
        fields[name] = normalize_date(value) if "date" in name else value
    return fields


def extract_item_values(text: str) -> dict[str, str]:
    numbers = re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d{1,3})?", text)
    if len(numbers) < 2:
        return {"description": text, "sku": "", "qty": "", "unit_price": "", "total": ""}
    total = numbers[-1].replace(",", "")
    qty = numbers[-3].replace(",", "") if len(numbers) > 2 else "1"
    price = numbers[-2].replace(",", "") if len(numbers) > 2 else numbers[-1].replace(",", "")
    if len(numbers) == 2:
        qty, price, total = "1", numbers[0].replace(",", ""), numbers[1].replace(",", "")
    description = text[: max(0, text.rfind(numbers[-1]))].strip(" -:;")
    return {"description": description or text, "qty": qty, "unit_price": price, "total": total}


def _to_float(value: str) -> float | None:
    try:
        return float(normalize_number(str(value)).replace(",", ""))
    except (TypeError, ValueError):
        return None


def totals_mismatch(qty: str, price: str, total: str, tolerance: float = 0.05) -> bool:
    left, right, expected = _to_float(qty), _to_float(price), _to_float(total)
    if left is None or right is None or expected is None:
        return False
    return abs(left * right - expected) > max(tolerance, abs(expected) * 0.01)


def reconcile_quantity_from_total(qty: str, price: str, total: str) -> tuple[str, bool]:
    """Repair a short OCR quantity only when invoice arithmetic proves it.

    This is deliberately narrower than decimal guessing: it changes a value
    only when the independently read price and line total divide to a positive
    whole number.  The caller still marks the item for review.
    """
    current, unit_price, line_total = _to_float(qty), _to_float(price), _to_float(total)
    if current is None or unit_price is None or line_total is None or unit_price == 0:
        return qty, False
    expected = line_total / unit_price
    rounded = round(expected)
    if rounded <= 0 or rounded > 1_000_000 or abs(expected - rounded) > 0.001:
        return qty, False
    if abs(current - expected) <= 0.001:
        return qty, False
    return str(rounded), True


def _item_header(rows: Sequence[Sequence[Any]]) -> tuple[int, list[str]] | None:
    """Find an item-table header, not just the most text-heavy OCR row."""
    best: tuple[int, list[str], int] | None = None
    fields = {"description", "sku", "qty", "unit_price", "total"}
    for index, row in enumerate(rows[:50]):
        mapped = [canonical_header(str(value or "").strip()) for value in row]
        signals = len(set(mapped) & fields)
        if signals < 3:
            continue
        if best is None or signals > best[2]:
            best = (index, mapped, signals)
    return (best[0], best[1]) if best else None


def _single_number(value: Any) -> bool:
    text = clean_text(value)
    return bool(re.fullmatch(r"\d+(?:[.,]\d{1,3})?", text))


def _description_has_ocr_noise(value: str) -> bool:
    """Flag visibly corrupted item wording without rejecting valid Arabic/English text."""
    text = str(value or "")
    return bool(re.search(r"[©�]|(?:[ـ_]){4,}", text))


def _party_value_is_only_a_label(value: str) -> bool:
    """Reject page labels accidentally captured as a company/customer name."""
    text = clean_text(value).casefold()
    compact = re.sub(r"\s+", "", text)
    return bool(re.search(
        r"(?:\binvoice\b|فاتور|\bamount\s*due\b|المبلغ\s*المستحق|"
        r"(?:اسم|name)\s*(?:العميل|الشركة)|\b(?:client|customer)\s*name\b)",
        compact,
        re.I,
    ))


def invoice_table_is_reliable(rows: Sequence[Sequence[Any]]) -> bool:
    """Require a real item grid before creating semantic invoice fields."""
    header = _item_header(rows)
    if header is None:
        return False
    header_at, mapped = header
    indices = {name: index for index, name in enumerate(mapped) if name in {"description", "qty", "unit_price", "total"}}
    if not {"description", "qty", "unit_price", "total"}.issubset(indices):
        return False
    valid_items = 0
    for row in rows[header_at + 1 :]:
        description = str(row[indices["description"]] if indices["description"] < len(row) else "").strip()
        if not description or re.search(r"(?:total|subtotal|vat|المجموع|الضريبة)", description, re.I):
            continue
        values = {
            name: str(row[indices[name]] if indices[name] < len(row) else "")
            for name in ("qty", "unit_price", "total")
        }
        if not all(_single_number(values[name]) for name in values):
            continue
        quantity, price, total = (_to_float(values[name]) for name in ("qty", "unit_price", "total"))
        if quantity is None or price is None or total is None or quantity <= 0 or price < 0 or total < 0:
            return False
        expected = quantity * price
        # Line totals may include a normal tax or discount, but a value that
        # is 10x/100x the independently read quantity × rate is almost always
        # a lost decimal point. Such a grid is exportable for review, but it
        # must not become a semantic invoice automatically.
        if expected == 0:
            if total > 0.02:
                return False
        elif not 0.50 <= total / expected <= 1.50:
            return False
        valid_items += 1
    return valid_items > 0


def parse_invoice_table(
    rows: Sequence[Sequence[Any]],
    scores: Sequence[Sequence[float]] | None = None,
    context_pages: Sequence[str] | None = None,
) -> dict[str, Any]:
    lines = [" ".join(str(cell or "") for cell in row) for row in rows]
    lines.extend(line for page in context_pages or [] for line in str(page).splitlines())
    header_fields = extract_invoice_fields(lines)
    for party_key in ("supplier", "client_name"):
        if _party_value_is_only_a_label(header_fields.get(party_key, "")):
            header_fields.pop(party_key, None)
    item_header = _item_header(rows)
    header_at = item_header[0] if item_header else find_header_row(rows)
    raw_headers = [str(cell or "").strip() for cell in rows[header_at]] if rows else []
    mapped = item_header[1] if item_header else [canonical_header(value) for value in raw_headers]
    looks_like_items = any(name in ITEM_HEADERS or name in {"description", "qty", "unit_price", "total"} for name in mapped)
    items: list[dict[str, Any]] = []
    low = 0
    if looks_like_items:
        index_map = {name: index for index, name in enumerate(mapped)}
        for offset, row in enumerate(rows[header_at + 1 :]):
            description = str(row[index_map["description"]] if "description" in index_map and index_map["description"] < len(row) else " ".join(str(c or "") for c in row)).strip()
            sku = str(row[index_map["sku"]] if "sku" in index_map and index_map["sku"] < len(row) else "")
            qty = str(row[index_map["qty"]] if "qty" in index_map and index_map["qty"] < len(row) else "")
            price = str(row[index_map["unit_price"]] if "unit_price" in index_map and index_map["unit_price"] < len(row) else "")
            total = str(row[index_map["total"]] if "total" in index_map and index_map["total"] < len(row) else "")
            if not description or re.search(r"(?:المجموع|total|subtotal|vat|الضريبة)", description, re.I):
                extra = extract_invoice_fields([description + " " + " ".join(str(c or "") for c in row)])
                header_fields.update({key: value for key, value in extra.items() if value})
                continue
            row_index = header_at + 1 + offset
            confs = list(scores[row_index]) if scores and row_index < len(scores) else []
            confidence = round(sum(confs) / len(confs), 1) if confs else 100.0
            description_confidence = (
                float(confs[index_map["description"]])
                if "description" in index_map and index_map["description"] < len(confs)
                else confidence
            )
            qty, arithmetic_corrected = reconcile_quantity_from_total(qty, price, total)
            mismatch = totals_mismatch(qty, price, total)
            review = (
                confidence < CONFIDENCE_THRESHOLD
                or description_confidence < CONFIDENCE_THRESHOLD
                or mismatch
                or arithmetic_corrected
                or _description_has_ocr_noise(description)
            )
            low += int(review)
            items.append({
                "description": description, "sku": sku, "qty": qty, "unit_price": price, "total": total,
                "confidence": confidence, "review": review,
            })
    else:
        for offset, row in enumerate(rows):
            text = " ".join(str(cell or "") for cell in row).strip()
            if not text or (len(text.split()) < 8 and extract_invoice_fields([text])):
                continue
            parsed = extract_item_values(text)
            if not parsed["total"]:
                continue
            confs = list(scores[offset]) if scores and offset < len(scores) else []
            confidence = round(sum(confs) / len(confs), 1) if confs else 100.0
            mismatch = totals_mismatch(parsed["qty"], parsed["unit_price"], parsed["total"])
            review = confidence < CONFIDENCE_THRESHOLD or mismatch or _description_has_ocr_noise(str(parsed.get("description") or ""))
            low += int(review)
            items.append({**parsed, "confidence": confidence, "review": review})
    totals = {
        "subtotal": header_fields.get("subtotal", ""),
        "tax_amount": header_fields.get("tax_amount", ""),
        "grand_total": header_fields.get("grand_total", ""),
        "currency": header_fields.get("currency", ""),
    }
    if items and not totals["grand_total"]:
        amounts = [_to_float(item["total"]) for item in items]
        if all(value is not None for value in amounts):
            totals["grand_total"] = f"{sum(value or 0 for value in amounts):.2f}"
            if not totals["subtotal"]:
                totals["subtotal"] = totals["grand_total"]
    return {"header": header_fields, "items": items, "totals": totals, "low_confidence": low}


def lines_to_table(pages: Sequence[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for page in pages:
        for line in page.splitlines():
            text = line.strip()
            if not text:
                continue
            if "\t" in text:
                rows.append([part.strip() for part in text.split("\t")])
            elif re.search(r"\s{2,}", text):
                rows.append([part.strip() for part in re.split(r"\s{2,}", text) if part.strip()])
            else:
                rows.append([text])
    return rows
