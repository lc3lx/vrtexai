"""Optional local vision cross-check for cells OCR is unsure about.

Runs a small vision-language model (Qwen2.5-VL) entirely on the customer's
machine through llama.cpp. It is deliberately *narrow*: it is shown one cell
crop at a time and asked what that cell says. It never sees the whole page and
never produces a table, because a model asked to read a whole document will
happily invent rows — which is the failure this rewrite exists to remove.

Absent model files are not an error. The geometric path is complete on its own;
vision only sharpens cells that are already flagged.
"""
from __future__ import annotations

import base64
import io
import os
import re
from pathlib import Path
from typing import Any

_MODEL: Any = None
_MODEL_ERROR: str | None = None

# Cap the work: a CPU re-read costs roughly 20 seconds per cell, and a page
# with hundreds of doubtful cells is a bad scan, not a job for a vision model.
MAX_CELLS = 12


def models_root() -> Path:
    env = os.environ.get("VERTEX_LLM_MODELS")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    for candidate in (
        here / "llm_models",
        here.parent / "runtime" / "llm_models",
        Path(os.environ.get("PYTHONHOME") or "") / "llm_models",
    ):
        if candidate.is_dir():
            return candidate
    return here / "llm_models"


def _is_usable_gguf(path: Path, minimum_bytes: int) -> bool:
    """Reject a file that is not a complete GGUF.

    An interrupted copy or download leaves a plausible-looking .gguf behind.
    Loading one aborts the process rather than raising, so it is screened out
    here instead of being discovered at extraction time.
    """
    try:
        if path.stat().st_size < minimum_bytes:
            return False
        with path.open("rb") as handle:
            return handle.read(4) == b"GGUF"
    except OSError:
        return False


def find_model() -> tuple[Path, Path] | None:
    """Locate a vision model plus its projector, as (model, mmproj)."""
    root = models_root()
    if not root.is_dir():
        return None
    projectors = [
        path for path in sorted(root.glob("*.gguf"))
        if "mmproj" in path.name.casefold() and _is_usable_gguf(path, 40_000_000)
    ]
    if not projectors:
        return None
    for path in sorted(root.glob("*.gguf")):
        name = path.name.casefold()
        if "mmproj" in name:
            continue
        if ("vl" in name or "vision" in name) and _is_usable_gguf(path, 500_000_000):
            return path, projectors[0]
    return None


def mode() -> str:
    value = (os.environ.get("VERTEX_VISION") or "auto").strip().casefold()
    return value if value in {"auto", "off", "always"} else "auto"


def available() -> tuple[bool, str]:
    if mode() == "off":
        return False, "معطّل عبر VERTEX_VISION=off"
    found = find_model()
    if not found:
        return False, "لا يوجد نموذج رؤية محلي (يلزم ملف VL مع mmproj)."
    try:
        import llama_cpp  # noqa: F401
        from llama_cpp.llama_chat_format import Qwen25VLChatHandler  # noqa: F401
    except Exception as error:
        return False, f"llama-cpp بدون دعم رؤية: {error}"
    return True, str(found[0])


def _load():
    global _MODEL, _MODEL_ERROR
    if _MODEL is not None:
        return _MODEL
    if _MODEL_ERROR:
        raise RuntimeError(_MODEL_ERROR)
    found = find_model()
    if not found:
        _MODEL_ERROR = "لا يوجد نموذج رؤية محلي."
        raise RuntimeError(_MODEL_ERROR)
    model_path, projector_path = found
    try:
        from common import quiet_native_output
        from llama_cpp import Llama
        from llama_cpp.llama_chat_format import Qwen25VLChatHandler

        with quiet_native_output():
            handler = Qwen25VLChatHandler(clip_model_path=str(projector_path), verbose=False)
            _MODEL = Llama(
                model_path=str(model_path),
                chat_handler=handler,
                n_ctx=4096,
                n_threads=max(1, min(6, os.cpu_count() or 2)),
                n_batch=192,
                verbose=False,
            )
    except Exception as error:
        _MODEL_ERROR = f"تعذّر تحميل نموذج الرؤية: {type(error).__name__}: {error}"
        raise RuntimeError(_MODEL_ERROR) from error
    return _MODEL


