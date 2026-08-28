"""CPU invoice understanding via PaddleOCR PP-Structure (offline models).

Designed for low-RAM laptops (~8GB): one page at a time, no GPU, no invented totals.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from clean import validate_value
from common import CONFIDENCE_THRESHOLD, FileResult
from export import output_file, write_invoice
from invoice import (
    extract_invoice_fields,
    extract_item_values,
    invoice_table_is_reliable,
    parse_invoice_table,
    totals_mismatch,
)
from ocr import image_pages

_ENGINE = None
_ENGINE_ERROR: str | None = None


def models_root() -> Path:
    env = os.environ.get("VERTEX_PADDLE_MODELS")
    if env:
        return Path(env)
    # Prefer bundled models next to the worker, then next to runtime python.
    here = Path(__file__).resolve().parent
    candidates = [
        here / "paddle_models",
        here.parent / "runtime" / "paddle_models",
        Path(os.environ.get("PYTHONHOME") or "") / "paddle_models",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[0]


def _resolve_model_dir(*parts: str) -> str | None:
    root = models_root()
    path = root.joinpath(*parts)
    if path.is_dir() and any(path.iterdir()):
        return str(path)
    return None


def _build_engine():
    global _ENGINE, _ENGINE_ERROR
    if _ENGINE is not None:
        return _ENGINE
    if _ENGINE_ERROR:
        raise RuntimeError(_ENGINE_ERROR)
    try:
        # Keep paddle CPU-only and quiet on customer machines.
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        import paddleocr.paddleocr as paddleocr_mod
        from paddleocr import PPStructure  # type: ignore

        # PPStructure always calls maybe_download for formula models even when
        # formula=False. Never reach the network on customer machines.
        def _offline_maybe_download(model_dir, url):  # type: ignore[no-untyped-def]
            if model_dir and Path(model_dir).is_dir() and any(Path(model_dir).iterdir()):
                return
            return

        paddleocr_mod.maybe_download = _offline_maybe_download
    except Exception as error:
        _ENGINE_ERROR = (
            "محرك فهم الفاتورة (PaddleOCR) غير متاح في هذه النسخة. "
            f"{type(error).__name__}: {error}"
        )
        raise RuntimeError(_ENGINE_ERROR) from error

    kwargs: dict[str, Any] = {
        "use_gpu": False,
        "show_log": False,
        # Layout/table checkpoints only ship for en/ch. Arabic invoices still
        # use Multilingual detection + arabic recognition via explicit dirs.
        "lang": "en",
        "layout": True,
        "table": True,
        "ocr": True,
        "recovery": False,
        "enable_mkldnn": False,
        "formula": False,
    }
    det = _resolve_model_dir("det", "Multilingual_PP-OCRv3_det_infer") or _resolve_model_dir("det")
    rec = _resolve_model_dir("rec", "arabic_PP-OCRv3_rec_infer") or _resolve_model_dir("rec")
    table = _resolve_model_dir("table", "en_ppstructure_mobile_v2.0_SLANet_infer") or _resolve_model_dir("table")
    layout = _resolve_model_dir("layout", "picodet_lcnet_x1_0_fgd_layout_infer") or _resolve_model_dir("layout")
    if det:
        kwargs["det_model_dir"] = det
    if rec:
        kwargs["rec_model_dir"] = rec
        # Point recognition dictionary at the arabic alphabet shipped with paddleocr.
        try:
            import paddleocr as _paddleocr_pkg

            pkg = Path(_paddleocr_pkg.__file__).resolve().parent
            arabic_dict = pkg / "ppocr" / "utils" / "dict" / "arabic_dict.txt"
            if arabic_dict.is_file():
                kwargs["rec_char_dict_path"] = str(arabic_dict)
        except Exception:
            pass
    if table:
        kwargs["table_model_dir"] = table
    if layout:
        kwargs["layout_model_dir"] = layout
        # Some layout archives ship model.pd* instead of inference.pd*.
        layout_path = Path(layout)
        for src_name, dst_name in (
            ("model.pdmodel", "inference.pdmodel"),
            ("model.pdiparams", "inference.pdiparams"),
            ("model.pdiparams.info", "inference.pdiparams.info"),
        ):
            src, dst = layout_path / src_name, layout_path / dst_name
            if src.is_file() and not dst.is_file():
                try:
                    dst.write_bytes(src.read_bytes())
                except OSError:
                    pass
    # Never download at customer runtime.
    os.environ.setdefault("HUB_HOME", str(models_root()))
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    try:
        _ENGINE = PPStructure(**kwargs)
    except TypeError:
        # Older paddleocr builds accept a smaller kwargs set.
        slim = {key: kwargs[key] for key in ("use_gpu", "show_log", "lang") if key in kwargs}
        for key in (
            "det_model_dir",
            "rec_model_dir",
            "table_model_dir",
            "layout_model_dir",
            "rec_char_dict_path",
        ):
            if key in kwargs:
                slim[key] = kwargs[key]
        _ENGINE = PPStructure(**slim)
    return _ENGINE


def _html_table_to_rows(html: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html or "", flags=re.I | re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.I | re.S)
        cleaned = [re.sub(r"<[^>]+>", " ", cell) for cell in cells]
        cleaned = [re.sub(r"\s+", " ", cell).strip() for cell in cleaned]
        if any(cleaned):
            rows.append(cleaned)
    return rows


def _structure_to_tables_and_text(result: list[dict[str, Any]]) -> tuple[list[list[list[str]]], list[str]]:
    tables: list[list[list[str]]] = []
    lines: list[str] = []
    for block in result or []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "").casefold()
        res = block.get("res")
        if kind == "table":
            html = ""
            if isinstance(res, dict):
                html = str(res.get("html") or "")
                for cell in res.get("cell_bbox") or []:
                    pass
            elif isinstance(res, list):
                # Some builds return OCR lines for the table crop.
                for item in res:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        text = item[1][0] if isinstance(item[1], (list, tuple)) else item[1]
                        if text:
                            lines.append(str(text))
            rows = _html_table_to_rows(html)
            if rows:
                tables.append(rows)
            continue
        if isinstance(res, list):
            for item in res:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    payload = item[1]
                    text = payload[0] if isinstance(payload, (list, tuple)) else payload
                    if text:
                        lines.append(str(text).strip())
                elif isinstance(item, dict) and item.get("text"):
                    lines.append(str(item["text"]).strip())
        elif isinstance(res, dict) and res.get("text"):
            lines.append(str(res["text"]).strip())
    return tables, [line for line in lines if line]


def analyze_image(image) -> tuple[list[list[str]], list[str], list[str]]:
    """Run PP-Structure on one RGB ndarray/path. Returns tables, paragraphs, warnings."""
    import numpy as np

    engine = _build_engine()
    warnings: list[str] = []
    array = np.asarray(image)
    result = engine(array)
    tables, lines = _structure_to_tables_and_text(result if isinstance(result, list) else [])
    if not tables and lines:
        # Fall back to line clustering when layout found text but no HTML table.
        from invoice import lines_to_table

        tables = [lines_to_table(["\n".join(lines)])]
        warnings.append("لم يُكتشف جدول HTML؛ تم تجميع النص سطراً بسطر للمراجعة.")
    return tables, lines, warnings


def _merge_parsed(parts: list[dict[str, Any]]) -> dict[str, Any]:
    header: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    totals: dict[str, str] = {}
    low = 0
    for part in parts:
        for key, value in (part.get("header") or {}).items():
            if value and not header.get(key):
                header[key] = value
        items.extend(part.get("items") or [])
        for key, value in (part.get("totals") or {}).items():
            if value and not totals.get(key):
                totals[key] = value
        low += int(part.get("low_confidence") or 0)
    if items and not totals.get("grand_total"):
        amounts = []
        ok = True
        for item in items:
            try:
                amounts.append(float(str(item.get("total") or "").replace(",", "")))
            except ValueError:
                ok = False
                break
        if ok and amounts:
            totals["grand_total"] = f"{sum(amounts):.2f}"
            totals.setdefault("subtotal", totals["grand_total"])
    return {"header": header, "items": items, "totals": totals, "low_confidence": low}


def analyze(source: Path, master: dict[str, list[str]], output_dir: Path) -> FileResult:
    """Document path: local AI first, geometric understanding as the safety net.

    The AI path reads the whole page with a local vision model through Ollama
    and is checked against the geometric reading before anything is written.
    When Ollama is absent, disabled, or fails three attempts, the geometric
    pipeline runs instead — it is complete on its own and needs no model, so a
    customer who skipped the download still gets an Excel file.
    """
    ai_error = ""
    try:
        import ai_extract

        ready, detail = ai_extract.available()
        if ready:
            return ai_extract.analyze(source, master, output_dir)
        ai_error = detail
    except Exception as failure:
        ai_error = f"{type(failure).__name__}: {failure}"

    try:
        from invoice_understand import analyze as understand_analyze

        result = understand_analyze(source, master, output_dir)
        if result.status != "failed":
            if ai_error:
                result.warnings = list(result.warnings or []) + [f"ai-fallback:{ai_error[:160]}"]
            return result
        error = result.error or ""
    except Exception as failure:
        error = f"{type(failure).__name__}: {failure}"

    from invoice_agent import analyze as agent_analyze

    fallback = agent_analyze(source, master, output_dir)
    if fallback.status != "failed" and error:
        fallback.warnings = list(fallback.warnings or []) + [f"understand-fallback:{error[:120]}"]
    return fallback


def paddle_available() -> tuple[bool, str]:
    try:
        _build_engine()
        return True, str(models_root())
    except Exception as error:
        return False, str(error)
