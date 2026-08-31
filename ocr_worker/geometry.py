"""Pairing a printed label with the value that belongs to it, by position.

The page was being read top to bottom and then flattened into one string before
any field was looked for. Invoices do not survive that. They put several fields
on a line — ``Invoice No: 118    Date: 04/03/2026    Currency: USD`` — and once
that is one line of text, a pattern hunting for what follows "Invoice No" can
just as easily find "118 Date" or the next label instead of the number.

The independent reader already returns every word with its box, so the value for
a label is simply the text beside it on the same line, which is what a person
reads. Nothing here is specific to one supplier's layout: a label is anything
printed as a label, and whatever sits next to it is its value.

The coordinates come from the OCR pass, not from the vision model, so this works
whichever provider is answering — including the hosted ones, which return text
with no geometry at all.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")

# A label announces itself: it ends with a colon, in either script's punctuation.
_LABEL_END = re.compile(r"[:：﹕︓]\s*$")

# Text that is only punctuation or a stray mark is not a value.
_MEANINGFUL = re.compile(r"[\w؀-ۿ]")


def _mid_y(word: dict[str, Any]) -> float:
    return (float(word.get("y0", 0.0)) + float(word.get("y1", 0.0))) / 2.0


def _height(word: dict[str, Any]) -> float:
    return max(1.0, float(word.get("y1", 0.0)) - float(word.get("y0", 0.0)))


def group_lines(words: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Words gathered into the lines they were printed on.

    Two words share a line when their vertical centres are within half the
    height of the shorter one. That tolerance is what keeps a superscript or a
    slightly rotated scan on the line it belongs to instead of starting a new
    one for every word.
    """
    usable = [
        word for word in words
        if str(word.get("text") or "").strip() and "x0" in word and "y0" in word
    ]
    if not usable:
        return []

    lines: list[list[dict[str, Any]]] = []
    for word in sorted(usable, key=_mid_y):
        placed = False
        for line in reversed(lines):
            reference = line[-1]
            tolerance = min(_height(word), _height(reference)) * 0.5
            if abs(_mid_y(word) - _mid_y(reference)) <= tolerance:
                line.append(word)
                placed = True
                break
        if not placed:
            lines.append([word])

    for line in lines:
        line.sort(key=lambda item: float(item.get("x0", 0.0)))
    return lines


def _line_is_rtl(line: list[dict[str, Any]]) -> bool:
    arabic = sum(1 for word in line if ARABIC.search(str(word.get("text") or "")))
    return arabic * 2 > len(line)


def _join(words: list[dict[str, Any]], rtl: bool) -> str:
    """The text of a run of words, in the order its own script reads."""
    ordered = sorted(words, key=lambda item: float(item.get("x0", 0.0)), reverse=rtl)
    return re.sub(r"\s+", " ", " ".join(str(w.get("text") or "") for w in ordered)).strip()


