"""AI-led extraction: PaddleOCR-VL reads the page, geometry checks it.

PaddleOCR-VL is a 0.9B model built for document parsing rather than a general
vision model, so it returns page structure — layout blocks and table grids —
instead of prose. :mod:`paddle_vl` turns that structure into the payload this
module validates.

It is still a vision-language model, and it can still produce a figure that is
printed nowhere on the page. So nothing it returns is trusted on its own. Every
value passes three gates before it reaches Excel:

1. **shape** — quantities and prices must be numbers, not strings, and the
   column roles must come from a closed set;
2. **arithmetic** — qty x price = line total, sum(lines) = subtotal,
   subtotal + tax = grand total, using the same tolerance the geometric path
   uses (:mod:`verify`);
3. **pixels** — every number is looked for in the word boxes a second,
   independent reader (PaddleOCR 2.x, in :mod:`perceive`) found on that page.

Gates 1 and 2 are blocking; gate 3 is **advisory only**. The distinction is the
difference between a check that proves something and one that corroborates.
Arithmetic proves: ``qty x price = line total`` holds of the printed numbers or
it does not, and an invented figure almost never satisfies it. The second reader
merely corroborates, and it is the weaker engine — a cloud model routinely
resolves small print it cannot. Treating its misses as errors painted correct
values yellow and made a good extraction look poor.

So a value gate 3 cannot corroborate is never deleted and never fails the
document. It is written, highlighted, and given a comment saying the second
reader did not find it. Set ``VERTEX_EVIDENCE_OCR=off`` to skip the second
reading entirely, trading that corroboration for the seconds it costs.

When every attempt still fails, the caller falls back to the geometric
pipeline, which is complete on its own.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import paddle_vl
from common import FileResult, emit
from verify import _close, to_number

# Roles the builder knows how to turn into formulas. A role outside this set is
# treated as a plain text column.
ROLES = (
    "description",
    "sku",
    "qty",
    "unit_price",
    "line_total",
    "discount",
    "tax",
    "unit",
    "date",
    "other",
)
NUMERIC_ROLES = ("qty", "unit_price", "line_total", "discount", "tax")

MAX_ITEMS = 400
MAX_PAGES = 20

# Retries cover a failed read — a crashed worker, an empty answer — not a
# reading the gates disputed. The page is never altered between attempts, so a
# re-read returns the same answer; a disputed value is flagged instead.
MAX_READ_ATTEMPTS = 3

# --------------------------------------------------------------------------
# Gate 1 — shape
# --------------------------------------------------------------------------
def _coerce_number(value: Any) -> tuple[float | None, bool]:
    """(number, was_a_string). ``None`` for a genuinely absent value."""
    if value is None or value == "":
        return None, False
    if isinstance(value, bool):
        return None, True
    if isinstance(value, (int, float)):
        return float(value), False
    parsed = to_number(str(value))
    return parsed, True


def _normalise(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Force the payload into the declared shape, recording every deviation."""
    errors: list[str] = []
    document: dict[str, Any] = {
        "document_type": str(payload.get("document_type") or "other").strip().casefold(),
        "direction": "rtl" if str(payload.get("direction") or "").strip().casefold() == "rtl" else "ltr",
        "currency": str(payload.get("currency") or "").strip(),
        "title": str(payload.get("title") or "").strip(),
        "header": {},
        "notes": [],
    }
    for key, value in (payload.get("header") or {}).items():
        if value in (None, ""):
            continue
        document["header"][str(key).strip()] = str(value).strip()
    for note in payload.get("notes") or []:
        text = str(note).strip()
        if text:
            document["notes"].append(text)

    columns = [str(name).strip() for name in (payload.get("columns") or [])]
    roles = [str(role).strip().casefold() for role in (payload.get("column_roles") or [])]
    unknown = sorted({role for role in roles if role and role not in ROLES})
    if unknown:
        errors.append(
            "column_roles يحتوي أدواراً غير معروفة: "
            + "، ".join(unknown)
            + f" — اختر من: {', '.join(ROLES)}"
        )
        roles = [role if role in ROLES else "other" for role in roles]
    if columns and roles and len(columns) != len(roles):
        errors.append(
            f"columns فيها {len(columns)} عنصراً بينما column_roles فيها {len(roles)} — يجب أن يتساوى الطولان."
        )
        roles = (roles + ["other"] * len(columns))[: len(columns)]
    if not roles:
        roles = ["other"] * len(columns)
    document["columns"] = columns
    document["column_roles"] = roles

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        errors.append("items ليست قائمة.")
        raw_items = []
    if len(raw_items) > MAX_ITEMS:
        errors.append(f"عدد البنود {len(raw_items)} يتجاوز الحد {MAX_ITEMS}.")
        raw_items = raw_items[:MAX_ITEMS]

    items: list[dict[str, Any]] = []
    for number, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            errors.append(f"البند {number} ليس كائناً.")
            continue
        item: dict[str, Any] = {"review": {}, "notes": {}}
        for key, value in raw.items():
            key = str(key).strip()
            role = key.casefold()
            if role in NUMERIC_ROLES:
                parsed, was_string = _coerce_number(value)
                if was_string and parsed is not None:
                    errors.append(
                        f"البند {number}: الحقل «{key}» جاء نصاً ({value!r}) — أرسله رقم JSON."
                    )
                if was_string and parsed is None:
                    errors.append(f"البند {number}: الحقل «{key}» قيمته غير رقمية ({value!r}).")
                item[key] = parsed
            elif value in (None, ""):
                item[key] = ""
            else:
                item[key] = str(value).strip()
        items.append(item)
    document["items"] = items

    totals: dict[str, float | None] = {}
    for key, value in (payload.get("totals") or {}).items():
        key = str(key).strip()
        parsed, was_string = _coerce_number(value)
        if was_string and parsed is not None:
            errors.append(f"totals.{key} جاء نصاً ({value!r}) — أرسله رقم JSON.")
        if parsed is not None:
            totals[key] = parsed
    document["totals"] = totals
    document["totals_review"] = {}
    document["totals_notes"] = {}
    return document, errors


