"""PaddleOCR-VL runner. Executed as a separate process, on its own sys.path.

PaddleOCR 3.x (which owns the VL pipeline) and PaddleOCR 2.9 (which the
geometric reader is built on) are the same import name and cannot share an
interpreter. Rather than upgrade the geometric path — the fallback that has to
keep working when the model is absent — the VL engine is given its own
site-packages tree and its own process. This file is the only code that runs
inside it.

It stays deliberately thin: run the pipeline, dump what it produced, exit.
Turning that into the shape the exporter wants happens in :mod:`paddle_vl`,
back in the main interpreter, so the conversion is testable without any of
this being installed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _bootstrap() -> None:
    """Point PaddleX at the local cache and keep it off the health probe.

    This process is already launched by the VL virtual environment's own
    interpreter, so PaddleOCR 3.x is on ``sys.path`` without any help.
    """
    models = os.environ.get("VERTEX_VL_MODELS")
    if models:
        Path(models).mkdir(parents=True, exist_ok=True)
        # PaddleX keeps official weights under <cache>/official_models. Pointing
        # it at user data is what makes the download survive an app upgrade.
        os.environ["PADDLE_PDX_CACHE_HOME"] = models
    # PaddleX pings each model host before downloading and treats a slow or
    # filtered probe as "no hosting platform available", which aborts a download
    # that would have worked. The geometric path disables the same check.
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "huggingface")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    _trust_the_operating_system()
    _use_every_core()


def _trust_the_operating_system() -> None:
    """Verify TLS against the Windows certificate store, not a bundled list.

    The download libraries ship their own CA list (certifi). On any network that
    inspects HTTPS — a company proxy, some antivirus products — the connection
    is re-signed by a local authority that is installed in Windows but is not in
    that bundled list, so every model host fails certificate verification and
    the first-run download dies with "no model source is available".

    Measured on the customer's own class of machine: every host failed with
    certifi and every host returned 200 through the OS store. Windows already
    trusts whatever the administrator installed, so deferring to it is both the
    working answer and the correct one — verification stays on.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception as error:
        # Not fatal: on a plain network certifi is sufficient.
        _progress(f"تعذّر استخدام مخزن شهادات ويندوز: {type(error).__name__}")
    if (os.environ.get("VERTEX_VL_DEVICE") or "cpu").strip().casefold() != "gpu":
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")


def _use_every_core() -> None:
    """Give the model the whole machine.

    This runs on a dedicated appliance — nothing else uses the box — so the
    defaults, which hold threads back to stay a good neighbour, only leave the
    customer waiting. MKL-DNN is enabled here too; the geometric reader keeps it
    off because PaddleOCR 2.9 was unstable with it, but that is a different
    library version in a different process and the choice does not carry over.
    """
    cores = max(1, os.cpu_count() or 1)
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = str(cores)
    os.environ["FLAGS_use_mkldnn"] = "1"
    os.environ.setdefault("OMP_WAIT_POLICY", "ACTIVE")
    os.environ.setdefault("KMP_BLOCKTIME", "1")


def _apply_thread_count() -> None:
    """Tell Paddle itself, once it is imported, to use every core."""
    try:
        import paddle

        paddle.set_num_threads(max(1, os.cpu_count() or 1))
    except Exception as error:
        _progress(f"تعذّر ضبط عدد الأنوية: {type(error).__name__}")


_CHANNEL: Any = None


def _claim_stdout() -> None:
    """Keep the JSON channel to ourselves.

    PaddleX draws a download progress bar straight onto stdout, and the desktop
    app parses this process's stdout as one JSON object per line. A single bar
    frame breaks that protocol, so the real stdout is handed to this module and
    file descriptor 1 is pointed at stderr, where any library chatter is
    harmless.
    """
    global _CHANNEL
    if _CHANNEL is not None:
        return
    try:
        _CHANNEL = os.fdopen(os.dup(1), "w", encoding="utf-8", errors="replace")
        os.dup2(2, 1)
    except OSError:
        _CHANNEL = sys.stdout


def _progress(message: str, fraction: float = 0.0, completed: int = 0, total: int = 0) -> None:
    """One JSON line per update, matching what the desktop app already parses."""
    payload: dict[str, Any] = {"message": message}
    if fraction:
        payload["fraction"] = round(float(fraction), 4)
    if total:
        payload["completed"] = int(completed)
        payload["total"] = int(total)
    # ASCII-escaped: on a Western Windows console stdout may be cp1252, and raw
    # Arabic would raise. C#'s JsonSerializer unescapes it.
    line = json.dumps(payload, ensure_ascii=True)
    channel = _CHANNEL or sys.stdout
    channel.write(line + "\n")
    channel.flush()


def _device() -> str:
    """The device to run on, verified rather than assumed.

    The bundled paddlepaddle is the CPU build, so asking for "gpu" because the
    machine happens to have a card would fail at model load. A GPU is used only
    when the installed paddle was actually compiled for CUDA, which is the case
    only if someone has installed paddlepaddle-gpu into this environment.
    """
    if (os.environ.get("VERTEX_VL_DEVICE") or "").strip().casefold() != "gpu":
        return "cpu"
    try:
        import paddle

        if paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
            return "gpu"
    except Exception:
        pass
    return "cpu"


