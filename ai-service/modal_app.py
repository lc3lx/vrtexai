"""Excel Clear vision service, deployed on Modal.

This is a *deployment wrapper*, not a second implementation. The service itself
lives in ``app.py`` and is the same FastAPI application that runs locally, so
``/health`` and ``/process`` behave identically whether the backend is pointed at
a laptop or at a GPU in a datacentre. ``HttpProvider`` needs no change at all —
only ``AI_SERVICE_URL``.

Three things this file exists to arrange:

* **A GPU build of Paddle.** ``paddlepaddle-gpu`` on PyPI stops at 2.6.2; the 3.x
  GPU wheels are published only on Paddle's own index. The version is pinned to
  the same 3.2.2 the desktop environment uses, so the hosted service and the
  local fallback run identical code.
* **Weights on a volume.** The model is roughly 2GB. Downloading it on every cold
  start would burn the free credit on network transfer instead of inference.
* **One page at a time.** PaddleOCR-VL is not safe to call concurrently, so a
  container accepts a single request and Modal starts more containers instead.
"""
from __future__ import annotations

import modal

MODEL_DIR = "/models"
PADDLE_INDEX = "https://www.paddlepaddle.org.cn/packages/stable/cu126/"

image = (
    modal.Image.debian_slim(python_version="3.12")
    # OpenCV and Paddle both link against these; the slim image has neither.
    .apt_install("libgl1", "libglib2.0-0", "libgomp1", "ca-certificates")
    .pip_install(
        "paddlepaddle-gpu==3.2.2",
        # extra_index_url, not index_url: the wheel comes from Paddle's server
        # but its dependencies still come from PyPI.
        extra_index_url=PADDLE_INDEX,
    )
    .pip_install(
        # numpy 2.x is mandatory, not a preference: every wheel below is built
        # against the numpy 2 ABI, and pairing them with numpy 1.x fails at
        # import with "numpy._core.multiarray failed to import".
        "numpy>=2.1,<2.4",
        "paddleocr==3.7.0",
        "paddlex[ocr]==3.7.2",
        "truststore>=0.10",
        "fastapi==0.115.6",
        "python-multipart==0.0.20",
    )
    .env(
        {
            "PADDLE_PDX_CACHE_HOME": MODEL_DIR,
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
            "VL_DEVICE": "gpu",
            "PYTHONUNBUFFERED": "1",
        }
    )
    .add_local_file("app.py", "/root/app.py", copy=True)
)

app = modal.App("excel-clear-vision")

# Weights survive between containers here. Without it every cold start would
# re-download two gigabytes before it could read a single page.
weights = modal.Volume.from_name("excel-clear-vl-weights", create_if_missing=True)


@app.cls(
    image=image,
    gpu="L4",
    volumes={MODEL_DIR: weights},
    # Stay warm for five minutes after the last page. Loading the model costs
    # far more than idling briefly, and a burst of uploads is the normal shape
    # of this workload.
    scaledown_window=300,
    timeout=3600,
    secrets=[modal.Secret.from_name("excel-clear-ai", required_keys=["AI_SERVICE_KEY"])],
)
@modal.concurrent(max_inputs=1)
class Vision:
    @modal.enter()
    def start(self) -> None:
        """Load the model once, when the container starts — never per request."""
        import sys

        sys.path.insert(0, "/root")
        import app as service

        service._load_model()
        weights.commit()

    @modal.asgi_app()
    def web(self):
        """Serve the same FastAPI application the desktop service runs."""
        import os
        import sys

        sys.path.insert(0, "/root")
        import app as service
        from fastapi import Request
        from fastapi.responses import JSONResponse

        api = service.app
        expected = os.environ.get("AI_SERVICE_KEY", "")

        @api.middleware("http")
        async def require_key(request: Request, call_next):
            # A public GPU endpoint is someone else's free compute. The key is
            # held by the backend only; the browser never sees it.
            #
            # Returned, not raised: an HTTPException thrown from middleware
            # escapes FastAPI's handlers and surfaces as a 500, which would tell
            # a caller "our fault" when the truth is "wrong key".
            if expected and request.url.path.startswith("/process"):
                header = request.headers.get("authorization", "")
                if header.removeprefix("Bearer ").strip() != expected:
                    return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)

        return api


@app.local_entrypoint()
def smoke(image_path: str = "") -> None:
    """Deploy check: report what the container thinks it has."""
    print("Deployed. Point AI_SERVICE_URL at the printed web URL, then:")
    print("  curl -H 'Authorization: Bearer $AI_SERVICE_KEY' <url>/health")
    if image_path:
        print(f"  curl -F file=@{image_path} -H 'Authorization: Bearer ...' <url>/process")