def _data_uri(crop) -> str:
    from PIL import Image

    buffer = io.BytesIO()
    Image.fromarray(crop).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _crop(image, box: tuple[float, float, float, float], margin: float = 0.35):
    import numpy as np

    height, width = image.shape[:2]
    x0, y0, x1, y1 = box
    pad_x = max(8.0, (x1 - x0) * margin)
    pad_y = max(8.0, (y1 - y0) * margin)
    left = int(max(0, x0 - pad_x))
    top = int(max(0, y0 - pad_y))
    right = int(min(width, x1 + pad_x))
    bottom = int(min(height, y1 + pad_y))
    if right - left < 6 or bottom - top < 6:
        return None
    crop = np.asarray(image)[top:bottom, left:right]
    if crop.size == 0:
        return None
    # Small crops are what the model struggles with; give it pixels to work on.
    scale = max(1.0, 160.0 / max(1, crop.shape[0]))
    if scale > 1.0:
        import cv2

        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return crop


def read_cell(image, box: tuple[float, float, float, float], hint: str = "") -> str:
    """Ask the vision model what one cell says. Returns "" when unusable."""
    crop = _crop(image, box)
    if crop is None:
        return ""
    model = _load()
    instruction = (
        "You are reading one cell cropped from a printed business document. "
        "Reply with the exact text of the cell in the CENTRE of the image and nothing else. "
        "No explanation, no quotes, no label. If the centre cell is empty, reply EMPTY."
    )
    if hint:
        instruction += f" The automatic reader guessed: {hint!r} — correct it if wrong."
    from common import quiet_native_output

    try:
        with quiet_native_output():
            result = model.create_chat_completion(
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _data_uri(crop)}},
                        {"type": "text", "text": instruction},
                    ],
                }],
                temperature=0.0,
                max_tokens=48,
            )
    except Exception:
        return ""
    text = str(result["choices"][0]["message"]["content"] or "").strip()
    # The model likes to answer in the shape of a label ("​: جدة"), so the lead-in
    # is removed before anything else — otherwise its own "EMPTY" signal arrives
    # as ": EMPTY" and gets written into the cell as data.
    text = re.sub(r"^[`\"'\s:：]+|[`\"'\s:：]+$", "", text)
    if text.casefold() in {"empty", "none", "n/a", "null", ""}:
        return ""
    if len(text) > 80 or "\n" in text:
        # A sentence means the model started describing the picture.
        return ""
    # The model spaces out digits it reads one at a time (". 75" for "0.75").
    if not re.search(r"[A-Za-z؀-ۿ]", text):
        text = re.sub(r"(?<=[\d.,])\s+(?=[\d.,])", "", text)
        if text.startswith((".", ",")):
            text = "0" + text
    return text


def refine_document(image, document: dict[str, Any], threshold: float = 70.0) -> list[str]:
    """Re-read the least trustworthy cells in place. Returns notes."""
    notes: list[str] = []
    ok, detail = available()
    if not ok:
        return [f"vision:unavailable:{detail}"] if mode() == "always" else []

    doubtful: list[dict[str, Any]] = []
    for region in document.get("regions") or []:
        for row in region.get("rows") or []:
            for cell in row:
                text = str(cell.get("text") or "").strip()
                if not text or cell.get("x1", 0) <= cell.get("x0", 0):
                    continue
                if float(cell.get("conf") or 100.0) < threshold or cell.get("review"):
                    doubtful.append(cell)
    if not doubtful:
        return []
    doubtful.sort(key=lambda cell: float(cell.get("conf") or 0.0))
    doubtful = doubtful[:MAX_CELLS]

    try:
        _load()
    except Exception as error:
        return [f"vision:load-failed:{type(error).__name__}"]

    changed = offered = 0
    for cell in doubtful:
        box = (cell["x0"], cell["y0"], cell["x1"], cell["y1"])
        reading = read_cell(image, box, hint=str(cell.get("text") or ""))
        if not reading or reading == cell["text"]:
            continue
        alternatives = cell.setdefault("alternatives", [])
        numeric = bool(re.fullmatch(r"[\s\d.,%$€£﷼()+\-/]+", cell["text"] or ""))
        if numeric and float(cell.get("conf") or 0.0) >= 50.0:
            # A number the OCR is only somewhat unsure about is left alone. The
            # vision reading joins the alternatives, and the arithmetic pass
            # decides — it can tell which digit is right; neither model can.
            alternatives.insert(0, {"text": reading, "conf": 60.0})
            cell["review"] = True
            offered += 1
            continue
        alternatives.insert(0, {"text": cell["text"], "conf": float(cell.get("conf") or 0.0)})
        cell["text"] = reading
        cell["review"] = True
        cell["note"] = f"أعاد نموذج الرؤية قراءتها (كانت: {alternatives[0]['text']})"
        changed += 1
    notes.append(f"vision:rechecked:{len(doubtful)}:changed:{changed}:offered:{offered}")
    return notes
