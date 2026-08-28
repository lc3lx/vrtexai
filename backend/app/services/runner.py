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


def run(request: dict[str, Any]) -> dict[str, Any]:
    _worker_on_path(request["worker_root"])

    import ai_extract
    import excel_builder
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

        # Verification always happens here, never on the machine that did the
        # reading. A model grading its own output would prove nothing.
        report("verification", "running", page=page_number, pages=total_pages)
        started = time.perf_counter()
        payload = paddle_vl.to_payload(outcome.pages[0].as_payload())
        document, blocking, _advisory = ai_extract.validate(
            payload, ai_extract.page_numbers(words)
        )
        document["page"] = page_number
        documents.append(document)
        warnings.extend(blocking[:5])
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

    report("excel", "running", page=len(documents), pages=total_pages)
    started = time.perf_counter()
    destination = Path(request["result_dir"]) / f"{source.stem}.xlsx"
    records, low, review_items, _template, written = excel_builder.write_ai_workbook(
        destination, source, documents
    )
    timings["excel"] = int((time.perf_counter() - started) * 1000)
    report("excel", "done", ms=timings["excel"], page=len(documents), pages=total_pages)

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
        result = run(request)
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