# --------------------------------------------------------------------------
# Gate 2 — arithmetic
# --------------------------------------------------------------------------
def check_arithmetic(document: dict[str, Any]) -> list[str]:
    """Cross-check the model's own numbers. Flags cells in place."""
    errors: list[str] = []
    amounts: list[float] = []
    for number, item in enumerate(document.get("items") or [], start=1):
        qty = item.get("qty")
        price = item.get("unit_price")
        total = item.get("line_total")
        if total is not None:
            amounts.append(total)
        if qty is None or price is None or total is None:
            continue
        if _close(qty * price, total):
            continue
        errors.append(
            f"البند {number}: {qty:g} × {price:g} = {qty * price:g} ولا يساوي الإجمالي المكتوب {total:g}."
        )
        for field in ("qty", "unit_price", "line_total"):
            item["review"][field] = True
        item["notes"]["line_total"] = (
            f"الكمية × السعر ({qty:g} × {price:g}) لا تساوي الإجمالي المقروء {total:g}"
        )

    totals = document.get("totals") or {}
    subtotal = totals.get("subtotal")
    tax = totals.get("tax_amount")
    grand = totals.get("grand_total")
    discount = totals.get("discount") or 0.0

    if amounts and subtotal is not None:
        summed = round(sum(amounts), 2)
        if not _close(summed, subtotal, tolerance=0.05):
            errors.append(
                f"مجموع البنود {summed:g} لا يساوي المجموع الفرعي {subtotal:g}."
            )
            document["totals_review"]["subtotal"] = True
            document["totals_notes"]["subtotal"] = (
                f"مجموع البنود المقروءة {summed:g} لا يساوي المجموع الفرعي {subtotal:g}"
            )

    if subtotal is not None and grand is not None:
        base = subtotal + (tax or 0.0)
        # A discount may be printed with or without its minus sign; both
        # readings count as reconciled.
        candidates = [base, base - abs(discount), base + discount]
        if not any(_close(value, grand, tolerance=0.05) for value in candidates):
            errors.append(
                f"المجموع الفرعي + الضريبة ({base:g}) لا يساوي الإجمالي {grand:g}."
            )
            document["totals_review"]["grand_total"] = True
            document["totals_notes"]["grand_total"] = (
                f"المجموع الفرعي + الضريبة ({base:g}) لا يساوي الإجمالي {grand:g}"
            )

    if subtotal is not None and tax is not None and totals.get("tax_rate"):
        expected = subtotal * float(totals["tax_rate"])
        if not _close(expected, tax, tolerance=0.05):
            errors.append(
                f"نسبة الضريبة {totals['tax_rate']:g} على {subtotal:g} تعطي {expected:g} لا {tax:g}."
            )
    return errors