def label_value_pairs(words: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    """Every ``label: value`` the page prints, paired by where they sit.

    A line may hold several. The value of a label runs from just after it to the
    start of the next label on that line — so three fields printed side by side
    come back as three pairs, not as one sentence containing all six pieces.
    """
    pairs: list[tuple[str, str]] = []
    lines = group_lines(words)

    for index, line in enumerate(lines):
        rtl = _line_is_rtl(line)
        # Reading order, so "next" below means the word a reader meets next.
        ordered = sorted(line, key=lambda item: float(item.get("x0", 0.0)), reverse=rtl)

        # Where each label ends. Everything up to the following label is its value.
        starts = [
            position for position, word in enumerate(ordered)
            if _LABEL_END.search(str(word.get("text") or ""))
        ]
        if not starts:
            continue

        # Where each label begins. Between one colon and the next lie two things
        # run together: the tail of the previous field's value, then the next
        # label. The page separates its columns with white space, so the widest
        # gap in that stretch is the join — which is how a reader tells
        # "INV-118    Date:" apart without knowing what either means.
        label_starts: list[int] = []
        for number, position in enumerate(starts):
            begin = starts[number - 1] + 1 if number else 0
            label_starts.append(
                begin if number == 0 else _widest_gap(ordered, begin, position, rtl)
            )

        for number, position in enumerate(starts):
            begin = label_starts[number]
            label = _LABEL_END.sub("", _join(ordered[begin:position + 1], rtl)).strip()

            # The value runs to the start of the next label, not to its colon.
            stop = label_starts[number + 1] if number + 1 < len(starts) else len(ordered)
            value = _join(ordered[position + 1:stop], rtl)

            if not value and index + 1 < len(lines):
                # Some layouts print the value under its label rather than beside
                # it. Only accept that when the two actually line up.
                value = _below(ordered[begin:position + 1], lines[index + 1])

            if label and value and _MEANINGFUL.search(label) and _MEANINGFUL.search(value):
                pairs.append((label, value))
    return pairs


def _gap(left: dict[str, Any], right: dict[str, Any], rtl: bool) -> float:
    """The white space between two words that are next to each other in reading order."""
    if rtl:
        return float(left.get("x0", 0.0)) - float(right.get("x1", 0.0))
    return float(right.get("x0", 0.0)) - float(left.get("x1", 0.0))


def _widest_gap(ordered: list[dict[str, Any]], begin: int, end: int, rtl: bool) -> int:
    """Index where the next label starts, inside ``ordered[begin:end + 1]``.

    Falls back to the word just before the colon, which is the shortest label a
    document can print and a safer guess than swallowing the previous value.
    """
    best_index, best_gap = end, -1.0
    for position in range(begin + 1, end + 1):
        gap = _gap(ordered[position - 1], ordered[position], rtl)
        if gap > best_gap:
            best_index, best_gap = position, gap
    return best_index


# Which printed label means which field. Matched against the label alone —
# never against a whole line — so "Invoice No" cannot capture the date printed
# beside it. Anything not listed keeps the label the document itself used.
_CANONICAL: list[tuple[str, re.Pattern[str]]] = [
    ("invoice_number", re.compile(r"invoice\s*(?:no|number|#|num)?\b|رقم\s*الفاتورة|^الفاتورة$", re.I)),
    ("invoice_date", re.compile(r"^(?:invoice\s*)?date\b|تاريخ\s*الفاتورة|^التاريخ$", re.I)),
    ("due_date", re.compile(r"due\s*date|تاريخ\s*الاستحقاق", re.I)),
    ("supplier", re.compile(r"supplier|vendor|seller|sold\s*by|المورد|البائع", re.I)),
    ("client_name", re.compile(r"customer|client|buyer|bill\s*to|sold\s*to|العميل|المشتري", re.I)),
    # Shipping paperwork names its two parties by their role in the movement,
    # not in the sale. Order matters twice over here: the phone and address
    # lines are tried before the bare party name so "Shipper Phone" is not
    # swallowed by the pattern for "Shipper", and the consignee is tried before
    # the shipper because Arabic writes it as المرسل إليه — the shipper's own
    # word with one more after it.
    ("consignee_phone", re.compile(
        r"(?:consignee|receiver|recipient|deliver\s*to|ship\s*to)[^\w]*"
        r"(?:phone|tel|mobile|contact)"
        r"|(?:هاتف|جوال|تلفون)\s*(?:المرسل\s*إليه|المستلم)", re.I)),
    ("shipper_phone", re.compile(
        r"(?:shipper|sender|consignor)[^\w]*(?:phone|tel|mobile|contact)"
        r"|(?:هاتف|جوال|تلفون)\s*(?:المرسل|الشاحن)", re.I)),
    ("consignee_address", re.compile(
        r"(?:consignee|receiver|recipient)[^\w]*address|عنوان\s*(?:المرسل\s*إليه|المستلم)", re.I)),
    ("shipper_address", re.compile(
        r"(?:shipper|sender|consignor)[^\w]*address|عنوان\s*(?:المرسل|الشاحن)", re.I)),
    ("consignee", re.compile(
        r"consignee|receiver|recipient|deliver\s*to|ship\s*to|المرسل\s*إليه|المستلم", re.I)),
    ("shipper", re.compile(r"shipper|consignor|\bsender\b|المرسل|الشاحن", re.I)),
    ("tax_number", re.compile(r"\b(?:vat|tax|gst)\s*(?:no|number|id|reg)|الرقم\s*الضريبي", re.I)),
    ("payment_terms", re.compile(r"payment\s*terms?|\bterms\b|شروط\s*الدفع", re.I)),
    ("purchase_order", re.compile(r"\b(?:po|p\.o\.|purchase\s*order)\b|أمر\s*الشراء", re.I)),
]

# Labels whose value is an amount and belongs with the totals, not the header.
_TOTAL_LABELS: list[tuple[str, re.Pattern[str]]] = [
    # Tried in this order, and the order carries meaning: "المبلغ خاضع للضريبة"
    # is the taxable subtotal and must be claimed before the tax pattern sees
    # the word ضريبة inside it. Every one of these is a label a real invoice
    # printed — there is no house style to rely on, so the vocabulary is wide.
    ("subtotal", re.compile(
        r"sub\s*total|taxable\s*(?:amount|value)|net\s*(?:amount|value)"
        r"|المجموع\s*الفرعي|الإجمالي\s*قبل|خاضع\s*للضريب[ةه]|المبلغ\s*الخاضع"
        r"|^مجموع$|^المجموع$|^الإجمالي\s*الفرعي$", re.I)),
    ("discount", re.compile(r"discount|rebate|الخصم|خصم", re.I)),
    ("tax_amount", re.compile(
        r"^(?:vat|tax|gst)\b(?!\s*(?:no|number|id|reg))|^الضريبة|القيمة\s*المضافة"
        r"|مبلغ\s*الضريب[ةه]|قيمة\s*الضريب[ةه]", re.I)),
    # ``^due$`` matches the word alone and nothing more, so a receipt's "Due
    # 943.00" is read as what is owed while "Due Date: 2025-11-10" — which is
    # tried against these patterns first — is left for the header fields.
    ("grand_total", re.compile(
        r"grand\s*total|total\s*due|amount\s*due|balance\s*due|net\s*total"
        r"|invoice\s*total|total\s*invoice(?:\s*value|\s*amount)?"
        r"|^due$|^total$"
        r"|الإجمالي\s*النهائي|المبلغ\s*المستحق|^المستحق$|مبلغ\s*الفاتورة|إجمالي\s*الفاتورة"
        r"|المجموع\s*الكلي|صافي\s*الفاتورة|^الإجمالي$", re.I)),
]

_AMOUNT = re.compile(r"-?\d[\d,٬\s]*(?:[.٫]\d+)?")


def canonical_field(label: str) -> str | None:
    for name, pattern in _CANONICAL:
        if pattern.search(label):
            return name
    return None


def total_field(label: str) -> str | None:
    for name, pattern in _TOTAL_LABELS:
        if pattern.search(label):
            return name
    return None


def to_number(text: str) -> float | None:
    match = _AMOUNT.search(str(text).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
    if not match:
        return None
    cleaned = re.sub(r"[,\s٬]", "", match.group(0)).replace("٫", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def read_fields(words: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Header fields and totals, each read from beside its own printed label.

    Returns ``{"header": {...}, "totals": {...}, "extra": {...}}``. ``extra``
    holds labels this product has no name for — a delivery note reference, a
    project code — which are kept under the document's own wording rather than
    dropped, because the next customer's invoice will carry different ones.
    """
    header: dict[str, str] = {}
    totals: dict[str, float] = {}
    extra: dict[str, str] = {}

    for label, value in label_value_pairs(words):
        total_name = total_field(label)
        if total_name is not None:
            amount = to_number(value)
            if amount is not None and total_name not in totals:
                totals[total_name] = amount
            continue

        field = canonical_field(label)
        if field is not None:
            header.setdefault(field, value)
        elif len(label) <= 40 and label not in extra:
            extra[label] = value

    return {"header": header, "totals": totals, "extra": extra}


def _below(label_words: list[dict[str, Any]], next_line: list[dict[str, Any]]) -> str:
    """The text under a label, when it sits under rather than beside it."""
    left = min(float(word.get("x0", 0.0)) for word in label_words)
    right = max(float(word.get("x1", 0.0)) for word in label_words)
    width = max(1.0, right - left)
    rtl = _line_is_rtl(next_line)
    ordered = sorted(next_line, key=lambda item: float(item.get("x0", 0.0)), reverse=rtl)
    anchor = next(
        (
            position for position, word in enumerate(ordered)
            # Overlapping the label's own column by a third is enough to be
            # "under" it and little enough to exclude the neighbouring field.
            if min(right, float(word.get("x1", 0.0))) - max(left, float(word.get("x0", 0.0)))
            > width * 0.33
        ),
        None,
    )
    if anchor is None:
        return ""

    # A name is wider than its label: "Bill To:" sits over "Northwind Trading
    # Ltd". So the value is the whole run of words the anchor belongs to, out to
    # the white space that separates it from the next column.
    height = max(_height(word) for word in ordered)
    limit = height * 1.6
    first, last = anchor, anchor
    while first > 0 and _gap(ordered[first - 1], ordered[first], rtl) <= limit:
        first -= 1
    while last + 1 < len(ordered) and _gap(ordered[last], ordered[last + 1], rtl) <= limit:
        last += 1
    return _join(ordered[first:last + 1], rtl)
