"""Perception layer: image -> words with coordinates, confidence and script.

This is the single source of truth for *what the image says* and *where*.
Everything downstream (layout reconstruction, verification, export) works from
the words this module returns; nothing further re-reads the pixels except the
optional vision cross-check.

Why it exists: the previous pipeline handed a flat Tesseract text blob to a
text model, which destroyed all geometry and forced the model to guess table
structure.  PaddleOCR gives boxes + confidences, so the structure can be
recovered deterministically instead of invented.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

import numpy as np

ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
# Arabic *letters* only — Arabic-Indic digits (٠-٩) are deliberately excluded.
# A digit in Arabic script is not evidence that the region is Arabic text, and
# treating it as such is how numeric cells used to be lost.
ARABIC_LETTER = re.compile(r"[ؠ-يٮ-ۓۺ-ۿﭐ-ﴽﵐ-ﷻﹰ-ﻼ]")
ARABIC_DIGIT = re.compile(r"[٠-٩۰-۹]")
LATIN = re.compile(r"[A-Za-z]")
NUMERIC_ONLY = re.compile(r"^[\s\d.,:/\\%$€£﷼+\-()#*]+$")

# Boxes below this are re-read at higher magnification before we trust them.
RETRY_CONFIDENCE = 0.75
# Short side we upscale to before detection; small text on phone photos of a
# screen is the main failure mode and detection is resolution sensitive.
TARGET_SHORT_SIDE = 1600

_ENGINES: dict[str, Any] = {}
_ENGINE_ERROR: str | None = None


# --------------------------------------------------------------------------
# Engine construction (offline, CPU only)
# --------------------------------------------------------------------------
def models_root() -> Path:
    """Locate the bundled PaddleOCR inference models."""
    env = os.environ.get("VERTEX_PADDLE_MODELS")
    if env:
        return Path(env)
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


def _model_dir(*parts: str) -> str | None:
    path = models_root().joinpath(*parts)
    if path.is_dir() and any(path.iterdir()):
        return str(path)
    return None


def _rec_dir(preferred: str, fallback_glob: str) -> str | None:
    direct = _model_dir("rec", preferred)
    if direct:
        return direct
    root = models_root() / "rec"
    if root.is_dir():
        for path in sorted(root.glob(fallback_glob)):
            if path.is_dir() and any(path.iterdir()):
                return str(path)
    return None


def _char_dict(name: str) -> str | None:
    try:
        import paddleocr

        path = Path(paddleocr.__file__).resolve().parent / "ppocr" / "utils" / name
        return str(path) if path.is_file() else None
    except Exception:
        return None


def _prepare_paddle_environment() -> None:
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("HUB_HOME", str(models_root()))


def _engine(script: str):
    """Build (once) a PaddleOCR instance whose recognizer matches `script`.

    Two recognizers are kept because neither one alone is good enough on the
    documents this product sees: the Arabic model mangles Latin text and drops
    digit-only cells, and the English model cannot emit Arabic glyphs at all.
    """
    global _ENGINE_ERROR
    if script in _ENGINES:
        return _ENGINES[script]
    if _ENGINE_ERROR:
        raise RuntimeError(_ENGINE_ERROR)

    _prepare_paddle_environment()
    try:
        import paddleocr.paddleocr as paddleocr_mod
        from paddleocr import PaddleOCR

        # PaddleOCR reaches for the network on construction even when every
        # model directory is supplied. Customer machines must never do that.
        paddleocr_mod.maybe_download = lambda model_dir, url: None
    except Exception as error:
        _ENGINE_ERROR = f"PaddleOCR unavailable: {type(error).__name__}: {error}"
        raise RuntimeError(_ENGINE_ERROR) from error

    if script == "ar":
        rec = _rec_dir("arabic_PP-OCRv3_rec_infer", "arabic*")
        char_dict = _char_dict("dict/arabic_dict.txt")
    else:
        rec = _rec_dir("en_PP-OCRv3_rec_infer", "en_*")
        char_dict = _char_dict("en_dict.txt") or _char_dict("dict/en_dict.txt")

    kwargs: dict[str, Any] = {
        "use_gpu": False,
        "show_log": False,
        "lang": "en",
        "use_angle_cls": False,
        "enable_mkldnn": False,
        # Patient detection: keep faint gridline-adjacent text instead of
        # dropping it, and let boxes grow enough to cover full cell contents.
        "det_db_thresh": 0.25,
        "det_db_box_thresh": 0.45,
        "det_db_unclip_ratio": 1.8,
        "max_text_length": 100,
    }
    det = _model_dir("det", "Multilingual_PP-OCRv3_det_infer") or _model_dir("det")
    if det:
        kwargs["det_model_dir"] = det
    if rec:
        kwargs["rec_model_dir"] = rec
    if char_dict:
        kwargs["rec_char_dict_path"] = char_dict

    try:
        engine = PaddleOCR(**kwargs)
    except TypeError:
        slim = {
            key: value
            for key, value in kwargs.items()
            if key in {
                "use_gpu", "show_log", "lang", "use_angle_cls",
                "det_model_dir", "rec_model_dir", "rec_char_dict_path",
            }
        }
        engine = PaddleOCR(**slim)
    _ENGINES[script] = engine
    return engine


def paddle_available() -> tuple[bool, str]:
    """Used by the self-check so the installer can be validated before shipping."""
    try:
        _engine("en")
        _engine("ar")
        return True, str(models_root())
    except Exception as error:
        return False, str(error)


# --------------------------------------------------------------------------
# Image conditioning
# --------------------------------------------------------------------------
def _deskew_angle(gray: np.ndarray) -> float:
    """Small-angle text skew, measured from the text mask rather than edges."""
    import cv2

    inverted = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    # Join glyphs into text lines so minAreaRect measures the baseline.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3))
    joined = cv2.morphologyEx(inverted, cv2.MORPH_CLOSE, kernel)
    points = cv2.findNonZero(joined)
    if points is None or len(points) < 200:
        return 0.0
    angle = cv2.minAreaRect(points)[-1]
    if angle < -45:
        angle = 90 + angle
    elif angle > 45:
        angle = angle - 90
    return float(angle) if abs(angle) < 15 else 0.0


def prepare_image(image: np.ndarray) -> np.ndarray:
    """Condition a page for PaddleOCR.

    Deliberately does NOT binarize. `preprocess.preprocess()` returns an
    adaptive-threshold image, which is right for Tesseract and wrong here —
    Paddle's detector and recognizer were trained on natural images and lose
    accuracy on hard black-and-white input.
    """
    import cv2

    from preprocess import drop_color_noise

    work = np.asarray(image)
    if work.ndim == 2:
        work = cv2.cvtColor(work, cv2.COLOR_GRAY2RGB)
    elif work.shape[2] == 4:
        work = cv2.cvtColor(work, cv2.COLOR_RGBA2RGB)
    work = np.ascontiguousarray(work.astype(np.uint8))

    try:
        work = drop_color_noise(work)
    except Exception:
        pass

    # Photos of screens carry moire and glare. Bilateral filtering flattens
    # both while keeping glyph edges; blur-based denoisers erase thin digits.
    gray = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
    if float(np.std(gray)) < 70:
        work = cv2.bilateralFilter(work, 7, 45, 45)

    angle = _deskew_angle(gray)
    if abs(angle) > 0.3:
        height, width = work.shape[:2]
        matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
        work = cv2.warpAffine(
            work, matrix, (width, height),
            flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE,
        )

    height, width = work.shape[:2]
    short = min(height, width)
    if short and short < TARGET_SHORT_SIDE:
        scale = min(TARGET_SHORT_SIDE / float(short), 3.0)
        work = cv2.resize(work, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return work


# --------------------------------------------------------------------------
# Detection + dual recognition
# --------------------------------------------------------------------------
def _detect(image: np.ndarray) -> list[np.ndarray]:
    result = _engine("en").ocr(image, det=True, rec=False, cls=False)
    boxes = result[0] if result and isinstance(result, list) else None
    out: list[np.ndarray] = []
    for box in boxes or []:
        array = np.asarray(box, dtype=np.float32)
        if array.shape == (4, 2):
            out.append(array)
    return out


def _crop(image: np.ndarray, box: np.ndarray, pad: int = 0) -> np.ndarray | None:
    from paddleocr.tools.infer.utility import get_rotate_crop_image

    work = box
    if pad:
        centre = work.mean(axis=0)
        work = centre + (work - centre) * (1.0 + pad / 100.0)
    height, width = image.shape[:2]
    work[:, 0] = np.clip(work[:, 0], 0, width - 1)
    work[:, 1] = np.clip(work[:, 1], 0, height - 1)
    try:
        crop = get_rotate_crop_image(image, work.astype(np.float32))
    except Exception:
        x0, y0 = work.min(axis=0)
        x1, y1 = work.max(axis=0)
        crop = image[int(y0):int(y1), int(x0):int(x1)]
    if crop is None or crop.size == 0 or crop.shape[0] < 4 or crop.shape[1] < 4:
        return None
    return crop


def _recognize(crops: list[np.ndarray], script: str) -> list[tuple[str, float]]:
    if not crops:
        return []
    try:
        result = _engine(script).ocr(crops, det=False, rec=True, cls=False)
    except Exception:
        return [("", 0.0)] * len(crops)
    rows = result[0] if result and isinstance(result, list) and len(result) == 1 and isinstance(result[0], list) else result
    out: list[tuple[str, float]] = []
    for item in rows or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((str(item[0] or "").strip(), float(item[1] or 0.0)))
        else:
            out.append(("", 0.0))
    while len(out) < len(crops):
        out.append(("", 0.0))
    return out[: len(crops)]


def _choose(en: tuple[str, float], ar: tuple[str, float]) -> tuple[str, float, str]:
    """Pick between the two recognizers for one box.

    The evidence is asymmetric, and that asymmetry is the whole rule:

    * The English dictionary contains no Arabic glyphs, so English output is
      never proof the region is Latin — the model simply cannot say otherwise.
    * The Arabic dictionary *does* contain Latin letters and digits. So when
      the Arabic model itself returns Latin, the region is certainly not Arabic.

    The dangerous case is a lone digit: the Arabic model confidently returns an
    Arabic word ("يم" at 0.80) where English returns the correct "3" at 0.22.
    Confidence alone picks the wrong one, which is exactly how the quantity
    column used to vanish. Numeric English output therefore always wins.
    """
    en_text, en_conf = en
    ar_text, ar_conf = ar
    if not en_text and not ar_text:
        return "", 0.0, "und"
    if not en_text:
        return ar_text, ar_conf, "ar" if ARABIC_LETTER.search(ar_text) else "latin"
    if not ar_text:
        return en_text, en_conf, "latin"

    if not ARABIC_LETTER.search(ar_text):
        # Both recognizers agree the region is not Arabic script.
        if ARABIC_DIGIT.search(ar_text) and ar_conf >= en_conf + 0.25:
            return ar_text, ar_conf, "ar"
        return (en_text, en_conf, "latin") if en_conf + 0.15 >= ar_conf else (ar_text, ar_conf, "latin")

    # Arabic letters present: real Arabic, or the model hallucinating on a
    # numeral. A number cannot be Arabic letters, so trust English there.
    if NUMERIC_ONLY.match(en_text):
        return en_text, en_conf, "latin"
    if ar_conf >= en_conf:
        return ar_text, ar_conf, "ar"
    return en_text, en_conf, "latin"


def _word(box: np.ndarray, text: str, conf: float, script: str) -> dict[str, Any]:
    xs, ys = box[:, 0], box[:, 1]
    return {
        "text": text,
        "conf": round(float(conf) * 100.0, 1),
        "x0": float(xs.min()), "y0": float(ys.min()),
        "x1": float(xs.max()), "y1": float(ys.max()),
        "script": script,
        # Competing readings of the same pixels, best-first. Paddle's scores are
        # not comparable across crop scales, so a tighter re-read that disagrees
        # is kept rather than discarded: arithmetic verification downstream can
        # tell which reading is right far more reliably than confidence can.
        "alternatives": [],
    }


def _add_alternative(word: dict[str, Any], text: str, conf: float) -> None:
    text = (text or "").strip()
    if not text or text == word["text"]:
        return
    score = round(float(conf) * 100.0, 1)
    for existing in word["alternatives"]:
        if existing["text"] == text:
            existing["conf"] = max(existing["conf"], score)
            return
    word["alternatives"].append({"text": text, "conf": score})
    word["alternatives"].sort(key=lambda item: -item["conf"])
    del word["alternatives"][3:]


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    ax0, ay0 = a[:, 0].min(), a[:, 1].min()
    ax1, ay1 = a[:, 0].max(), a[:, 1].max()
    bx0, by0 = b[:, 0].min(), b[:, 1].min()
    bx1, by1 = b[:, 0].max(), b[:, 1].max()
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(1.0, (ax1 - ax0) * (ay1 - ay0))
    area_b = max(1.0, (bx1 - bx0) * (by1 - by0))
    return float(inter / min(area_a, area_b))


def read_words(
    image: np.ndarray, thorough: bool = True
) -> tuple[list[dict[str, Any]], list[str], np.ndarray]:
    """Read one page into words carrying position, confidence and script.

    Returns the conditioned image alongside the words, and coordinates are in
    *that* image's space. Conditioning deskews the page, so the words no longer
    line up with the original pixels — anything that later measures the page
    (printed rules, vision crops) has to measure the same image the words were
    read from.
    """
    warnings: list[str] = []
    original = np.asarray(image)
    if original.ndim == 2:
        import cv2

        original = cv2.cvtColor(original, cv2.COLOR_GRAY2RGB)
    prepared = prepare_image(original)

    boxes = _detect(prepared)
    if thorough:
        # Second detection at higher magnification recovers short, isolated
        # cells (bare quantities like "10" / "5") that the first pass misses.
        import cv2

        big = cv2.resize(prepared, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_CUBIC)
        extra = [box / 1.6 for box in _detect(big)]
        added = 0
        for candidate in extra:
            if all(_iou(candidate, existing) < 0.35 for existing in boxes):
                boxes.append(candidate)
                added += 1
        if added:
            warnings.append(f"perceive:rescan+{added}")

    if not boxes:
        return [], warnings + ["perceive:no-text"], prepared

    boxes.sort(key=lambda box: (box[:, 1].min(), box[:, 0].min()))
    crops = []
    kept: list[np.ndarray] = []
    for box in boxes:
        crop = _crop(prepared, box.copy())
        if crop is not None:
            crops.append(crop)
            kept.append(box)

    en_results = _recognize(crops, "en")
    ar_results = _recognize(crops, "ar")

    words: list[dict[str, Any]] = []
    # Each entry pairs a word with the box it came from, so the retry pass can
    # re-crop exactly the same region.
    weak: list[tuple[int, np.ndarray]] = []
    for index, box in enumerate(kept):
        text, conf, script = _choose(en_results[index], ar_results[index])
        if not text:
            continue
        words.append(_word(box, text, conf, script))
        if conf < RETRY_CONFIDENCE:
            weak.append((len(words) - 1, box))

    # Re-read low-confidence boxes at 3x with padding; keep whichever wins.
    if thorough and weak:
        import cv2

        retry_crops, retry_index = [], []
        for position, box in weak:
            crop = _crop(prepared, box.copy(), pad=14)
            if crop is None:
                continue
            retry_crops.append(cv2.resize(crop, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC))
            retry_index.append(position)
        if retry_crops:
            retry_en = _recognize(retry_crops, "en")
            retry_ar = _recognize(retry_crops, "ar")
            improved = 0
            for slot, position in enumerate(retry_index):
                text, conf, script = _choose(retry_en[slot], retry_ar[slot])
                if not text:
                    continue
                word = words[position]
                if conf * 100.0 > word["conf"]:
                    _add_alternative(word, word["text"], word["conf"] / 100.0)
                    word.update(text=text, conf=round(conf * 100.0, 1), script=script)
                    improved += 1
                else:
                    _add_alternative(word, text, conf)
            if improved:
                warnings.append(f"perceive:retry+{improved}")

    words = _deduplicate(words)
    return words, warnings, prepared


def _deduplicate(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop boxes that cover the same pixels twice.

    The second detection pass recovers genuinely missed cells but also emits
    partial duplicates of text it already found; keeping both would put the
    same value into a table twice.
    """
    ordered = sorted(words, key=lambda word: -float(word.get("conf") or 0.0))
    kept: list[dict[str, Any]] = []
    for word in ordered:
        box = np.array([
            [word["x0"], word["y0"]], [word["x1"], word["y0"]],
            [word["x1"], word["y1"]], [word["x0"], word["y1"]],
        ], dtype=np.float32)
        clash = None
        for existing in kept:
            other = np.array([
                [existing["x0"], existing["y0"]], [existing["x1"], existing["y0"]],
                [existing["x1"], existing["y1"]], [existing["x0"], existing["y1"]],
            ], dtype=np.float32)
            if _iou(box, other) > 0.55:
                clash = existing
                break
        if clash is None:
            kept.append(word)
        else:
            _add_alternative(clash, word["text"], float(word.get("conf") or 0.0) / 100.0)
    kept.sort(key=lambda word: (word["y0"], word["x0"]))
    return kept