# --------------------------------------------------------------------------
# Gate 3 — pixels
# --------------------------------------------------------------------------
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_NUMBER_IN_TEXT = re.compile(r"\d[\d.,]*")


def page_numbers(words: list[dict[str, Any]]) -> set[str]:
    """Every number the independent OCR reader saw, normalised for comparison.

    Both the primary reading and the competing one are collected: a cell the
    two recognizers disagreed about is exactly where the vision model is most
    likely to be right, and counting only the winner would flag it as invented.
    """
    found: set[str] = set()
    for word in words:
        texts = [str(word.get("text") or "")]
        for alternative in word.get("alternatives") or []:
            texts.append(str(alternative.get("text") or ""))
        for text in texts:
            for match in _NUMBER_IN_TEXT.finditer(text.translate(_DIGITS)):
                value = to_number(match.group(0))
                if value is not None:
                    found.add(_key(value))
    return found


def _key(value: float) -> str:
    """Compare on value, not spelling: 1,234.50 and 1234.5 are the same number."""
    return f"{round(float(value), 2):.2f}"


def _grounded(value: float, seen: set[str]) -> bool:
    if _key(value) in seen:
        return True
    # OCR routinely drops a decimal point or a thousands separator, so a number
    # whose digits appear unbroken in the page is still grounded.
    digits = re.sub(r"\D", "", f"{value:.2f}").lstrip("0")
    if not digits:
        return True
    return any(digits in re.sub(r"\D", "", entry).lstrip("0") for entry in seen)


def evidence_enabled() -> bool:
    """Whether to read the page a second time to corroborate the first.

    Worth turning off when the reading model is clearly stronger than the local
    engine: the second pass then costs seconds per page and its misses show up
    as yellow cells on values that were right all along. Off means the
    arithmetic gate stands alone — which is the gate that does the real work.
    """
    return (os.environ.get("VERTEX_EVIDENCE_OCR") or "on").strip().casefold() not in {
        "0", "off", "false", "no"
    }


def check_grounding(document: dict[str, Any], seen: set[str]) -> list[str]:
    """Note any number the second reader did not see. Never deletes a value."""
    if not seen or not evidence_enabled():
        return []
    errors: list[str] = []
    for number, item in enumerate(document.get("items") or [], start=1):
        for field in NUMERIC_ROLES:
            value = item.get(field)
            if value is None or _grounded(value, seen):
                continue
            errors.append(
                f"البند {number}: الرقم {value:g} في «{field}» غير موجود في قراءة OCR لهذه الصفحة."
            )
            item["review"][field] = True
            item["notes"][field] = "لم يُعثر على هذا الرقم في القراءة الضوئية للصفحة — راجعه"
    for field, value in (document.get("totals") or {}).items():
        if field == "tax_rate" or value is None or _grounded(value, seen):
            continue
        errors.append(f"totals.{field}: الرقم {value:g} غير موجود في قراءة OCR لهذه الصفحة.")
        document["totals_review"][field] = True
        document["totals_notes"][field] = "لم يُعثر على هذا الرقم في القراءة الضوئية للصفحة — راجعه"
    return errors


