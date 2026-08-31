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
    }
    for key, value in (payload.get("header") or {}).items():
        if value in (None, ""):
            continue
        document["header"][str(key).strip()] = str(value).strip()

    # The page itself, block by block, exactly as the reader met it. This is
    # what the workbook reproduces; ``header``, ``items`` and ``totals`` beside
    # it are the *interpretation*, used for the arithmetic, the formulas and the
    # data sheet. Keeping the two apart is what lets the customer have both a
    # faithful copy of their invoice and a table they can calculate with.
    document["sections"] = [
        section for section in (payload.get("sections") or [])
        if isinstance(section, dict) and section.get("kind")
    ]
    # The footing rows of the item table, exactly as printed. Their figures have
    # already been banked in ``totals``; these are kept so the sheet can show
    # the line the page actually prints under its table.
    document["item_totals"] = [
        row for row in (payload.get("item_totals") or [])
        if isinstance(row, dict) and row.get("values")
    ]

    columns = [str(name).strip() for name in (payload.get("columns") or [])]
    roles = [str(role).strip().casefold() for role in (payload.get("column_roles") or [])]
    unknown = sorted({role for role in roles if role and role not in ROLES})
    if unknown:
        errors.append(
            "column_roles holds roles that are not recognised: "
            + ", ".join(unknown)
            + f" — choose from: {', '.join(ROLES)}"
        )
        roles = [role if role in ROLES else "other" for role in roles]
    if columns and roles and len(columns) != len(roles):
        errors.append(
            f"columns has {len(columns)} entries but column_roles has {len(roles)} — the two must match."
        )
        roles = (roles + ["other"] * len(columns))[: len(columns)]
    if not roles:
        roles = ["other"] * len(columns)
    document["columns"] = columns
    document["column_roles"] = roles

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        errors.append("items is not a list.")
        raw_items = []
    if len(raw_items) > MAX_ITEMS:
        errors.append(f"{len(raw_items)} items exceeds the limit of {MAX_ITEMS}.")
        raw_items = raw_items[:MAX_ITEMS]

    items: list[dict[str, Any]] = []
    for number, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            errors.append(f"Item {number} is not an object.")
            continue
        item: dict[str, Any] = {"review": {}, "notes": {}}
        for key, value in raw.items():
            key = str(key).strip()
            role = key.casefold()
            if role in NUMERIC_ROLES:
                parsed, was_string = _coerce_number(value)
                if was_string and parsed is not None:
                    errors.append(
                        f"Item {number}: field {key!r} arrived as text ({value!r}) — send it as a JSON number."
                    )
                if was_string and parsed is None:
                    errors.append(f"Item {number}: field {key!r} is not numeric ({value!r}).")
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
            errors.append(f"totals.{key} arrived as text ({value!r}) — send it as a JSON number.")
        if parsed is not None:
            totals[key] = parsed
    document["totals"] = totals
    document["totals_review"] = {}
    document["totals_notes"] = {}
    return document, errors


# --------------------------------------------------------------------------
# Gate 2 — arithmetic
# --------------------------------------------------------------------------
def _digits(value: float) -> str:
    """The digit sequence of a number, without separators or decimal point."""
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return re.sub(r"\D", "", text).lstrip("0") or "0"


def _one_slip_apart(read: float, solved: float) -> bool:
    """Could ``read`` be ``solved`` misread once?

    This is what keeps a repair honest. The arithmetic can always *solve* for a
    missing value, but solving alone would let the pipeline write any number it
    liked over what the page says. So a correction is only accepted when it is
    also a plausible *misreading* — one digit dropped, one digit added, or one
    digit read as another. Those are the slips a reader actually makes; a rate
    of 25,000 read as 2,500 is one of them, and it is the error the customer
    found on their invoice.

    Anything further away is left alone and flagged, because at that distance
    the honest answer is "a human should look", not a confident replacement.
    """
    left, right = _digits(read), _digits(solved)
    if left == right:
        # Same digits, different magnitude: a decimal point in the wrong place.
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(1 for a, b in zip(left, right) if a != b) == 1
    longer, shorter = (left, right) if len(left) > len(right) else (right, left)
    return any(longer[:index] + longer[index + 1:] == shorter for index in range(len(longer)))


