"""Shared types and constants for the offline Vertex document worker."""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

SUPPORTED = {
    ".pdf",
    ".doc", ".docx",
    ".xls", ".xlsx", ".xlsm", ".csv",
    ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
    ".txt", ".rtf",
}
IMAGE_TYPES = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".webp"}
TABULAR_TYPES = {".csv", ".xls", ".xlsx", ".xlsm"}
OFFICE_TYPES = {".doc", ".docx", ".ppt", ".pptx"}
TEXT_TYPES = {".txt", ".rtf"}
CONFIDENCE_THRESHOLD = 75.0
YELLOW = "FFF2CC"
RED = "F4CCCC"
SPACE = re.compile(r"\s+")
SYMBOLS = re.compile(r"[*!%\[\]]+")
NUMBER_FIELD = re.compile(
    r"(?:qty|quantity|price|total|amount|tax|invoice|sku|code|id|weight|وزن|رقم|كمية|سعر|إجمالي|ضريبة|قيمة)",
    re.I,
)
ID_FIELD = re.compile(r"(?:id|code|sku|serial|رقم|رمز|شحنة|معرف)", re.I)
DATE_FIELD = re.compile(r"(?:date|تاريخ)", re.I)
FIELD_VALUE = r"[^\n:：]{1,120}"
INVOICE_PATTERNS = {
    "supplier": re.compile(
        r"(?:supplier|vendor|company|المورد|اسم الشركة|الشركة|البائع)[ \t]*[:：-]?[ \t]*(" + FIELD_VALUE + r")",
        re.I,
    ),
    "client_name": re.compile(
        r"(?:customer|client|buyer|العميل|اسم العميل|المشتري|حررت الفاتورة)[ \t]*[:：-]?[ \t]*(" + FIELD_VALUE + r")",
        re.I,
    ),
    "invoice_number": re.compile(
        r"(?:invoice[ \t]*(?:no|number|#)?|رقم الفاتورة|الفاتورة)[ \t]*[.:#：-]?[ \t]*([A-Z0-9][A-Z0-9./_-]{0,40})",
        re.I,
    ),
    "invoice_date": re.compile(
        r"(?:invoice[ \t]*)?(?:date|تاريخ الفاتورة|التاريخ)[ \t]*[:：-]?[ \t]*(\d{1,4}[./-]\d{1,2}[./-]\d{1,4})",
        re.I,
    ),
    "due_date": re.compile(
        r"(?:due[ \t]*date|تاريخ الاستحقاق)[ \t]*[:：-]?[ \t]*(\d{1,4}[./-]\d{1,2}[./-]\d{1,4})",
        re.I,
    ),
    "tax_number": re.compile(
        r"(?:trn|(?:vat|tax)[ \t]*(?:no|number|reg(?:istration)?)|الرقم الضريبي|رقم التسجيل الضريبي)[ \t]*[:：-]?[ \t]*([A-Z0-9-]{5,})",
        re.I,
    ),
    "grand_total": re.compile(
        r"(?:grand[ \t]*total|invoice[ \t]*total|amount[ \t]*due|الإجمالي|المجموع الكلي|المبلغ المستحق|الرصيد المستحق)[ \t]*[:：-]?[ \t]*([\d,]+(?:\.\d{1,3})?)",
        re.I,
    ),
    "subtotal": re.compile(
        r"(?:sub[ \t]*total|subtotal|المجموع الجزئي|المجموع الفرعي)[ \t]*[:：-]?[ \t]*([\d,]+(?:\.\d{1,3})?)",
        re.I,
    ),
    "tax_amount": re.compile(
        r"(?:vat[ \t]*amount|tax[ \t]*amount|value[ \t]*added[ \t]*tax|مبلغ الضريبة|ضريبة القيمة المضافة)[ \t]*[:：-]?[ \t]*([\d,]+(?:\.\d{1,3})?)",
        re.I,
    ),
    "currency": re.compile(r"\b(SAR|AED|USD|EUR|ر\.س|درهم)\b", re.I),
}
HEADER_ALIASES = {
    "supplier": ("supplier", "vendor", "المورد", "البائع", "اسم الشركة", "اسم الجهة"),
    "client_name": ("client", "customer", "customer name", "العميل", "اسم العميل"),
    "invoice_date": ("invoice date", "date", "تاريخ الفاتورة", "التاريخ", "تاريخ الانطلاق"),
    "invoice_number": ("invoice no", "invoice number", "رقم الفاتورة"),
    "tax_number": ("tax number", "vat", "trn", "الرقم الضريبي"),
    "description": ("description", "item", "product", "الوصف", "الصنف", "البيان", "المنتج", "وصف المنتج"),
    "qty": ("qty", "quantity", "الكمية"),
    "unit_price": ("unit price", "price", "rate", "سعر الوحدة", "معدل"),
    "total": ("total", "amount", "الإجمالي", "المبلغ"),
    "status": ("status", "الحالة"),
    "email": ("email", "e-mail", "الإيميل", "البريد"),
    "phone": ("phone", "mobile", "الجوال", "الهاتف", "رقم التواصل"),
    "city": ("city", "المدينة"),
    "region": ("region", "المنطقة"),
    "name": ("name", "الاسم", "اسم الشركة"),
    "id_code": ("id_code", "id", "code", "رمز الشركة", "رقم الشحنة"),
    "sku": ("sku", "item code", "product id", "معرف المنتج"),
}
INVOICE_KEYWORDS = (
    "فاتورة", "invoice", "tax invoice", "vat invoice", "رقم الفاتورة", "الرقم الضريبي",
    "trn", "فاتورة ضريبية", "فاتورة ضرائب", "cash bill", "sales invoice",
)
RECEIPT_KEYWORDS = ("cash bill", "thank you", "change", "rounding", "please come again")


