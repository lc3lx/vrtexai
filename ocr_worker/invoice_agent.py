"""Invoice Agent: gather OCR evidence → decide structured fields → verify → Excel.

This is intentionally not "dumb layout dump".  The agent:
1) reads the page with high-accuracy OCR (and optional structure tables),
2) decides header / items / totals (local LLM when bundled, else reasoning parser),
3) checks qty×price and totals before writing Excel.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from clean import validate_value
from common import CONFIDENCE_THRESHOLD, FileResult
from export import output_file, write_invoice
from invoice import extract_invoice_fields, totals_mismatch
from ocr import image_pages, setup_tesseract

_LLM = None
_LLM_ERROR: str | None = None


def llm_root() -> Path:
    env = os.environ.get("VERTEX_LLM_MODELS")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    candidates = [
        here / "llm_models",
        here.parent / "runtime" / "llm_models",
        Path(os.environ.get("PYTHONHOME") or "") / "llm_models",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[0]


def find_gguf() -> Path | None:
    root = llm_root()
    if not root.is_dir():
        return None
    preferred = (
        "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
        "qwen2.5-1.5b-instruct-q4_k_m.gguf",
    )
    for name in preferred:
        path = root / name
        if path.is_file():
            return path
    matches = sorted(root.glob("*.gguf"))
    return matches[0] if matches else None


def _money(text: str) -> str:
    match = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", text or "")
    return match.group(1).replace(",", "") if match else ""


def _to_float(value: str) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def gather_evidence(source: Path) -> dict[str, Any]:
    """Collect page text and candidate tables without inventing fields."""
    import numpy as np
    from PIL import Image

    pytesseract = setup_tesseract()
    pages_text: list[str] = []
    tables: list[list[list[str]]] = []
    warnings: list[str] = []

    for image in image_pages(source):
        rgb = Image.fromarray(np.asarray(image)).convert("RGB")
        # Dual pass: block layout + sparse for screen photos / moiré.
        texts = []
        # Two high-value passes keep 8GB CPUs responsive while covering EN/AR.
        for lang, config in (("eng", "--oem 1 --psm 6"), ("eng+ara", "--oem 1 --psm 6")):
            try:
                texts.append(pytesseract.image_to_string(rgb, lang=lang, config=config) or "")
            except Exception:
                continue
        # Prefer the pass with the most invoice-like tokens (INV / Total / Item / numbers).
        def _score(t: str) -> int:
            return (
                len(re.findall(
                    r"(?i)\b(?:invoice|item|total|subtotal|customer|qty|quantity|tax|inv-|"
                    r"فاتور|الكمية|السعر|المبلغ|المجموع|الضريبة|العميل|المورد)\b",
                    t,
                ))
                * 50
                + len(re.findall(r"\$\s*\d+\.\d{2}|\d+[.,]\d{2}", t)) * 20
                + len(re.findall(r"[A-Za-z\u0600-\u06FF0-9]", t))
            )

        best = max(texts, key=_score, default="")
        if best.strip():
            pages_text.append(best.strip())
        # Always gather a grid candidate.  Arabic/table invoices rarely match
        # English "Item N" patterns; the local grid is required for generality.
        try:
            from ocr import ocr_page_table

            result = ocr_page_table(image, include_page_text=False)
            table, _scores, mode = result[0], result[1], result[2]
            if table and len(table) >= 2:
                tables.append(table)
            if mode:
                warnings.append(f"grid:{mode}")
        except Exception as error:
            warnings.append(f"grid-skip:{type(error).__name__}")
        del image

    # Optional Paddle Structure — evidence only. Disabled by default because it is
    # heavy on 8GB CPUs and must never be the sole decider.
    if os.environ.get("VERTEX_USE_PADDLE_STRUCTURE", "").strip() in {"1", "true", "yes"}:
        try:
            from invoice_ai import analyze_image as paddle_analyze

            for image in image_pages(source):
                paddle_tables, lines, page_warnings = paddle_analyze(image)
                tables.extend(paddle_tables)
                if lines:
                    pages_text.append("\n".join(lines))
                warnings.extend(page_warnings)
                del image
                break
        except Exception as error:
            warnings.append(f"structure-skip:{type(error).__name__}")

    blob = "\n".join(pages_text)
    return {"text": blob, "pages": pages_text, "tables": tables, "warnings": warnings}


def _llm_chat(prompt: str) -> str:
    global _LLM, _LLM_ERROR
    gguf = find_gguf()
    if gguf is None:
        raise RuntimeError("لا يوجد نموذج فهم محلي (GGUF).")
    if _LLM_ERROR:
        raise RuntimeError(_LLM_ERROR)
    if _LLM is None:
        try:
            from llama_cpp import Llama  # type: ignore
        except Exception as error:
            _LLM_ERROR = f"llama-cpp غير متاح: {error}"
            raise RuntimeError(_LLM_ERROR) from error
        # Tight settings for 8GB machines: one model, small context, few threads.
        threads = max(1, min(4, (os.cpu_count() or 2) - 1))
        _LLM = Llama(
            model_path=str(gguf),
            n_ctx=4096,
            n_threads=threads,
            n_batch=128,
            verbose=False,
        )
    system = (
        "You are an invoice extraction agent. "
        "Return ONLY valid JSON with keys header, items, totals. "
        "header: supplier, client_name, invoice_number, invoice_date, due_date, tax_number, currency. "
        "items: array of {description, sku, qty, unit_price, total}. "
        "totals: subtotal, tax_amount, grand_total, currency. "
        "Never invent amounts. Use empty string when unknown. "
        "Prefer real invoice numbers like INV-... over filename words."
    )
    result = _LLM.create_chat_completion(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt[:12000]},
        ],
        temperature=0.0,
        max_tokens=1200,
    )
    return str(result["choices"][0]["message"]["content"] or "")


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object")
    return json.loads(text[start : end + 1])


def decide_with_llm(evidence: dict[str, Any]) -> dict[str, Any] | None:
    text = evidence.get("text") or ""
    if len(text.strip()) < 20:
        return None
    prompt = (
        "Extract the invoice into JSON.\n"
        "OCR text follows:\n"
        f"{text}\n"
    )
    try:
        raw = _llm_chat(prompt)
        parsed = _extract_json(raw)
    except Exception:
        return None
    return _normalize_parsed(parsed, source="llm", confidence=82.0)


def decide_with_parser(evidence: dict[str, Any]) -> dict[str, Any]:
    """Deterministic reasoning over OCR text for common invoice layouts."""
    raw_text = evidence.get("text") or ""
    # Drop browser/filename chrome that poisons invoice-number regexes.
    cleaned_lines = []
    for line in raw_text.splitlines():
        compact = re.sub(r"\s+", " ", line).strip()
        if not compact:
            continue
        if re.search(r"(?i)\.(?:jpg|jpeg|png|pdf|xlsx?)\b|excel-template|sales-invoice-excel|_max\.|aaa216", compact):
            continue
        cleaned_lines.append(compact)
    lines = cleaned_lines
    joined = "\n".join(lines)

    header = extract_invoice_fields(lines)
    # Stronger English sales-invoice patterns (screen photos / templates).
    patterns = {
        "invoice_number": r"(?:invoice\s*(?:no\.?|number|#)|رقم الفاتورة)\s*[:#._\-\s]*([A-Z]{2,5}[-_]?\d{4,}[A-Z0-9-]*)",
        "invoice_date": r"(?:^|\n)\s*date\s+(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})",
        "client_name": r"(?:customer|client|buyer|العميل)\s+([A-Za-z\u0600-\u06FF0-9 .&'-]{2,60})",
        "supplier": r"(?:supplier|vendor|from|المورد)\s+([A-Za-z\u0600-\u06FF0-9 .&'-]{2,60})",
        "subtotal": r"sub\s*total\s*\$?\s*([\d,]+\.\d{2})",
        "tax_amount": r"(?:tax(?:\s*\(\s*\d+\s*%\s*\))?|vat)\s*\$?\s*([\d,]+\.\d{2})",
        "grand_total": r"(?i)(?:^|\n|\|)\s*total(?!\s*(?:price|qty|quantity|amount))\s*\$?\s*([\d,]+\.\d{2})",
        "currency": r"\$(?=\s*\d)|\b(USD|SAR|AED|EUR)\b",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, joined, re.I | re.M)
        if not match:
            continue
        if key == "currency":
            header[key] = "USD" if "$" in match.group(0) else match.group(1).upper()
        else:
            header[key] = match.group(1).strip()
    # Fallback: Total line anywhere near end of document.
    if not header.get("grand_total"):
        match = re.search(r"(?is)total\s*\$?\s*([\d,]+\.\d{2})\s*(?:\n|$).{0,80}(?:payment|credit|method)?", joined)
        if match:
            header["grand_total"] = match.group(1).strip()

    # Always prefer a real INV-#### token over OCR leftovers.
    inv = re.search(r"\b(INV[-_]?\d{4,}[A-Z0-9-]*)\b", joined, re.I)
    if inv:
        header["invoice_number"] = inv.group(1)
    else:
        bad_numbers = {"excel", "template", "sales", "invoice", "max", "jpg", "png", "pdf"}
        number = str(header.get("invoice_number") or "")
        if number.casefold() in bad_numbers or "template" in number.casefold() or "excel" in number.casefold():
            header.pop("invoice_number", None)

    items: list[dict[str, Any]] = []

    def _push_item(desc: str, qty: str, price: str, total: str, confidence: float = 85.0) -> None:
        qty_f, price_f, total_f = _to_float(qty), _to_float(price), _to_float(total)
        if qty_f is None or price_f is None:
            return
        if qty_f <= 0 or qty_f > 100000 or price_f < 0 or price_f > 1_000_000:
            return
        # Reject date-like garbage (qty=2022 etc.).
        if qty_f >= 1900 and qty_f <= 2100 and price_f < 32:
            return
        # Recover lost decimals: "$15.00" OCR'd as "1500".
        if price_f >= 100 and "." not in str(price):
            for div in (100.0, 10.0):
                alt = price_f / div
                if total_f is not None and abs(qty_f * alt - total_f) <= max(0.05, abs(total_f) * 0.02):
                    price_f = alt
                    break
                if total_f is not None and total_f < price_f and abs(qty_f * total_f - alt) <= 0.05:
                    price_f, total_f = total_f, round(qty_f * total_f, 2)
                    break
                if total_f is None and alt < 1000:
                    # Keep candidate; final choice after expected calc.
                    pass
        expected = round(qty_f * price_f, 2)
        review = False
        if total_f is None:
            total_f = expected
            confidence = min(confidence, 70.0)
            review = True
        elif expected > 0 and not (0.5 <= total_f / expected <= 1.5):
            # Prefer qty×price when printed total is OCR-garbled ($10.00 → 5100).
            if price_f >= 100 and "." not in str(price):
                for div in (100.0, 10.0):
                    alt_price = price_f / div
                    alt_expected = round(qty_f * alt_price, 2)
                    if abs(alt_expected - total_f) <= max(0.05, abs(total_f) * 0.02) or total_f > alt_expected * 5:
                        price_f = alt_price
                        expected = alt_expected
                        break
            total_f = expected
            confidence = min(confidence, 60.0)
            review = True
        elif abs(expected - total_f) > max(0.05, abs(total_f) * 0.02):
            review = True
            confidence = min(confidence, 55.0)
        items.append({
            "description": desc.strip() or f"Item {len(items) + 1}",
            "sku": "",
            "qty": f"{qty_f:g}",
            "unit_price": f"{price_f:.2f}",
            "total": f"{total_f:.2f}",
            "confidence": confidence,
            "review": review,
        })

    # Vertical OCR reconstruction: "item 3" / "tem 3" then qty, price, total on following lines.
    i = 0
    while i < len(lines):
        line = lines[i]
        label = re.match(r"(?i)^(?:item|tem|itern|iłem)\s*(\d+)\s*$", line)
        label2 = re.match(r"(?i)^(?:item|tem)\s*(\d+)\s+(\d+(?:\.\d+)?)\s+\$?\s*([\d.]+)\s+\$?\s*([\d.]+)\s*$", line)
        if label2:
            _push_item(f"Item {label2.group(1)}", label2.group(2), label2.group(3), label2.group(4), 90.0)
            i += 1
            continue
        if label:
            nums: list[str] = []
            j = i + 1
            while j < len(lines) and len(nums) < 4:
                raw = lines[j]
                money = re.fullmatch(r"\$?\s*([\d,]+(?:\.\d{1,2})?)\s*", raw)
                if money:
                    nums.append(money.group(1))
                    j += 1
                    continue
                if re.fullmatch(r"[A-Za-z|=\[\]']{1,4}", raw):
                    j += 1
                    continue
                break
            money_vals = [n for n in nums if "." in n]
            int_vals = [n for n in nums if "." not in n]
            if int_vals and money_vals:
                qty = int_vals[0]
                price = money_vals[0]
                total = money_vals[1] if len(money_vals) > 1 else ""
                _push_item(f"Item {label.group(1)}", qty, price, total, 88.0)
                i = j
                continue
        # Single-line: Item 1 10 $10.00 $100.00
        match = re.search(
            r"(?i)(?:item|tem)\s*(\d+)\D{0,8}(\d+(?:\.\d+)?)\D{0,8}\$?\s*([\d.]+)\D{0,12}\$?\s*([\d.]+)?",
            line,
        )
        if match and not re.search(r"(?i)sub\s*total|\btax\b|payment|description", line):
            _push_item(f"Item {match.group(1)}", match.group(2), match.group(3), match.group(4) or "", 86.0)
        i += 1

    # Money-row fallback when labels died but qty/price/total triples remain near "item".
    if len(items) < 2:
        for match in re.finditer(
            r"(?is)(?:item|tem)\s*(\d+)\W+(\d+(?:\.\d+)?)\W+\$?\s*([\d.]+)\W+\$?\s*([\d.]+)",
            joined,
        ):
            _push_item(f"Item {match.group(1)}", match.group(2), match.group(3), match.group(4), 78.0)

    # De-duplicate by description, keep highest-confidence row.
    dedup: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item.get("description") or "").casefold()
        prev = dedup.get(key)
        if prev is None or float(item.get("confidence") or 0) >= float(prev.get("confidence") or 0):
            dedup[key] = item
    items = list(dedup.values())
    # Stable order by item number when labeled.
    def _item_key(item: dict[str, Any]):
        match = re.match(r"(?i)^item\s*(\d+)", str(item.get("description") or "").strip())
        return (0, int(match.group(1))) if match else (1, str(item.get("description") or ""))
    items.sort(key=_item_key)

    # If line totals explode past the printed subtotal, recover lost decimals on prices.
    sub_check = _to_float(header.get("subtotal") or "")
    items_sum = sum(_to_float(item["total"]) or 0 for item in items)
    if sub_check and items_sum > max(sub_check * 1.5, sub_check + 5):
        repaired: list[dict[str, Any]] = []
        for item in items:
            qty_f = _to_float(item["qty"])
            price_f = _to_float(item["unit_price"])
            if qty_f is None or price_f is None:
                repaired.append(item)
                continue
            if price_f >= 100:
                for div in (100.0, 10.0):
                    alt = price_f / div
                    if alt <= 0:
                        continue
                    new_total = round(qty_f * alt, 2)
                    # Accept if this keeps the basket near the printed subtotal.
                    trial_sum = items_sum - (_to_float(item["total"]) or 0) + new_total
                    if abs(trial_sum - sub_check) < abs(items_sum - sub_check):
                        item = {
                            **item,
                            "unit_price": f"{alt:.2f}",
                            "total": f"{new_total:.2f}",
                            "confidence": min(float(item.get("confidence") or 100), 65.0),
                            "review": True,
                        }
                        items_sum = trial_sum
                        break
            repaired.append(item)
        items = repaired

    # Pipe-table / shattered rows with qty + $price + $total.
    if len(items) < 4:
        for line in lines:
            if re.search(r"(?i)sub\s*total|\btax\b|grand|payment|description\s*\|", line):
                continue
            match = re.search(
                r"(?i)(?:item\s*)?(\d{1,4})?\D{0,12}?(\d{1,4})\s+\$\s*([\d]+(?:\.\d{2})?)\s*.{0,12}?\$?\s*([\d]+(?:\.\d{2})?)",
                line,
            )
            if match:
                label, qty, price, total = match.groups()
                desc = f"Item {label}" if label and int(label) <= 50 else f"Item {len(items) + 1}"
                if _to_float(qty) and _to_float(qty) <= 10000:
                    _push_item(desc, qty, price, total, 85.0)
                continue
            if not re.search(r"\$\s*[\d.]+", line):
                continue
            cells = [c.strip() for c in re.split(r"\|", line) if c.strip()]
            money_cells = [c for c in cells if re.search(r"\$?\s*\d+[.,]\d{2}", c)]
            qty_cells = [
                c for c in cells
                if re.fullmatch(r"\d{1,4}", c.replace(",", "")) and int(c.replace(",", "")) < 1000
            ]
            if len(money_cells) >= 2 and qty_cells:
                qty = qty_cells[-1]  # qty usually sits just before money columns
                price = _money(money_cells[0])
                total = _money(money_cells[1])
                desc_cells = [
                    c for c in cells
                    if c not in qty_cells and all(c not in m for m in money_cells)
                ]
                desc = next((c for c in desc_cells if re.search(r"[A-Za-z\u0600-\u06FF]", c)), f"Item {len(items) + 1}")
                if re.search(r"(?i)item\s*(\d+)", desc):
                    desc = f"Item {re.search(r'(?i)item\s*(\d+)', desc).group(1)}"
                _push_item(desc, qty, price, total, 84.0)

    # Prefer table parser when a usable grid exists (Arabic invoices especially).
    best_table = None
    best_table_items = 0
    for table in evidence.get("tables") or []:
        try:
            from invoice import invoice_table_is_reliable, parse_invoice_table

            parsed = parse_invoice_table(table, None, evidence.get("pages") or [])
            count = len(parsed.get("items") or [])
            if count > best_table_items:
                best_table, best_table_items = parsed, count
            if invoice_table_is_reliable(table) and count >= max(2, len(items)):
                return _normalize_parsed(parsed, source="table", confidence=90.0)
        except Exception:
            continue
    if best_table and best_table_items > len(items):
        table_items = best_table.get("items") or []
        table_sum = sum(_to_float(item.get("total")) or 0 for item in table_items)
        parser_sum = sum(_to_float(item.get("total")) or 0 for item in items)
        sub = _to_float(header.get("subtotal") or (best_table.get("header") or {}).get("subtotal") or "")
        if sub:
            if abs(table_sum - sub) + 0.01 < abs(parser_sum - sub):
                return _normalize_parsed(best_table, source="table", confidence=78.0)
        elif not items:
            return _normalize_parsed(best_table, source="table", confidence=78.0)

    # Mark arithmetic mismatches for review.
    for item in items:
        if totals_mismatch(item["qty"], item["unit_price"], item["total"]):
            item["review"] = True
            item["confidence"] = min(float(item.get("confidence") or 100), 55.0)

    totals = {
        "subtotal": header.get("subtotal", ""),
        "tax_amount": header.get("tax_amount", ""),
        "grand_total": header.get("grand_total", ""),
        "currency": header.get("currency", "") or ("USD" if "$" in joined else ""),
    }
    # Recover tax when OCR mangled it but subtotal + grand total are present.
    sub_f = _to_float(totals.get("subtotal") or "")
    grand_f = _to_float(totals.get("grand_total") or "")
    tax_f = _to_float(totals.get("tax_amount") or "")
    if sub_f is not None and grand_f is not None:
        expected_tax = round(grand_f - sub_f, 2)
        if tax_f is None or abs(tax_f - expected_tax) > 0.05 or tax_f > grand_f:
            totals["tax_amount"] = f"{expected_tax:.2f}"
            header["tax_amount"] = totals["tax_amount"]
    elif sub_f is not None and tax_f is not None and not totals.get("grand_total"):
        totals["grand_total"] = f"{round(sub_f + tax_f, 2):.2f}"
        header["grand_total"] = totals["grand_total"]
        grand_f = _to_float(totals["grand_total"])
    if items and not totals["grand_total"]:
        # Only synthesize grand total when printed total is missing AND line
        # arithmetic is coherent (avoid promoting broken OCR sums).
        amounts = [_to_float(item["total"]) for item in items]
        if all(v is not None for v in amounts) and not any(item.get("review") for item in items):
            totals["grand_total"] = f"{sum(v or 0 for v in amounts):.2f}"
            totals.setdefault("subtotal", totals["grand_total"])
    # Never let broken line sums override a coherent printed total.
    printed_grand = _to_float(totals.get("grand_total") or "")
    items_sum = sum(_to_float(item["total"]) or 0 for item in items)
    if printed_grand is not None and items and abs(items_sum - printed_grand) > max(1.0, printed_grand * 0.2):
        for item in items:
            item["review"] = True

    # Drop OCR-shatter rows before returning.
    items = [
        item for item in items
        if not re.search(r"[|\[\]#\u200f\u200e]", str(item.get("description") or ""))
        and len(re.findall(r"[\w\u0600-\u06FF]", str(item.get("description") or ""))) >= 2
    ]
    # When a real INV number exists, keep labeled line items preferentially.
    if re.search(r"(?i)\bINV[-_]?\d+", str(header.get("invoice_number") or "")):
        labeled = [item for item in items if re.match(r"(?i)^item\s*\d+", str(item.get("description") or "").strip())]
        if labeled:
            items = labeled

    return {
        "header": header,
        "items": items,
        "totals": totals,
        "low_confidence": sum(1 for item in items if item.get("review")),
        "agent": "parser",
    }


def _normalize_parsed(parsed: dict[str, Any], source: str, confidence: float) -> dict[str, Any]:
    header = dict(parsed.get("header") or {})
    items = []
    for raw in parsed.get("items") or []:
        item = {
            "description": str(raw.get("description") or "").strip(),
            "sku": str(raw.get("sku") or "").strip(),
            "qty": str(raw.get("qty") or "").strip(),
            "unit_price": _money(str(raw.get("unit_price") or "")),
            "total": _money(str(raw.get("total") or "")),
            "confidence": float(raw.get("confidence") or confidence),
            "review": bool(raw.get("review")),
        }
        if not item["description"] and not item["total"]:
            continue
        if totals_mismatch(item["qty"], item["unit_price"], item["total"]):
            item["review"] = True
        if item["confidence"] < CONFIDENCE_THRESHOLD:
            item["review"] = True
        items.append(item)
    totals = dict(parsed.get("totals") or {})
    for key in ("subtotal", "tax_amount", "grand_total"):
        if totals.get(key):
            totals[key] = _money(str(totals[key]))
        elif header.get(key):
            totals[key] = _money(str(header[key]))
    if not totals.get("currency"):
        totals["currency"] = header.get("currency", "")
    return {
        "header": header,
        "items": items,
        "totals": totals,
        "low_confidence": sum(1 for item in items if item.get("review")),
        "agent": source,
    }


def decide(evidence: dict[str, Any]) -> dict[str, Any]:
    """Agent decision: pick the strongest coherent structured result."""
    candidates: list[dict[str, Any]] = []

    llm = decide_with_llm(evidence)
    if llm and llm.get("items"):
        candidates.append(llm)

    parser = decide_with_parser(evidence)
    if parser:
        candidates.append(parser)
        # If the text parser already locked a coherent invoice basket, trust it.
        items = parser.get("items") or []
        totals = parser.get("totals") or {}
        header = parser.get("header") or {}
        items_sum = sum(_to_float(item.get("total")) or 0 for item in items)
        sub = _to_float(totals.get("subtotal") or header.get("subtotal") or "")
        inv = str(header.get("invoice_number") or "")
        if items and sub and abs(items_sum - sub) <= max(1.0, sub * 0.15):
            return parser
        if items and re.search(r"(?i)^INV[-_]?\d+", inv) and len(items) >= 2:
            return parser

    for table in evidence.get("tables") or []:
        try:
            from invoice import parse_invoice_table

            parsed = _normalize_parsed(
                parse_invoice_table(table, None, evidence.get("pages") or []),
                source="table-soft",
                confidence=75.0,
            )
            if parsed.get("items"):
                candidates.append(parsed)
        except Exception:
            continue

    def _clean_items(payload: dict[str, Any]) -> dict[str, Any]:
        cleaned = []
        for item in payload.get("items") or []:
            desc = str(item.get("description") or "")
            if re.search(r"[|\[\]#\u200f\u200e]{1,}|^\W+$", desc):
                continue
            if re.fullmatch(r"(?i)item\s*\d+", desc.strip()) or len(re.findall(r"[\w\u0600-\u06FF]", desc)) >= 3:
                cleaned.append(item)
            elif item.get("total") and item.get("qty"):
                cleaned.append(item)
        out = dict(payload)
        out["items"] = cleaned
        out["low_confidence"] = sum(1 for item in cleaned if item.get("review"))
        return out

    candidates = [_clean_items(c) for c in candidates]
    candidates = [c for c in candidates if c.get("items") or c.get("header") or c.get("totals")]

    def _rank(payload: dict[str, Any]) -> tuple:
        items = payload.get("items") or []
        header = payload.get("header") or {}
        totals = payload.get("totals") or {}
        items_sum = sum(_to_float(item.get("total")) or 0 for item in items)
        sub = _to_float(totals.get("subtotal") or header.get("subtotal") or "")
        grand = _to_float(totals.get("grand_total") or header.get("grand_total") or "")
        coherence = 0
        if sub and items_sum and abs(items_sum - sub) <= max(1.0, sub * 0.15):
            coherence += 200
        if grand and items_sum and abs(items_sum - grand) <= max(1.0, grand * 0.25):
            coherence += 80
        if header.get("invoice_number") and re.search(r"(?i)inv|فاتور|\d{3,}", str(header.get("invoice_number"))):
            coherence += 40
        filled_header = sum(1 for key in ("invoice_number", "invoice_date", "client_name", "supplier") if header.get(key))
        filled_totals = sum(1 for key in ("subtotal", "tax_amount", "grand_total") if totals.get(key))
        good_items = sum(1 for item in items if not item.get("review"))
        # Prefer fewer noisy rows over huge incoherent dumps.
        return (coherence, good_items + filled_header + filled_totals, len(items) if coherence else min(len(items), 12))

    if not candidates:
        return {"header": {}, "items": [], "totals": {}, "low_confidence": 0, "agent": "empty"}

    best = max(candidates, key=_rank)
    for other in candidates:
        if other is best:
            continue
        for key, value in (other.get("header") or {}).items():
            if value and not (best.get("header") or {}).get(key):
                best.setdefault("header", {})[key] = value
        for key, value in (other.get("totals") or {}).items():
            if value and not (best.get("totals") or {}).get(key):
                best.setdefault("totals", {})[key] = value
    return best


def analyze(source: Path, master: dict[str, list[str]], output_dir: Path) -> FileResult:
    destination = output_file(source, output_dir)
    try:
        evidence = gather_evidence(source)
        if not (evidence.get("text") or "").strip() and not evidence.get("tables"):
            return FileResult(str(source), status="failed", error="لم يتم قراءة نص الفاتورة.")
        parsed = decide(evidence)
        warnings = list(evidence.get("warnings") or [])
        warnings.append(f"agent:{parsed.get('agent') or 'unknown'}")

        for item in parsed.get("items") or []:
            text, changed = validate_value(item.get("description", ""), "description", master)
            item["description"] = text
            if changed:
                item["review"] = True

        if not parsed.get("items"):
            from export import write_generic_tables

            header = parsed.get("header") or {}
            context_rows = [
                {"values": [line], "confidences": [60.0], "review": True}
                for line in (evidence.get("text") or "").splitlines()
                if line.strip()
            ]
            records, low, review_items, template, destination = write_generic_tables(
                destination,
                source,
                [
                    {
                        "name": "Header",
                        "headers": ["Field", "Value"],
                        "rows": [
                            {
                                "values": [label, header.get(key, "")],
                                "confidences": [70.0],
                                "review": not bool(header.get(key)),
                            }
                            for key, label in [
                                ("supplier", "Supplier"),
                                ("client_name", "Client Name"),
                                ("invoice_number", "Invoice Number"),
                                ("invoice_date", "Invoice Date"),
                                ("due_date", "Due Date"),
                                ("tax_number", "Tax Number"),
                                ("currency", "Currency"),
                            ]
                        ],
                    },
                    {"name": "OCR Context", "headers": ["OCR text"], "rows": context_rows},
                ],
            )
            warnings.append("تعذر تثبيت البنود بثقة؛ راجع OCR Context.")
            return FileResult(
                str(source), str(destination), records=records, low_confidence=low,
                warnings=warnings, template=template, review_items=review_items,
            )

        records, low, review_items, template, destination = write_invoice(destination, source, parsed)
        if low:
            warnings.append("توجد عناصر صفراء تتطلب مراجعة.")
        return FileResult(
            str(source), str(destination), records=records, low_confidence=low,
            warnings=warnings or None, template=template, review_items=review_items,
        )
    except PermissionError as error:
        return FileResult(
            str(source),
            status="failed",
            error="تعذّر حفظ Excel لأن الملف مفتوح. أغلق ملف النتائج في Excel ثم أعد المحاولة.",
        )
    except Exception as error:
        return FileResult(
            str(source),
            status="failed",
            error=f"Invoice Agent: {type(error).__name__}: {error}",
        )