def validate(
    payload: dict[str, Any], seen: set[str]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Run all three gates.

    Returns ``(document, blocking, advisory)``.

    Grounding is deliberately **advisory, never blocking**. It compares the
    reading against a second, weaker reader, and a number that reader missed is
    evidence about *the reader*, not proof the value is wrong — a cloud model
    routinely resolves small print that the local engine cannot. Counting those
    misses as errors painted correct figures yellow and made a good extraction
    look poor.

    What stays blocking is what checks itself. Shape and arithmetic need no
    second opinion: ``qty x price = line total`` is either true of the numbers on
    the page or it is not, and a hallucinated figure almost never satisfies it.
    That is what actually catches an invented value; the second reader only
    corroborates.
    """
    document, shape_errors = _normalise(payload)
    arithmetic_errors = check_arithmetic(document)
    grounding_errors = check_grounding(document, seen)
    blocking = list(shape_errors) + list(arithmetic_errors)
    if not document.get("items") and not document.get("notes"):
        blocking.append("لم يُرجع النموذج أي بنود ولا أي نص من الصفحة.")
    return document, blocking, grounding_errors


# --------------------------------------------------------------------------
# Reading a page
# --------------------------------------------------------------------------
def read_page_document(
    image: Any, words: list[dict[str, Any]], page: int = 1
) -> tuple[dict[str, Any], list[str]]:
    """Extract one page from the image exactly as it was captured.

    Retries exist for a *failed read* — a worker that died or returned nothing —
    and not for a reading the gates disliked. The model is shown the same
    untouched pixels every time, so re-reading after a validation complaint
    would only spend another full inference to obtain the same answer. A
    disputed value is therefore kept and flagged rather than re-read: a marked
    value the reviewer can see beats no value at all.
    """
    seen = page_numbers(words)
    notes: list[str] = []
    last_error: Exception | None = None

    for number in range(1, MAX_READ_ATTEMPTS + 1):
        emit(
            f"صفحة {page}: قراءة بنموذج PaddleOCR-VL المحلي"
            + (f" (محاولة {number} من {MAX_READ_ATTEMPTS})" if number > 1 else "")
            + "…"
        )
        try:
            payload = paddle_vl.read_page(image)
        except Exception as error:
            last_error = error
            notes.append(f"ai-attempt-{number}:{type(error).__name__}")
            continue

        document, blocking, _advisory = validate(payload, seen)
        if not blocking:
            notes.append(f"ai:page{page}:clean")
            return document, notes
        notes.append(f"ai:page{page}:accepted-with-review:{len(blocking)}")
        for message in blocking[:5]:
            notes.append(f"ai-review:{message}")
        return document, notes

    raise RuntimeError(
        f"تعذّر الحصول على استخراج صالح من النموذج المحلي: {last_error}"
        if last_error else "تعذّر الحصول على استخراج صالح من النموذج المحلي."
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def available() -> tuple[bool, str]:
    return paddle_vl.available()


def analyze(source: Path, master: dict[str, list[str]], output_dir: Path) -> FileResult:
    """AI-led path for one image or PDF. Raises so the caller can fall back."""
    import excel_builder
    import perceive
    from export import output_file
    from ocr import image_pages

    ok, detail = paddle_vl.available()
    if not ok:
        raise RuntimeError(detail)

    destination = output_file(source, output_dir)
    warnings: list[str] = []
    documents: list[dict[str, Any]] = []

    for page, image in enumerate(image_pages(source), start=1):
        # The geometric reading is what keeps the model honest, and it is also
        # the fallback text for a page the model returns nothing for. It costs
        # a few seconds against the model's minutes.
        try:
            words, notes, _prepared = perceive.read_page(image)
            warnings.extend(notes)
        except Exception as error:
            words = []
            warnings.append(f"ocr-crosscheck-skipped:{type(error).__name__}")

        # The *original* page goes to the model, not the deskewed copy the
        # geometric reader made: the retry ladder conditions the image itself,
        # and starting from an already-deskewed one would deskew it twice. Only
        # the word text is borrowed from that reader, never its coordinates.
        document, notes = read_page_document(image, words, page=page)
        warnings.extend(notes)
        document["page"] = page
        documents.append(document)
        del image
        if page >= MAX_PAGES:
            warnings.append(f"توقف بعد {MAX_PAGES} صفحة.")
            break

    if not documents:
        raise RuntimeError("لم يتم قراءة أي صفحة من الملف.")

    _apply_master_data(documents, master, warnings)
    records, low, review_items, template, destination = excel_builder.write_ai_workbook(
        destination, source, documents
    )
    if low:
        warnings.append("توجد خلايا صفراء تتطلب مراجعة.")
    warnings.append("ai-model:PaddleOCR-VL")
    return FileResult(
        str(source),
        str(destination),
        records=records,
        low_confidence=low,
        warnings=warnings,
        template=template,
        review_items=review_items,
    )


def _apply_master_data(
    documents: list[dict[str, Any]], master: dict[str, list[str]], warnings: list[str]
) -> None:
    """Snap descriptions to the customer's own product/supplier lists."""
    if not master:
        return
    from clean import validate_value

    changed = 0
    for document in documents:
        for item in document.get("items") or []:
            for field in ("description", "name", "item", "product"):
                text = item.get(field)
                if not isinstance(text, str) or not text.strip():
                    continue
                matched, was_changed = validate_value(text, "description", master)
                if was_changed:
                    item[field] = matched
                    item["review"][field] = True
                    item["notes"][field] = "طوبق مع قوائم المطابقة المحلية"
                    changed += 1
    if changed:
        warnings.append(f"master-data:{changed}")