@dataclass
class FileResult:
    source: str
    output: str | None = None
    status: str = "success"
    records: int = 0
    low_confidence: int = 0
    warnings: list[str] | None = None
    error: str | None = None
    template: dict[str, Any] | None = None
    review_items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        if self.status == "failed":
            return "failed"
        if self.status == "unsupported":
            return "unsupported"
        if self.low_confidence:
            return "needs_review"
        if self.warnings:
            return "auto_corrected"
        return "success"


def emit(message: str) -> None:
    # Progress lines travel over a redirected pipe. On many Western Windows
    # installs stdout is cp1252, so raw Arabic in ensure_ascii=False crashes.
    # ASCII-escaped JSON is always printable and C# JsonSerializer unescapes it.
    print(json.dumps({"message": message}, ensure_ascii=True), flush=True)


class quiet_native_output:
    """Silence writes made by native libraries to stdout/stderr.

    llama.cpp logs image-encoding progress from C, which `verbose=False` does
    not reach. The desktop app parses this process's stdout as one JSON object
    per line, so a single stray native log line breaks progress reporting.
    Python-level redirection is not enough; the file descriptors themselves
    have to be swapped.
    """

    def __enter__(self) -> "quiet_native_output":
        self._saved: list[tuple[int, int]] = []
        try:
            self._null = os.open(os.devnull, os.O_WRONLY)
            for target in (1, 2):
                self._saved.append((target, os.dup(target)))
                os.dup2(self._null, target)
        except OSError:
            self.__exit__(None, None, None)
        return self

    def __exit__(self, *_exc: Any) -> None:
        for target, backup in self._saved:
            try:
                os.dup2(backup, target)
                os.close(backup)
            except OSError:
                pass
        self._saved = []
        null = getattr(self, "_null", None)
        if null is not None:
            try:
                os.close(null)
            except OSError:
                pass
            self._null = None


def configure_stdio() -> None:
    """Make redirected pipes accept UTF-8 when the host process allows it."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            continue


configure_stdio()