def _repair_item(item: dict[str, Any], seen: set[str]) -> tuple[str, float, float] | None:
    """Correct one misread figure in a row, when the evidence identifies it.

    Returns ``(field, was, now)``, or ``None`` when nothing can be shown.

    Solving for the missing value is the easy half. The hard half is knowing
    *which* of the three figures was misread — on a row of round numbers, any
    one of them can be made to fit by a single slip, and picking between them
    at random would be worse than leaving the row flagged. So the candidates
    are narrowed by evidence, in order of how much the evidence proves:

    1. **The second reader.** A figure it also saw on the page is not the
       misreading, and a correction it *did* see is not an invention — it is
       the same pixels read the other way. This is the only rung that settles
       the matter outright, and it needs the local reader installed.
    2. **The direction of the slip.** A reader drops a digit far more often
       than it adds one, so a correction that lengthens the number is more
       likely than one that shortens it.
    3. **Where the slip fits.** Losing a digit from a five-figure rate is a
       likelier accident than losing one from a single-digit quantity, so the
       longer figure is the suspect.

    If those still leave two equally good stories, nothing is written and the
    row stays flagged for a human. Reporting doubt is part of the job.
    """
    qty, price, total = item.get("qty"), item.get("unit_price"), item.get("line_total")
    if qty is None or price is None or total is None or _close(qty * price, total):
        return None

    found: list[tuple[str, float, float]] = []
    if price:
        solved = round(total / price, 4)
        if solved > 0 and _one_slip_apart(qty, solved):
            found.append(("qty", qty, solved))
    if qty:
        solved = round(total / qty, 4)
        if solved > 0 and _one_slip_apart(price, solved):
            found.append(("unit_price", price, solved))
    solved = round(qty * price, 2)
    if solved > 0 and _one_slip_apart(total, solved):
        found.append(("line_total", total, solved))
    if not found:
        return None

    if seen:
        # A figure the page demonstrably carries is not the one that was misread.
        standing = [entry for entry in found if not _grounded(entry[1], seen)]
        # A correction the page demonstrably carries is the reading to take.
        confirmed = [entry for entry in standing if _grounded(entry[2], seen)]
        found = confirmed or standing or found

    if len(found) > 1:
        lengthened = [entry for entry in found if len(_digits(entry[2])) > len(_digits(entry[1]))]
        found = lengthened or found
    if len(found) > 1:
        longest = max(len(_digits(entry[1])) for entry in found)
        found = [entry for entry in found if len(_digits(entry[1])) == longest]
    if len(found) != 1:
        return None

    field, was, now = found[0]
    item[field] = now
    item["review"][field] = True
    item["notes"][field] = (
        f"Read as {was:g}; corrected to {now:g} — one misread digit away, and the "
        f"only reading that makes quantity x price equal the line total"
    )
    return found[0]


def check_arithmetic(document: dict[str, Any], seen: set[str] | None = None) -> list[str]:
    """Cross-check the model's own numbers. Repairs what it can show, flags the rest."""
    errors: list[str] = []
    amounts: list[float] = []
    seen = seen or set()
    for number, item in enumerate(document.get("items") or [], start=1):
        repaired = _repair_item(item, seen)
        if repaired is not None:
            field, was, now = repaired
            message = (
                f"Item {number}: {field} read as {was:g} corrected to {now:g} — "
                "quantity x price now equals the line total."
            )
            errors.append(message)
            # Kept on the document so the workbook can say how many figures it
            # rewrote. A silent correction is a correction nobody can audit.
            document.setdefault("repaired", []).append(message)
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
            f"Item {number}: {qty:g} x {price:g} = {qty * price:g}, which is not the printed line total {total:g}."
        )
        for field in ("qty", "unit_price", "line_total"):
            item["review"][field] = True
        item["notes"]["line_total"] = (
            f"Quantity x price ({qty:g} x {price:g}) does not equal the line total read here, {total:g}"
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
                f"The items add up to {summed:g}, which is not the subtotal {subtotal:g}."
            )
            document["totals_review"]["subtotal"] = True
            document["totals_notes"]["subtotal"] = (
                f"The items read add up to {summed:g}, not the subtotal {subtotal:g}"
            )

    if subtotal is not None and grand is not None:
        base = subtotal + (tax or 0.0)
        # A discount may be printed with or without its minus sign; both
        # readings count as reconciled.
        candidates = [base, base - abs(discount), base + discount]
        if not any(_close(value, grand, tolerance=0.05) for value in candidates):
            errors.append(
                f"Subtotal plus tax ({base:g}) does not equal the total {grand:g}."
            )
            document["totals_review"]["grand_total"] = True
            document["totals_notes"]["grand_total"] = (
                f"Subtotal plus tax ({base:g}) does not equal the total {grand:g}"
            )

    if subtotal is not None and tax is not None and totals.get("tax_rate"):
        expected = subtotal * float(totals["tax_rate"])
        if not _close(expected, tax, tolerance=0.05):
            errors.append(
                f"A tax rate of {totals['tax_rate']:g} on {subtotal:g} gives {expected:g}, not {tax:g}."
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
                f"Item {number}: the figure {value:g} in {field!r} is not in the OCR reading of this page."
            )
            item["review"][field] = True
            item["notes"][field] = "This figure was not found in the independent reading of the page — check it"
    for field, value in (document.get("totals") or {}).items():
        if field == "tax_rate" or value is None or _grounded(value, seen):
            continue
        errors.append(f"totals.{field}: the figure {value:g} is not in the OCR reading of this page.")
        document["totals_review"][field] = True
        document["totals_notes"][field] = "This figure was not found in the independent reading of the page — check it"
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
    # Whether anything actually corroborated these figures against the pixels.
    # Recorded rather than assumed: when the local reader is not installed the
    # third gate silently passes everything, and a page nothing checked must not
    # look on the review sheet like a page that was checked and found clean.
    document["evidence_checked"] = bool(seen) and evidence_enabled()
    arithmetic_errors = check_arithmetic(document, seen)
    grounding_errors = check_grounding(document, seen)
    blocking = list(shape_errors) + list(arithmetic_errors)
    if not document.get("items") and not document.get("header") and not document.get("totals"):
        blocking.append("The model returned no items and no fields from the page.")
    # How the table was read travels with the advisory notes. When a column
    # lands under the wrong heading on a customer's invoice, this is the line
    # that says which columns the reader thought were the quantity and the
    # price, and how well the arithmetic backed it — the difference between
    # diagnosing that page and guessing at it.
    advisory = list(grounding_errors) + [
        f"table: {note}" for note in (payload.get("diagnostics") or [])
    ][:20]
    if not document["evidence_checked"]:
        advisory.append(
            "no independent reading of this page was available, so no figure was "
            "checked against the pixels — install the local reader to enable it"
        )
    return document, blocking, advisory


# --------------------------------------------------------------------------
# Pages of one order
# --------------------------------------------------------------------------
#
# A four-page manifest is one shipment, not four. Read page by page it became
# four sheets, each repeating the same shipper and consignee and each carrying a
# quarter of the goods — so a total covered the page it happened to sit on and
# nothing added up against the paper. These functions put the pages of one order
# back together: the header from the page that printed it, the items from all of
# them in order, the totals from the last page that stated them.
#
# The join is refused rather than guessed at. Two invoices in one PDF stay two
# documents, because merging them would silently sum one customer's goods into
# another's total — a far worse failure than splitting a document that belonged
# together, which a reviewer sees at a glance.
_ORDER_KEYS = ("invoice_number", "purchase_order")


def _identity(document: dict[str, Any]) -> str:
    """The order number this page claims, normalised for comparison."""
    header = document.get("header") or {}
    for key in _ORDER_KEYS:
        value = str(header.get(key) or "").strip()
        if value:
            return re.sub(r"[^0-9a-zء-ي]", "", value.casefold())
    return ""


def _columns_of(document: dict[str, Any]) -> tuple[str, ...]:
    roles = [role for role in (document.get("column_roles") or []) if role != "other"]
    if roles:
        return tuple(roles)
    return tuple(str(name).strip().casefold() for name in (document.get("columns") or []))


def _contradicts(base: dict[str, Any], page: dict[str, Any]) -> bool:
    """Do the two pages disagree about a field they both name?

    Compared on the fields themselves, not on how they were typed: a continuation
    page reprints the consignee in a smaller box and the reader returns it with
    different spacing or a dropped comma.
    """
    base_header = base.get("header") or {}
    page_header = page.get("header") or {}
    for key, value in page_header.items():
        other = base_header.get(key)
        if other is None:
            continue
        left = re.sub(r"\W+", "", str(value).casefold())
        right = re.sub(r"\W+", "", str(other).casefold())
        if left and right and left != right:
            return True
    return False


def continues(base: dict[str, Any], page: dict[str, Any]) -> bool:
    """Is ``page`` the rest of ``base``, rather than a new document?"""
    base_id, page_id = _identity(base), _identity(page)
    if base_id and page_id:
        # Both pages say which order they belong to. Nothing else is needed,
        # and nothing else is allowed to overrule it.
        return base_id == page_id
    if _contradicts(base, page):
        return False
    if not page.get("items"):
        # A page of fields alone — a terms sheet, a signature page — belongs to
        # the document it follows.
        return True
    base_columns, page_columns = _columns_of(base), _columns_of(page)
    return bool(base_columns) and base_columns == page_columns


def _absorb(base: dict[str, Any], page: dict[str, Any]) -> None:
    """Fold a continuation page into the document it continues."""
    base.setdefault("pages", [base.get("page", 1)])
    base["pages"].append(page.get("page", 0))
    for item in page.get("items") or []:
        item.setdefault("_page", page.get("page", 0))
        base.setdefault("items", []).append(item)
    for key, value in (page.get("header") or {}).items():
        # Only what the earlier pages never said. The first page a field appears
        # on is the one that printed it in full.
        base.setdefault("header", {}).setdefault(key, value)
    # The last page to state a total is the one that means it: an earlier page
    # carries a running figure, the last carries the amount due.
    base.setdefault("totals", {}).update(page.get("totals") or {})
    # The later page's own blocks follow the earlier page's, which is the order
    # they were printed in — a continuation sheet's notes and signatures belong
    # after the goods, not interleaved with them.
    base.setdefault("sections", []).extend(
        section for section in (page.get("sections") or [])
        # The item table is written once, from every page's rows together.
        if section.get("kind") not in {"items", "totals"}
    )
    if not base.get("currency"):
        base["currency"] = page.get("currency") or ""
    if not base.get("columns"):
        base["columns"] = page.get("columns") or []
        base["column_roles"] = page.get("column_roles") or []


def merge_pages(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group the pages of one order into one document, in page order."""
    merged: list[dict[str, Any]] = []
    for document in documents:
        document.setdefault("pages", [document.get("page", 1)])
        for item in document.get("items") or []:
            item.setdefault("_page", document.get("page", 1))
        if merged and continues(merged[-1], document):
            _absorb(merged[-1], document)
        else:
            merged.append(document)

    for document in merged:
        if len(document.get("pages") or []) < 2:
            continue
        # The arithmetic was checked a page at a time, against a subtotal that
        # covered only that page. Now that every item is present it is checked
        # against the document's own totals, and the page-level complaints it
        # replaces are cleared rather than left standing.
        document["totals_review"] = {}
        document["totals_notes"] = {}
        check_arithmetic(document)  # the page's own evidence is long gone by now
    return merged


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
            f"Page {page}: reading with the local PaddleOCR-VL model"
            + (f" (attempt {number} of {MAX_READ_ATTEMPTS})" if number > 1 else "")
            + "…"
        )
        try:
            payload = paddle_vl.read_page(image)
        except Exception as error:
            last_error = error
            notes.append(f"ai-attempt-{number}:{type(error).__name__}")
            continue

        document, blocking, advisory = validate(payload, seen)
        notes.extend(advisory[:8])
        if not blocking:
            notes.append(f"ai:page{page}:clean")
            return document, notes
        notes.append(f"ai:page{page}:accepted-with-review:{len(blocking)}")
        for message in blocking[:5]:
            notes.append(f"ai-review:{message}")
        return document, notes

    raise RuntimeError(
        f"No valid extraction could be obtained from the local model: {last_error}"
        if last_error else "No valid extraction could be obtained from the local model."
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
            warnings.append(f"Stopped after {MAX_PAGES} pages.")
            break

    if not documents:
        raise RuntimeError("No page in this file could be read.")

    read = len(documents)
    documents = merge_pages(documents)
    if len(documents) < read:
        warnings.append(f"merged-pages:{read}->{len(documents)}")

    _apply_master_data(documents, master, warnings)
    records, low, review_items, template, destination = excel_builder.write_ai_workbook(
        destination, source, documents
    )
    if low:
        warnings.append("Some cells are highlighted and need a human check.")
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
                    item["notes"][field] = "Matched against the local reference lists"
                    changed += 1
    if changed:
        warnings.append(f"master-data:{changed}")
