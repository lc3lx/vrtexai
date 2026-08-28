---
title: Excel Clear Vision
emoji: 📐
colorFrom: gray
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# Excel Clear Vision

PaddleOCR-VL 0.9B behind a single endpoint. Reads one page image and returns its
structure — layout blocks and HTML tables — as JSON.

It does **not** decide whether what it read is correct. Validation (shape,
arithmetic, and pixel evidence from an independent OCR reader) runs on the Excel
Clear backend, deliberately: a model that graded its own output would prove
nothing.

## Endpoints

    GET  /health    -> {"ready": bool, "detail": str, "model": str}
    POST /process   -> multipart "file"; returns page structure

Response:

```json
{
  "success": true,
  "pages": [{"result": {"parsing_res_list": []}, "markdown": ""}],
  "model": "PaddleOCR-VL-0.9B",
  "inference_ms": 18400,
  "queue_ms": 120
}
```

This is the same shape the desktop `vl_worker.py` writes, so one parser on the
backend handles both the hosted service and the local fallback.

## State

None. Each request writes one temporary file, runs inference, and deletes it in
a `finally` block. No accounts, no documents, no logs of content.

## Model loading

The model loads once at start-up (`MODEL_LOADING` → `MODEL_READY` in the logs)
and is held in memory. Requests are serialised through a lock: PaddleOCR-VL is
not safe to call concurrently, and a GPU serves one page at a time regardless.
