"""One job, in its own process.

Paddle's native libraries are not safe to load inside an async web server: the
import alone brought the API down with a silent segfault, taking every other
request with it. The desktop product has always run this work as a child
process for the same reason, and the web app follows it.

Isolation buys three things:

* a crash in a native library fails one job instead of the whole service;
* the model's memory is returned to the operating system when the page is done;
* the API process stays responsive to status polls throughout.

Invoked as:  python -m app.services.runner <request.json>
It reads a request, writes a result beside it, and exits.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any


# Progress travels back to the parent on stdout, one JSON object per line behind
# a marker. Paddle and its dependencies print freely to the same stream, so the
# marker is what separates our messages from their noise.
PROGRESS = "@@EC-PROGRESS "


def report(stage: str, state: str, **fields: Any) -> None:
    """Tell the parent where we are. Flushed, or it would arrive at exit."""
    try:
        sys.stdout.write(PROGRESS + json.dumps({"stage": stage, "state": state, **fields}) + "\n")
        sys.stdout.flush()
    except Exception:
        # Progress is a courtesy to the interface. Never let it fail the job.
        pass


def _worker_on_path(worker_root: str) -> None:
    if worker_root not in sys.path:
        sys.path.insert(0, worker_root)


def _dump_page(source: Path, number: int, payload: dict[str, Any], warnings: list[str]) -> None:
    """Keep the reader's raw answer for a page, when asked to.

    Set ``VERTEX_DUMP_PAGES`` to a directory and every page the model returns is
    written there untouched. A customer's invoice that comes out wrong can then
    be replayed through ``paddle_vl.to_payload`` in a second, on any machine,
    without the model, the image or the wait — which is the difference between
    diagnosing a new invoice shape and guessing at it.

    Off unless the variable is set: these files hold the customer's document.
    """
    import os

    directory = (os.environ.get("VERTEX_DUMP_PAGES") or "").strip()
    if not directory:
        return
    try:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^\w.-]+", "_", source.stem)[:60] or "page"
        path = target / f"{stem}.p{number}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as error:
        # A diagnostic that fails must never fail the job it was diagnosing.
        warnings.append(f"page-dump-skipped:{type(error).__name__}")


def run_clean(request: dict[str, Any]) -> dict[str, Any]:
    """Tidy a spreadsheet the customer already has.

    No model is called and nothing leaves this machine: header rows above the
    real one are dropped, every cell is normalised, duplicate rows are removed,
    and anything corrected against the local lists is highlighted for a human.
    It is the desktop product's own cleaner, reused rather than reimplemented.
    """
    _worker_on_path(request["worker_root"])

    from clean import load_master_data
    from tabular import clean_tabular

    source = Path(request["source"])
    result_dir = Path(request["result_dir"])
    result_dir.mkdir(parents=True, exist_ok=True)

    report("clean", "running", page=1, pages=1)
    started = time.perf_counter()
    outcome = clean_tabular(source, load_master_data(request.get("master_root")), result_dir)
    elapsed = int((time.perf_counter() - started) * 1000)
    report("clean", "done", ms=elapsed, page=1, pages=1, page_complete=True)

    if outcome.status == "failed" or not outcome.output:
        return {
            "ok": False,
            "code": "clean_failed",
            "error": outcome.error or "the spreadsheet could not be cleaned",
        }

    return {
        "ok": True,
        "result": str(outcome.output),
        "records": outcome.records,
        "low_confidence": outcome.low_confidence,
        # The cleaner marks whole rows rather than single cells, so each entry
        # points at the row it corrected.
        "flagged": [
            {
                "cell": f"{item.get('sheet') or ''}!{item.get('row') or ''}",
                "value": item.get("value"),
                "reason": str(item.get("suggestion") or "corrected against local lists"),
                "gate": "cleaning",
            }
            for item in (outcome.review_items or [])
        ],
        "warnings": list(outcome.warnings or [])[:50],
        "timings": {"clean": elapsed},
        "provider": "local",
        "model": "deterministic-cleaner",
        "pages": 1,
    }


def run(request: dict[str, Any]) -> dict[str, Any]:
    _worker_on_path(request["worker_root"])

    import ai_extract
    import excel_builder
    import geometry
    import paddle_vl
    import perceive
    from ocr import image_pages
    from PIL import Image

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app.core.config import get_settings
    from app.services.ai_provider import build_provider

    settings = get_settings()
    provider = build_provider(settings)
    source = Path(request["source"])

    timings: dict[str, int] = {}
    warnings: list[str] = []
    documents: list[dict[str, Any]] = []
    provider_name = model_name = ""

    import tempfile

    total_pages = int(request.get("pages") or 0)

    for page_number, image in enumerate(image_pages(source), start=1):
        # Independent evidence first: it is what the model is checked against,
        # and it is the fallback text for a page the model returns nothing for.
        report("evidence_ocr", "running", page=page_number, pages=total_pages)
        started = time.perf_counter()
        try:
            words, notes, _prepared = perceive.read_page(image)
            warnings.extend(notes)
        except Exception as error:
            words = []
            warnings.append(f"evidence-ocr-skipped:{type(error).__name__}")
        timings["evidence_ocr"] = timings.get("evidence_ocr", 0) + int(
            (time.perf_counter() - started) * 1000
        )
        report("evidence_ocr", "done", ms=timings["evidence_ocr"],
               page=page_number, pages=total_pages)

        report("ai_vision", "running", page=page_number, pages=total_pages)
        with tempfile.TemporaryDirectory(prefix="ec-page-") as workspace:
            page_path = Path(workspace) / f"page{page_number}.png"
            Image.fromarray(image).save(page_path, format="PNG")
            outcome = provider.read(page_path)
        timings["ai_vision"] = timings.get("ai_vision", 0) + outcome.inference_ms
        timings["ai_queue"] = timings.get("ai_queue", 0) + outcome.queue_ms
        provider_name, model_name = outcome.provider, outcome.model
        if outcome.fallback_reason:
            warnings.append(f"gpu-service-skipped: {outcome.fallback_reason}")
        report("ai_vision", "done", ms=timings["ai_vision"],
               page=page_number, pages=total_pages)

        _dump_page(source, page_number, outcome.pages[0].as_payload(), warnings)

        # Verification always happens here, never on the machine that did the
        # reading. A model grading its own output would prove nothing.
        report("verification", "running", page=page_number, pages=total_pages)
        started = time.perf_counter()
        payload = paddle_vl.to_payload(outcome.pages[0].as_payload())

        # Where each label and value actually sit on the page. The vision model
        # returns text in reading order and the hosted providers return no
        # geometry at all, so a field printed beside another — "Invoice No: 118
        # Date: 04/03/2026" — could be paired with its neighbour's value. The
        # evidence reader has a box for every word, so the pairing is taken from
        # the page itself and believed over the text-order guess.
        try:
            positioned = geometry.read_fields(words)
            for field, value in positioned["header"].items():
                payload.setdefault("header", {})[field] = value
            for field, value in positioned["extra"].items():
                payload.setdefault("header", {}).setdefault(field, value)
            for field, amount in positioned["totals"].items():
                payload.setdefault("totals", {})[field] = amount
        except Exception as error:
            warnings.append(f"geometry-skipped:{type(error).__name__}")
        document, blocking, advisory = ai_extract.validate(
            payload, ai_extract.page_numbers(words)
        )
        document["page"] = page_number
        documents.append(document)
        warnings.extend(blocking[:5])
        # Which columns the reader took for the quantity, the price and the
        # total, and how well the arithmetic agreed. Kept because the failure
        # this diagnoses — a value under the wrong heading — looks perfectly
        # fine in the workbook and can only be caught by knowing what was read.
        warnings.extend(advisory[:8])
        timings["verification"] = timings.get("verification", 0) + int(
            (time.perf_counter() - started) * 1000
        )
        report("verification", "done", ms=timings["verification"],
               page=page_number, pages=total_pages, page_complete=True)

        if page_number >= ai_extract.MAX_PAGES:
            warnings.append(f"stopped after {ai_extract.MAX_PAGES} pages")
            break

    if not documents:
        raise RuntimeError("no page could be read from this file")

    # The pages of one order become one document before anything is written. A
    # four-page manifest read page by page produced four sheets, each repeating
    # the same shipper and consignee and each holding a quarter of the goods —
    # so no total covered the shipment the customer was actually checking.
    # Documents that do not belong together are left apart; see
    # ``ai_extract.continues`` for what counts as belonging.
    read_pages = len(documents)
    documents = ai_extract.merge_pages(documents)
    if len(documents) < read_pages:
        warnings.append(f"merged {read_pages} pages into {len(documents)}")

    report("excel", "running", page=total_pages, pages=total_pages)
    started = time.perf_counter()
    destination = Path(request["result_dir"]) / f"{source.stem}.xlsx"
    records, low, review_items, _template, written = excel_builder.write_ai_workbook(
        destination, source, documents
    )
    timings["excel"] = int((time.perf_counter() - started) * 1000)
    report("excel", "done", ms=timings["excel"], page=total_pages, pages=total_pages)

    flagged = [
        {
            "cell": str(item.get("cell") or item.get("address") or "—"),
            "value": item.get("value"),
            "reason": str(item.get("reason") or item.get("note") or "needs review"),
            "gate": str(item.get("gate") or "evidence"),
        }
        for item in (review_items or [])
    ]
    return {
        "ok": True,
        "result": str(written),
        "records": records,
        "low_confidence": low,
        "flagged": flagged,
        "warnings": warnings[:50],
        "timings": timings,
        "provider": provider_name,
        "model": model_name,
        "pages": len(documents),
    }


def main() -> int:
    request_path = Path(sys.argv[1])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    output = Path(request["result_json"])
    try:
        result = run_clean(request) if request.get("kind") == "clean" else run(request)
    except Exception as error:
        # A code as well as the text: the browser says why in the reader's own
        # language, and the text stays in the record for whoever debugs it.
        text = str(error)
        if isinstance(error, (FileNotFoundError, OSError)) and not text:
            text = type(error).__name__
        code = (
            "no_page_read" if "no page could be read" in text
            else "source_missing" if isinstance(error, FileNotFoundError)
            else "reader_failed"
        )
        result = {"ok": False, "code": code, "error": f"{type(error).__name__}: {error}"}
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
