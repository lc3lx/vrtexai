"""OpenCV / Pillow page cleanup before OCR."""
from __future__ import annotations

from typing import Any

import numpy as np


def detect_mode(image: Any) -> str:
    if image.ndim == 3:
        std = float(np.std(image))
        height, width = image.shape[:2]
        if width > 0 and height / width > 1.6:
            return "receipt"
        # Spreadsheet / UI screenshots often sit above the old 38 threshold.
        if std < 55:
            return "screenshot"
    else:
        height, width = image.shape[:2]
        if width > 0 and height / width > 1.6:
            return "receipt"
    return "scan"


def drop_color_noise(image: Any) -> Any:
    """Remove scattered watermarks and pen marks without wiping colored header bars."""
    import cv2
    if image.ndim != 3 or image.shape[2] < 3:
        return image
    hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
    green = cv2.inRange(hsv, (35, 18, 40), (90, 255, 255))
    blue = cv2.inRange(hsv, (90, 50, 40), (130, 255, 255))
    mask = cv2.bitwise_or(green, blue)
    row_density = (mask > 0).mean(axis=1)
    for y, density in enumerate(row_density):
        if density > 0.55:
            mask[y] = 0
    if int(np.count_nonzero(mask)) < 80:
        return image
    return cv2.inpaint(image, mask, 3, cv2.INPAINT_TELEA)


def enhance_gray(gray: Any) -> Any:
    """Lift low-contrast (greyed) text without destroying light gridlines."""
    import cv2
    if gray.ndim != 2:
        return gray
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def deskew(binary: Any) -> Any:
    import cv2
    points = np.column_stack(np.where(binary < 128))
    if len(points) < 50:
        return binary
    angle = cv2.minAreaRect(points)[-1]
    angle = -(90 + angle) if angle < -45 else -angle
    if 0.2 < abs(angle) < 15:
        height, width = binary.shape[:2]
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
        return cv2.warpAffine(binary, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return binary


def sharpen(gray: Any) -> Any:
    import cv2
    blur = cv2.GaussianBlur(gray, (0, 0), 1.0)
    return cv2.addWeighted(gray, 1.4, blur, -0.4, 0)


def preprocess(image: Any, mode: str | None = None) -> Any:
    import cv2
    cleaned = drop_color_noise(image) if image.ndim == 3 else image
    gray = cv2.cvtColor(cleaned, cv2.COLOR_RGB2GRAY) if cleaned.ndim == 3 else cleaned
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    chosen = mode or detect_mode(image)
    if chosen == "screenshot":
        denoised = cv2.fastNlMeansDenoising(gray, None, 6, 7, 21)
        return cv2.adaptiveThreshold(denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, 7)
    if chosen == "receipt":
        sharp = sharpen(gray)
        denoised = cv2.fastNlMeansDenoising(sharp, None, 12, 7, 21)
        _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return deskew(binary)
    sharp = sharpen(gray)
    denoised = cv2.fastNlMeansDenoising(sharp, None, 10, 7, 21)
    binary = cv2.adaptiveThreshold(denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11)
    return deskew(binary)