def _check_imports() -> None:
    """Name the missing piece before PaddleX buries it in a stack trace.

    When a compiled dependency will not load — most often because the machine
    lacks the Visual C++ runtime that Paddle links against — the failure
    surfaces as an ImportError thrown many frames deep inside PaddleX, and what
    reaches the customer is a wall of file paths. Importing the pieces here, in
    order, turns that into one sentence naming the module that failed.
    """
    for module, hint in (
        ("numpy", "مكوّن الحساب الرقمي"),
        ("pandas", "مكوّن الجداول"),
        ("paddle", "محرّك PaddleOCR-VL"),
    ):
        try:
            __import__(module)
        except Exception as error:
            raise ImportError(
                f"تعذّر تحميل {hint} ({module}): {type(error).__name__}: {error}. "
                "غالباً تنقص الجهاز مكتبات Microsoft Visual C++ (x64)."
            ) from error


def _build():
    _check_imports()
    from paddleocr import PaddleOCRVL

    _apply_thread_count()
    return PaddleOCRVL(device=_device())


# What the weights occupy once unpacked, used to turn bytes-on-disk into a
# percentage. Measured, not guessed: PaddleOCR-VL-1.6 plus PP-DocLayoutV3.
EXPECTED_MODEL_BYTES = 2_060_000_000


def _directory_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def fetch_models() -> int:
    """Download the weights, reporting progress while it happens.

    PaddleX offers no download callback, so the growing size of the cache
    directory is polled instead. Without this the customer stares at a still
    progress bar for a couple of gigabytes and concludes the app has hung —
    which is exactly what this one-time step must not look like.
    """
    import threading

    root = Path(os.environ.get("VERTEX_VL_MODELS") or ".")
    root.mkdir(parents=True, exist_ok=True)
    _progress("جارٍ تحضير تنزيل أوزان النموذج…")

    done = threading.Event()
    failure: list[BaseException] = []

    def work() -> None:
        try:
            _build()
        except BaseException as error:  # reported on the main thread
            failure.append(error)
        finally:
            done.set()

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    while not done.wait(timeout=2.0):
        size = _directory_size(root)
        fraction = min(0.99, size / EXPECTED_MODEL_BYTES) if size else 0.0
        _progress(
            "جارٍ تنزيل أوزان النموذج…",
            fraction=fraction,
            completed=size,
            total=max(EXPECTED_MODEL_BYTES, size),
        )
    worker.join()
    if failure:
        raise failure[0]

    size = _directory_size(root)
    _progress("اكتمل تنزيل أوزان النموذج.", fraction=1.0, completed=size, total=size)
    return 0


def _plain(value: Any) -> Any:
    """Make the pipeline's result JSON-serialisable.

    The result carries numpy arrays and scalars; ``json.dumps`` refuses them,
    and the image arrays would be enormous even if it did not.
    """
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if hasattr(value, "tolist"):
        try:
            # A page image is megabytes of pixels nobody downstream reads.
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


def _result_payload(result: Any) -> dict[str, Any]:
    # `json` and `markdown` are computed properties, not plain attributes: they
    # can raise on a page the pipeline only partly understood. A page that
    # cannot be serialised should degrade, not abort the batch.
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
    elif isinstance(body, dict) and isinstance(body.get("markdown"), dict):
        markdown = str(body["markdown"].get("markdown_texts") or "")
    payload = _plain(body)
    return {"result": payload if isinstance(payload, dict) else {}, "markdown": markdown}


def parse(image: str, destination: str) -> int:
    _progress("جارٍ تحميل نموذج PaddleOCR-VL المحلي…")
    pipeline = _build()
    _progress("جارٍ قراءة الصفحة…")
    pages = [_result_payload(result) for result in pipeline.predict(image)]
    Path(destination).write_text(
        json.dumps({"pages": pages}, ensure_ascii=False), encoding="utf-8"
    )
    _progress(f"تمت قراءة {len(pages)} صفحة.", fraction=1.0)
    return 0


def serve() -> int:
    """Stay alive and read pages on demand.

    Loading this model costs about two and a half minutes on a CPU. Spawning a
    process per page would pay that for every page, every retry and every file
    in a batch, which is far more than the reading itself costs. So the model is
    loaded once and the process then answers one request per line of stdin:

        {"image": "...", "out": "..."}  ->  {"ok": true, "out": "..."}

    An empty line or EOF ends it.
    """
    _progress("جارٍ تحميل نموذج PaddleOCR-VL المحلي…")
    pipeline = _build()
    _progress("النموذج جاهز.", fraction=1.0)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            break
        try:
            request = json.loads(line)
            pages = [_result_payload(result) for result in pipeline.predict(request["image"])]
            Path(request["out"]).write_text(
                json.dumps({"pages": pages}, ensure_ascii=False), encoding="utf-8"
            )
            _reply({"ok": True, "out": request["out"], "pages": len(pages)})
        except Exception as error:  # one bad page must not kill the server
            _reply({"ok": False, "error": f"{type(error).__name__}: {error}"})
    return 0


def _reply(payload: dict[str, Any]) -> None:
    channel = _CHANNEL or sys.stdout
    channel.write(json.dumps(payload, ensure_ascii=True) + "\n")
    channel.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image")
    parser.add_argument("--out")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--fetch-models", action="store_true")
    args = parser.parse_args()
    _claim_stdout()
    _bootstrap()
    if args.fetch_models:
        return fetch_models()
    if args.serve:
        return serve()
    if not args.image or not args.out:
        raise SystemExit("مرّر --image و--out أو --serve أو --fetch-models.")
    return parse(args.image, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
