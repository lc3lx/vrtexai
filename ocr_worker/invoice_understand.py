"""Local document understanding: perceive -> reconstruct -> verify -> export.

The ordering matters and is the whole design. Values come from geometry and are
cross-checked by arithmetic; a language model is allowed to *name* what was
found and never to state what it is. The previous version handed a flat OCR
blob to a text model and asked it to produce the table, which is why rows went
missing, duplicated, and stopped adding up.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import layout
import perceive
import verify as verify_module
from clean import validate_value
from common import FileResult
from export import output_file, write_single_sheet

_LLM = None
_LLM_ERROR: str | None = None


# --------------------------------------------------------------------------
# Local text model — naming only
# --------------------------------------------------------------------------
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
        "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
        "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
        "qwen2.5-1.5b-instruct-q4_k_m.gguf",
    )
    for name in preferred:
        path = root / name
        if path.is_file():
            return path
    matches = [path for path in sorted(root.glob("*.gguf")) if "mmproj" not in path.name.casefold()
               and "vl" not in path.name.casefold()]
    return matches[0] if matches else None


def llm_available() -> tuple[bool, str]:
    gguf = find_gguf()
    if gguf is None:
        return False, "لا يوجد نموذج GGUF محلي في llm_models."
    try:
        import llama_cpp  # noqa: F401
    except Exception as error:
        return False, f"llama-cpp غير متاح: {error}"
    return True, str(gguf)


def _get_llm():
    global _LLM, _LLM_ERROR
    if _LLM_ERROR:
        raise RuntimeError(_LLM_ERROR)
    if _LLM is not None:
        return _LLM
    gguf = find_gguf()
    if gguf is None:
        _LLM_ERROR = "لا يوجد نموذج فهم محلي (GGUF)."
        raise RuntimeError(_LLM_ERROR)
    try:
        from common import quiet_native_output
        from llama_cpp import Llama
    except Exception as error:
        _LLM_ERROR = f"llama-cpp غير متاح: {error}"
        raise RuntimeError(_LLM_ERROR) from error
    threads = max(1, min(6, os.cpu_count() or 2))
    # llama.cpp logs from C; the desktop app parses this process's stdout.
    with quiet_native_output():
        _LLM = Llama(
            model_path=str(gguf),
            n_ctx=4096,
            n_threads=threads,
            n_batch=192,
            verbose=False,
        )
    return _LLM


def _extract_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object")
    blob = text[start : end + 1]
    blob = blob.replace("“", '"').replace("”", '"').replace("﻿", "")
    blob = re.sub(r",\s*([}\]])", r"\1", blob)
    return json.loads(blob)


# The model chooses a category; this file owns the wording. Asked for free text
# a 3B model drifts language — it titled an English invoice's sections in
# Spanish — so it is given a closed set to pick from instead, and the label the
# customer reads is produced here in the document's own language.
SECTION_LABELS: dict[str, tuple[str, str]] = {
    "document_header": ("Document Header", "ترويسة المستند"),
    "parties": ("Parties", "الأطراف"),
    "details": ("Details", "التفاصيل"),
    "line_items": ("Line Items", "البنود"),
    "totals": ("Totals", "الإجماليات"),
    "payment": ("Payment", "الدفع"),
    "contact": ("Contact", "بيانات الاتصال"),
    "dates": ("Dates", "التواريخ"),
    "terms": ("Terms & Notes", "الشروط والملاحظات"),
    "other": ("", ""),
}


def name_sections(document: dict[str, Any]) -> list[str]:
    """Classify each region into a known section type and label it.

    The model may only pick a category. It is never given a field it could
    write a value into, so the worst case is a mislabelled heading.
    """
    regions = document.get("regions") or []
    # Only structured blocks get a heading. A caption floating above one stray
    # line of prose is noise, and it is where the classifier is least reliable.
    unnamed = [
        index for index, region in enumerate(regions)
        if not region.get("title") and region.get("kind") in {"table", "key_value"}
    ]
    if not unnamed:
        return []
    arabic = str(document.get("direction") or "ltr") == "rtl"
    categories = ", ".join(key for key in SECTION_LABELS if key != "other")
    system = (
        "You classify sections of a business document. "
        f"For each region choose exactly one category from: {categories}, other. "
        'Reply ONLY with JSON: {"sections":[{"region":1,"category":"line_items"}]} '
        "Choose 'other' when unsure. Never output any text from the document."
    )
    user = "Classify each region.\n\n" + layout.render_for_classification(document, unnamed)[:1800]
    try:
        from common import quiet_native_output

        llm = _get_llm()
        with quiet_native_output():
            result = llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                max_tokens=200,
            )
        parsed = _extract_json(str(result["choices"][0]["message"]["content"] or ""))
    except Exception as error:
        return [f"naming-skipped:{type(error).__name__}"]

    applied = 0
    for entry in parsed.get("sections") or []:
        try:
            index = int(entry.get("region")) - 1
        except (TypeError, ValueError):
            continue
        category = str(entry.get("category") or "").strip().casefold()
        if index not in unnamed:
            continue
        label = SECTION_LABELS.get(category)
        if not label:
            continue
        title = label[1] if arabic else label[0]
        if not title:
            continue
        regions[index]["title"] = title
        applied += 1
    return [f"naming:{applied}"] if applied else []


# --------------------------------------------------------------------------
# Master-data validation
# --------------------------------------------------------------------------
def _apply_master_data(document: dict[str, Any], master: dict[str, list[str]]) -> int:
    if not master:
        return 0
    changed = 0
    for region in document.get("regions") or []:
        columns = region.get("columns") or []
        for row in region.get("rows") or []:
            for index, cell in enumerate(row):
                header = columns[index] if index < len(columns) else ""
                if not re.search(r"(?i)desc|name|item|product|وصف|صنف|بيان|اسم", str(header)):
                    continue
                text, was_changed = validate_value(cell.get("text") or "", "description", master)
                if was_changed:
                    cell["note"] = "طوبق مع قوائم المطابقة المحلية"
                    cell["review"] = True
                    changed += 1
                cell["text"] = text
    return changed


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def _vision_wanted(document: dict[str, Any]) -> bool:
    setting = vision_mode()
    if setting == "off":
        return False
    if setting == "always":
        return True
    # Only for pages the geometric path genuinely struggled with. A re-read
    # costs about 20 seconds per cell, and on a page OCR read confidently it
    # would add minutes to change nothing.
    return layout.mean_confidence(document) < 70.0


def vision_mode() -> str:
    try:
        import vision

        return vision.mode()
    except Exception:
        return "off"


def analyze(source: Path, master: dict[str, list[str]], output_dir: Path) -> FileResult:
    destination = output_file(source, output_dir)
    warnings: list[str] = []
    try:
        from ocr import image_pages

        documents: list[dict[str, Any]] = []
        # Each page is finished before the next is read, so only one page image
        # is ever in memory. Holding all of them would cost a few hundred MB on
        # a long PDF, on machines specified at 8GB.
        for page_number, image in enumerate(image_pages(source), start=1):
            # `prepared` is the image the word coordinates belong to; the raw
            # page has since been deskewed and rescaled, so measuring it would
            # put rules and crops in the wrong place.
            words, notes, prepared = perceive.read_page(image)
            warnings.extend(notes)
            document = layout.build_document(words, page=page_number, image=prepared)
            warnings.extend(document.get("warnings") or [])

            warnings.extend(verify_module.verify(document))
            if _vision_wanted(document):
                try:
                    import vision

                    warnings.extend(vision.refine_document(prepared, document))
                    # Values may have changed; re-run the cross-checks.
                    verify_module.verify(document)
                except Exception as error:
                    warnings.append(f"vision-skip:{type(error).__name__}")
            matched = _apply_master_data(document, master)
            if matched:
                warnings.append(f"master-data:{matched}")
            warnings.extend(name_sections(document))

            documents.append(document)
            del prepared, image
            if page_number >= 40:
                warnings.append("توقف بعد 40 صفحة.")
                break

        if not documents or all(not doc.get("regions") for doc in documents):
            return FileResult(str(source), status="failed", error="لم يتم قراءة أي نص من الملف.")

        cells = sum(layout.count_cells(document) for document in documents)
        confidence = round(
            sum(layout.mean_confidence(document) for document in documents) / len(documents), 1
        )
        warnings.append(f"cells:{cells}")
        warnings.append(f"confidence:{confidence}")

        records, low, review_items, template, destination = write_single_sheet(
            destination, source, documents
        )
        if low:
            warnings.append("توجد خلايا صفراء تتطلب مراجعة.")
        return FileResult(
            str(source),
            str(destination),
            records=records,
            low_confidence=low,
            warnings=warnings,
            template=template,
            review_items=review_items,
        )
    except PermissionError:
        return FileResult(
            str(source),
            status="failed",
            error="تعذّر حفظ Excel لأن الملف مفتوح. أغلق ملف النتائج في Excel ثم أعد المحاولة.",
        )
    except Exception as error:
        return FileResult(
            str(source),
            status="failed",
            error=f"Local Understand: {type(error).__name__}: {error}",
        )
