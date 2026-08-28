"""Excel Clear vision service — PaddleOCR-VL behind one HTTP endpoint.

This is the *only* piece that moves to a GPU host. It reads a page and returns
its structure. It knows nothing about users, customers, quotas, or Excel, and it
stores nothing: every request is independent and its temporary file is deleted
before the response is sent.

The response shape is not a new invention. It is exactly what the desktop
``vl_worker`` already writes, so ``paddle_vl.to_payload`` on the backend parses
either source with the same code:

    {"success": true,
     "pages": [{"result": {...}, "markdown": "..."}],
     "model": "PaddleOCR-VL-0.9B",
     "inference_ms": 18400}

Verification stays on the backend. A service that graded its own output would
destroy the guarantee the product is sold on.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vision")

MAX_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 30 * 1024 * 1024))
DEVICE = os.environ.get("VL_DEVICE", "gpu")
# One model, one lock. PaddleOCR-VL is not safe to call concurrently from
# several threads, and a GPU serves one page at a time anyway; queueing here is
# honest, whereas parallel calls would corrupt each other.
_LOCK = threading.Lock()
_PIPELINE: Any = None
_LOAD_ERROR: str = ""


def _prepare_environment() -> None:
    """Trust the OS certificate store and skip PaddleX's flaky host probe.

    Both were learned the hard way on customer machines: the model host check
    reports "no source available" on networks that inspect HTTPS, and the
    download libraries trust only their own bundled CA list.
    """
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception as error:  # pragma: no cover - environment dependent
        logger.warning("OS trust store unavailable: %s", error)


def _ensure_dynamic_mode() -> None:
    """Put *this thread* into Paddle's dynamic graph mode.

    Paddle tracks graph mode per thread. A script that runs everything on the
    main thread — like the desktop worker — gets dynamic mode for free, but a
    web server does not: the model loads on a start-up thread and each request
    is served from a worker thread, and those default to static mode, where the
    model's own code fails on ``int(Tensor)``.

    Calling this in every thread that touches the model is what makes the
    hosted service behave identically to the desktop one.
    """
    try:
        import paddle

        if not paddle.in_dynamic_mode():
            paddle.disable_static()
    except Exception as error:  # pragma: no cover - paddle absent in unit tests
        logger.warning("could not set dynamic graph mode: %s", error)


def _load_model() -> None:
    """Load PaddleOCR-VL once, at start-up.

    Loading costs minutes on a CPU and tens of seconds on a GPU. Doing it per
    request would dwarf the inference it is meant to serve.
    """
    global _PIPELINE, _LOAD_ERROR
    if _PIPELINE is not None or _LOAD_ERROR:
        return
    _prepare_environment()
    _ensure_dynamic_mode()
    started = time.perf_counter()
    logger.info("MODEL_LOADING device=%s", DEVICE)
    try:
        from paddleocr import PaddleOCRVL

        _PIPELINE = PaddleOCRVL(device=DEVICE)
    except Exception as error:
        _LOAD_ERROR = f"{type(error).__name__}: {error}"
        logger.exception("MODEL_FAILED")
        return
    logger.info("MODEL_READY ms=%d", int((time.perf_counter() - started) * 1000))


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_load_model, daemon=True).start()
    yield
    global _PIPELINE
    _PIPELINE = None


app = FastAPI(title="Excel Clear Vision", version="1.0.0", lifespan=lifespan)


def _plain(value: Any) -> Any:
    """Make the pipeline result JSON-serialisable.

    Results carry numpy arrays and scalars. The page image among them is
    megabytes of pixels nobody downstream reads, so arrays of two or more
    dimensions are dropped rather than serialised.
    """
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if hasattr(value, "tolist"):
        try:
            if getattr(value, "ndim", 0) >= 2:
                return None
            return _plain(value.tolist())
        except Exception:
            return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    return str(value)


def _page_payload(result: Any) -> dict[str, Any]:
    """One page in the shape the desktop worker already produces."""
    body: Any = {}
    try:
        raw = getattr(result, "json", None)
    except Exception:
        raw = None
    if isinstance(raw, dict):
        body = raw.get("res", raw)
    elif hasattr(result, "keys"):
        try:
            body = {key: result[key] for key in result.keys()}
        except Exception:
            body = {}
    markdown = ""
    try:
        source = getattr(result, "markdown", None)
    except Exception:
        source = None
    if isinstance(source, dict):
        markdown = str(source.get("markdown_texts") or "")
    payload = _plain(body)
    return {"result": payload if isinstance(payload, dict) else {}, "markdown": markdown}


@app.get("/health")
def health() -> dict[str, Any]:
    if _LOAD_ERROR:
        return {"ready": False, "detail": _LOAD_ERROR, "model": "PaddleOCR-VL-0.9B"}
    if _PIPELINE is None:
        return {"ready": False, "detail": "model loading", "model": "PaddleOCR-VL-0.9B"}
    return {"ready": True, "detail": f"ready on {DEVICE}", "model": "PaddleOCR-VL-0.9B"}


@app.post("/process")
async def process(file: UploadFile = File(...)) -> JSONResponse:
    queued = time.perf_counter()
    if _LOAD_ERROR:
        raise HTTPException(status_code=503, detail=f"model unavailable: {_LOAD_ERROR}")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"file exceeds {MAX_BYTES} bytes")

    workspace = tempfile.mkdtemp(prefix="vision-")
    try:
        # The client's filename never touches the filesystem: it is attacker
        # controlled and only its extension is of any use.
        suffix = Path(file.filename or "page.png").suffix[:10] or ".png"
        page_path = Path(workspace) / f"page{suffix}"
        page_path.write_bytes(data)

        with _LOCK:
            if _PIPELINE is None:
                _load_model()
            if _PIPELINE is None:
                raise HTTPException(status_code=503, detail=f"model unavailable: {_LOAD_ERROR}")
            # This request is served from a pooled thread, which does not
            # inherit the loader thread's graph mode.
            _ensure_dynamic_mode()
            queue_ms = int((time.perf_counter() - queued) * 1000)
            started = time.perf_counter()
            logger.info("INFERENCE_START file=%s bytes=%d", page_path.name, len(data))
            try:
                pages = [_page_payload(result) for result in _PIPELINE.predict(str(page_path))]
            except Exception as error:
                logger.exception("INFERENCE_FAILED")
                raise HTTPException(status_code=500, detail=f"inference failed: {error}") from error
            inference_ms = int((time.perf_counter() - started) * 1000)

        logger.info("INFERENCE_END ms=%d queue_ms=%d pages=%d", inference_ms, queue_ms, len(pages))
        return JSONResponse(
            {
                "success": True,
                "pages": pages,
                "model": "PaddleOCR-VL-0.9B",
                "inference_ms": inference_ms,
                "queue_ms": queue_ms,
            }
        )
    finally:
        # Stateless means stateless: nothing survives the request, even on error.
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
