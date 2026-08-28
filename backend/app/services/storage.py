"""Accepting a file from the internet.

Everything here treats the upload as hostile until proven otherwise: the name
is attacker-controlled, the extension is a claim rather than a fact, and the
declared content type is whatever the client felt like sending.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings

# Signatures, checked against the first bytes of the file itself. An extension
# is a claim; these are evidence.
SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"II*\x00", "image/tiff", ".tif"),
    (b"MM\x00*", "image/tiff", ".tif"),
    (b"BM", "image/bmp", ".bmp"),
]


class UploadRejected(ValueError):
    """The file cannot be accepted.

    Carries a ``code`` as well as English text so the browser can say why in the
    reader's own language, and ``params`` for the numbers the sentence needs.
    """

    def __init__(self, code: str, message: str, **params: object) -> None:
        super().__init__(message)
        self.code = code
        self.params = params


@dataclass
class StoredFile:
    path: Path
    stored_name: str
    original_name: str
    content_type: str
    size_bytes: int


def _sniff(data: bytes) -> tuple[str, str] | None:
    for magic, content_type, suffix in SIGNATURES:
        if data.startswith(magic):
            return content_type, suffix
    # WEBP is RIFF....WEBP — the marker sits at offset 8, not at the start.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def _safe_display_name(name: str) -> str:
    """A filename fit to store and show, with no path in it.

    Only the final component is kept, so "../../etc/passwd" and
    "C:\\Windows\\system32\\x" both reduce to a harmless leaf.
    """
    leaf = Path(name.replace("\\", "/")).name
    cleaned = "".join(ch for ch in leaf if ch.isprintable() and ch not in '<>:"|?*')
    return cleaned[:120] or "document"


def store_upload(data: bytes, original_name: str, customer_id: str) -> StoredFile:
    settings = get_settings()
    if not data:
        raise UploadRejected("file_empty", "The file is empty.")
    if len(data) > settings.max_upload_bytes:
        raise UploadRejected(
            "file_too_large",
            f"The file is larger than the {settings.max_upload_mb} MB limit.",
            limit=settings.max_upload_mb,
        )

    sniffed = _sniff(data)
    if sniffed is None:
        raise UploadRejected(
            "file_type",
            "That file type is not supported. Send a PNG, JPG, WEBP, TIFF, BMP or PDF.",
        )
    content_type, suffix = sniffed

    # The stored name is generated, never derived from the upload: a random
    # name cannot escape its directory or collide with another customer's file.
    stored_name = f"{secrets.token_hex(16)}{suffix}"
    folder = settings.storage_path / "uploads" / customer_id
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / stored_name
    destination.write_bytes(data)

    return StoredFile(
        path=destination,
        stored_name=stored_name,
        original_name=_safe_display_name(original_name),
        content_type=content_type,
        size_bytes=len(data),
    )


def count_pdf_pages(path: Path) -> int:
    """Page count for a PDF, or 1 for an image. 0 when it cannot be read."""
    if path.suffix.casefold() != ".pdf":
        return 1
    try:
        import pypdfium2

        document = pypdfium2.PdfDocument(str(path))
        try:
            return len(document)
        finally:
            document.close()
    except Exception:
        return 0


def result_path(customer_id: str, job_id: str) -> Path:
    folder = get_settings().storage_path / "results" / customer_id / job_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder
