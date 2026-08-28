"""Document type classification: invoice, receipt, or generic table."""
from __future__ import annotations

import re

from common import INVOICE_KEYWORDS, RECEIPT_KEYWORDS

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_URL = re.compile(r"https?://|www\.|instagram\.com", re.I)
_PHONE = re.compile(r"\b(?:\+?966|05)\d{7,10}\b")
_TABLE_HEADER_HINTS = (
    "الاسم", "اسم الجهة", "عنوان", "ايميل", "الإيميل", "البريد", "الهاتف", "الجوال",
    "المحمول", "موقع", "instagram", "email", "phone", "mobile", "website", "address",
    "sku", "description", "qty", "quantity", "الكمية", "السعر", "الإجمالي", "رمز الشركة",
)
_STRONG_INVOICE = (
    "فاتورة", "invoice", "tax invoice", "vat invoice", "رقم الفاتورة", "الرقم الضريبي",
    "فاتورة ضريبية", "فاتورة ضرائب", "sales invoice", "cash bill",
)


def _keyword_hit(text: str, keyword: str) -> bool:
    """Avoid short tokens like 'trn' matching inside unrelated words."""
    key = (keyword or "").casefold().strip()
    if not key:
        return False
    if len(key) <= 3:
        return re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text) is not None
    return key in text


def _table_signal_score(text: str, column_count: int, row_count: int) -> int:
    lowered = (text or "").casefold()
    score = 0
    if column_count >= 4:
        score += 2
    if row_count >= 8:
        score += 2
    if len(_EMAIL.findall(text or "")) >= 3:
        score += 3
    if len(_URL.findall(text or "")) >= 2:
        score += 2
    if len(_PHONE.findall(text or "")) >= 3:
        score += 2
    score += sum(1 for hint in _TABLE_HEADER_HINTS if hint in lowered)
    return score


def classify_document(text: str, column_count: int = 0, row_count: int = 0, mode: str = "") -> str:
    lowered = (text or "").casefold()
    table_score = _table_signal_score(text or "", column_count, row_count)
    strong_invoice = sum(1 for keyword in _STRONG_INVOICE if _keyword_hit(lowered, keyword))
    invoice_hits = sum(1 for keyword in INVOICE_KEYWORDS if _keyword_hit(lowered, keyword))

    if any(_keyword_hit(lowered, keyword) for keyword in RECEIPT_KEYWORDS) or mode == "receipt":
        if strong_invoice:
            return "invoice"
        if table_score >= 5:
            return "table"
        return "receipt"

    # Spreadsheet / contact / directory tables must not become invoices because of
    # a coincidental short token match (historically: "trn" inside unrelated OCR text).
    if table_score >= 5 and strong_invoice == 0:
        return "table"
    # A real invoice still has an items grid; column count must not hide فاتورة/Invoice.
    if strong_invoice >= 1:
        return "invoice"
    if invoice_hits >= 2 and table_score < 5:
        return "invoice"
    if row_count >= 4 and column_count >= 3:
        return "table"
    return "table" if column_count >= 2 else "invoice"
