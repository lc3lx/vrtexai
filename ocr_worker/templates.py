"""Persistent vendor layout templates keyed by header fingerprint."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def app_templates_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ExcelCleaner"
    root.mkdir(parents=True, exist_ok=True)
    return root / "templates.json"


def fingerprint(text: str) -> str:
    normalized = " ".join((text or "").casefold().split())[:400]
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def load_templates(*paths: Path) -> list[dict[str, Any]]:
    known: list[dict[str, Any]] = []
    signatures: set[str] = set()
    for path in paths:
        if path is None or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            signature = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if signature not in signatures:
                known.append(item)
                signatures.add(signature)
    return known


def save_templates(output_dir: Path, templates: list[dict[str, Any]]) -> None:
    destinations = [output_dir / "templates.json", app_templates_path()]
    existing = load_templates(*destinations)
    signatures = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in existing}
    for item in templates:
        if not item:
            continue
        signature = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if signature not in signatures:
            existing.append(item)
            signatures.add(signature)
    encoded = json.dumps(existing, ensure_ascii=False, indent=2)
    for path in destinations:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded, encoding="utf-8")
        except OSError:
            continue


def match_template(text: str, templates: list[dict[str, Any]]) -> dict[str, Any] | None:
    key = fingerprint(text)
    for item in templates:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind not in {None, "invoice", "table", "receipt", "tabular", "ocr-lines"}:
            continue
        if item.get("fingerprint") == key:
            return item
    return None


def build_template(source_name: str, doc_type: str, header_text: str, columns: list[str]) -> dict[str, Any]:
    return {
        "source": source_name,
        "type": doc_type,
        "fingerprint": fingerprint(header_text),
        "header_text": " ".join(header_text.split())[:240],
        "columns": columns,
    }