# --------------------------------------------------------------------------
# Tesseract fallback (only when Paddle is unavailable)
# --------------------------------------------------------------------------
def read_words_tesseract(
    image: np.ndarray,
) -> tuple[list[dict[str, Any]], list[str], np.ndarray]:
    from ocr import choose_ocr_lang, ocr_words, setup_tesseract
    from preprocess import preprocess

    pytesseract = setup_tesseract()
    prepared = preprocess(np.asarray(image))
    raw = ocr_words(pytesseract, prepared)
    lang = choose_ocr_lang("", words=raw)
    if lang != "ara+eng":
        raw = ocr_words(pytesseract, prepared, lang=lang)
    words = []
    for item in raw:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        left, top = float(item["left"]), float(item["top"])
        words.append({
            "text": text,
            "conf": max(0.0, float(item.get("conf") or 0.0)),
            "x0": left, "y0": top,
            "x1": left + float(item["width"]),
            "y1": top + float(item["height"]),
            "script": "ar" if ARABIC.search(text) else "latin",
            "alternatives": [],
        })
    return words, ["perceive:tesseract-fallback"], prepared


def read_page(
    image: np.ndarray, thorough: bool = True
) -> tuple[list[dict[str, Any]], list[str], np.ndarray]:
    """Preferred entry point: PaddleOCR, degrading to Tesseract if unavailable.

    The third value is the image the coordinates belong to.
    """
    try:
        words, warnings, prepared = read_words(image, thorough=thorough)
        if words:
            return words, warnings, prepared
        fallback, notes, prepared = read_words_tesseract(image)
        return fallback, warnings + notes, prepared
    except Exception as error:
        words, notes, prepared = read_words_tesseract(image)
        return words, notes + [f"perceive:paddle-failed:{type(error).__name__}"], prepared


def read_document(source: Path, thorough: bool = True) -> tuple[list[list[dict[str, Any]]], list[str]]:
    """Read every page of a file into per-page word lists."""
    from ocr import image_pages

    pages: list[list[dict[str, Any]]] = []
    warnings: list[str] = []
    for image in image_pages(source):
        words, notes, _prepared = read_page(image, thorough=thorough)
        pages.append(words)
        warnings.extend(notes)
        del image
    return pages, warnings


def words_text(words: Iterable[dict[str, Any]]) -> str:
    return " ".join(str(word.get("text") or "") for word in words)
