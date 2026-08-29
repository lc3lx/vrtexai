"""PaddleOCR-VL engine seam: run the model, turn its page structure into the
payload :mod:`ai_extract` validates.

Two responsibilities, kept apart on purpose:

* **Running it.** PaddleOCR 3.x cannot share an interpreter with the 2.9 install
  the geometric reader needs, so the model runs in :mod:`vl_worker` under its own
  virtual environment. That child is *long-lived*: loading the model costs about
  two and a half minutes on a CPU, so one host serves every page, retry and file
  in a batch. See :func:`_start_server` and :func:`shutdown`.
* **Converting it.** PaddleOCR-VL reports *structure* — layout blocks and HTML
  table grids — not invoice semantics. It does not say which column is a
  quantity or what the supplier is called. Those come from code that already
  exists and is already tested: :func:`verify.resolve_roles` infers the column
  roles from whether the table's own arithmetic holds, and
  :func:`invoice.extract_invoice_fields` pulls header fields by pattern.
  Deciding roles from arithmetic beats asking a model, because a wrong guess
  shows up immediately as a column that does not multiply.

The conversion is pure and takes plain dictionaries, so it is fully testable
without PaddleOCR 3.x installed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

# Only a safety net against a wedged model, never a performance budget. Pages
# now go to the model at full resolution, so a large scan legitimately takes far
# longer than the ~10 minutes measured on a downscaled one, and a customer's
# machine may be slower still. Tripping this early would abandon a page that was
# working, so it sits far above any expected cost.
WORKER_TIMEOUT = 7200.0
MAX_TABLE_ROWS = 400

# Roles whose cells are numbers on the page and must reach the gates as numbers.
# A reader returns pixels as text, so the conversion happens here, where the
# roles are known — not in the gates, which would otherwise reject every cell of
# every document for "arriving as a string".
_NUMERIC_ROLES = {"qty", "unit_price", "line_total", "discount", "tax"}

_TABLE_LABELS = {"table"}
_TITLE_LABELS = {"doc_title", "title", "paragraph_title", "chart_title"}
_SKIP_LABELS = {"image", "figure", "chart", "formula", "seal", "header_image", "aside_text"}


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------
def packages_root() -> Path | None:
    """The virtual environment holding PaddleOCR 3.x.

    A venv rather than a second directory on ``sys.path``: installing this tree
    with ``pip --target`` leaves overlapping namespace packages half-extracted
    (``modelscope`` and ``modelscope-hub`` claim the same folder), and the
    failure only surfaces when the pipeline is constructed. A venv layered over
    the bundled runtime resolves the precedence properly and still shares the
    heavy wheels — paddlepaddle, numpy, OpenCV — with the base install.
    """
    value = (os.environ.get("VERTEX_VL_PACKAGES") or "").strip()
    if value and Path(value).is_dir():
        return Path(value)
    bundled = Path(__file__).resolve().parent.parent / "runtime" / "vl_env"
    return bundled if bundled.is_dir() else None


def worker_python() -> Path | None:
    """The interpreter the VL model runs under, or None when not installed."""
    root = packages_root()
    if root is None:
        return None
    for candidate in (root / "Scripts" / "python.exe", root / "bin" / "python"):
        if candidate.is_file():
            _repair_venv_home(root)
            return candidate
    return None


def _repair_venv_home(root: Path) -> None:
    """Point ``pyvenv.cfg`` at wherever the runtime actually lives now.

    A virtual environment records its base interpreter as an absolute path, set
    when it was created on the build machine. The customer installs somewhere
    else entirely, which would leave the venv pointing at a directory that does
    not exist on their disk. The launcher itself is relocatable; only this one
    line is not, so it is rewritten in place whenever it has drifted.
    """
    config = root / "pyvenv.cfg"
    runtime = Path(__file__).resolve().parent.parent / "runtime"
    if not config.is_file() or not runtime.is_dir():
        return
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    executables = {"base-executable", "executable"}
    changed = False
    rebuilt: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip().casefold()
        if key in {"home", "base-prefix", "base-exec-prefix"} | executables:
            wanted = str(runtime / "python.exe") if key in executables else str(runtime)
            if line.split("=", 1)[-1].strip() != wanted:
                line = f"{line.split('=', 1)[0].rstrip()} = {wanted}"
                changed = True
        rebuilt.append(line)
    if not changed:
        return
    try:
        config.write_text("\n".join(rebuilt) + "\n", encoding="utf-8")
    except OSError as error:
        # Silence here would strand the customer: the venv would keep pointing
        # at a directory that exists only on the build machine, and the failure
        # would surface much later as an unexplained import error.
        raise RuntimeError(
            f"Could not correct the PaddleOCR-VL environment path ({config}): {error}"
        ) from error


def models_root() -> Path | None:
    value = (os.environ.get("VERTEX_VL_MODELS") or "").strip()
    return Path(value) if value and Path(value).is_dir() else None


def enabled() -> bool:
    return (os.environ.get("VERTEX_AI_EXTRACT") or "").strip().casefold() != "off"


def available() -> tuple[bool, str]:
    """(usable, detail) — same shape as ``vision.available``."""
    if not enabled():
        return False, "disabled by VERTEX_AI_EXTRACT=off"
    if worker_python() is None:
        return False, "PaddleOCR-VL components are not bundled in this build."
    models = models_root()
    if models is None or not any(models.rglob("*")):
        return False, "PaddleOCR-VL weights are not downloaded — run first-time setup."
    return True, f"PaddleOCR-VL @ {models}"


# --------------------------------------------------------------------------
# Running the model
# --------------------------------------------------------------------------
def _condition(image: Any, variant: str) -> Any:
    """The page exactly as it was read. Nothing is resampled.

    The model is given full resolution on purpose. Downscaling costs less time
    but throws away the small print — stamps, tax numbers, dense table rows —
    which is precisely the detail this path exists to recover, so the trade is
    refused and the model is left to work on every pixel that was captured.

    ``variant`` is kept so the retry ladder still has a shape, but no variant
    alters the image: a re-read must be a re-read of the same page.
    """
    return image


def worker_environment() -> dict[str, str]:
    """Environment for the VL child process."""
    environment = dict(os.environ)
    packages = packages_root()
    if packages is not None:
        environment["VERTEX_VL_PACKAGES"] = str(packages)
    models = (os.environ.get("VERTEX_VL_MODELS") or "").strip()
    if models:
        environment["VERTEX_VL_MODELS"] = models
    # The child must not inherit the paths that put PaddleOCR 2.9 first, nor a
    # PYTHONHOME pinned to the base runtime — either one defeats the venv.
    for key in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(key, None)
    return environment


_SERVER: subprocess.Popen | None = None


def _start_server() -> subprocess.Popen:
    """Launch the model host and wait for it to report ready.

    Loading PaddleOCR-VL costs roughly two and a half minutes on a CPU, so the
    process is started once and reused for every page, every retry and every
    file in the batch. Paying that per page instead would dwarf the reading.
    """
    worker = Path(__file__).resolve().parent / "vl_worker.py"
    if not worker.is_file():
        raise RuntimeError("The PaddleOCR-VL worker script is missing.")
    interpreter = worker_python()
    if interpreter is None:
        raise RuntimeError("The PaddleOCR-VL environment is not installed in this build.")
    from common import emit

    emit("Loading the local PaddleOCR-VL model (once per batch)…")
    process = subprocess.Popen(
        [str(interpreter), str(worker), "--serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=worker_environment(),
    )
    # The host emits progress lines while loading and a ready line at the end.
    while True:
        line = process.stdout.readline() if process.stdout else ""
        if not line:
            raise RuntimeError("The PaddleOCR-VL engine could not be started.")
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if payload.get("fraction") == 1.0 or payload.get("ok") is not None:
            return process


def _server() -> subprocess.Popen:
    global _SERVER
    if _SERVER is not None and _SERVER.poll() is None:
        return _SERVER
    _SERVER = _start_server()
    return _SERVER


def shutdown() -> None:
    """Stop the model host. Safe to call when it was never started."""
    global _SERVER
    process, _SERVER = _SERVER, None
    if process is None or process.poll() is not None:
        return
    try:
        if process.stdin:
            process.stdin.write("\n")
            process.stdin.flush()
            process.stdin.close()
        process.wait(timeout=20)
    except Exception:
        try:
            process.kill()
        except OSError:
            pass


def _read_line(process: subprocess.Popen, timeout: float) -> str:
    """One reply line, or TimeoutError.

    A pipe read cannot be given a deadline directly on Windows, so it happens on
    a worker thread the caller can stop waiting on.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(process.stdout.readline)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as error:
            raise TimeoutError from error
    finally:
        # Never wait: on timeout the reader is still blocked on the pipe, and
        # only killing the child releases it. The caller does that.
        pool.shutdown(wait=False)


def _run_worker(image_path: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="vertex-vl-") as workspace:
        destination = Path(workspace) / "page.json"
        process = _server()
        request = json.dumps({"image": str(image_path), "out": str(destination)})
        try:
            process.stdin.write(request + "\n")
            process.stdin.flush()
            line = _read_line(process, WORKER_TIMEOUT)
        except TimeoutError as error:
            # A wedged model would otherwise hang the whole batch with no way
            # out but the task manager.
            shutdown()
            raise RuntimeError("The PaddleOCR-VL engine timed out reading the page.") from error
        except (BrokenPipeError, OSError) as error:
            shutdown()
            raise RuntimeError("The PaddleOCR-VL engine stopped while reading.") from error
        if not line:
            shutdown()
            raise RuntimeError("The PaddleOCR-VL engine stopped without answering.")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise RuntimeError(f"The PaddleOCR-VL engine failed: {reply.get('error') or 'no detail given'}")
        if not destination.is_file():
            raise RuntimeError("The PaddleOCR-VL engine wrote no result.")
        return json.loads(destination.read_text(encoding="utf-8"))


def read_page(image: Any, variant: str = "raw") -> dict[str, Any]:
    """Read one page image and return the payload the gates validate."""
    from PIL import Image

    conditioned = _condition(image, variant)
    with tempfile.TemporaryDirectory(prefix="vertex-vlimg-") as workspace:
        page_path = Path(workspace) / "page.png"
        Image.fromarray(conditioned).save(page_path, format="PNG")
        raw = _run_worker(page_path)
    pages = raw.get("pages") or []
    if not pages:
        raise RuntimeError("The PaddleOCR-VL engine returned no page.")
    return to_payload(pages[0])


# --------------------------------------------------------------------------
# Converting the structure
# --------------------------------------------------------------------------
def blocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Layout blocks in reading order, whatever the pipeline called them."""
    for key in ("parsing_res_list", "layout_parsing_result", "parsing_result"):
        value = result.get(key)
        if isinstance(value, list) and value:
            return [item for item in value if isinstance(item, dict)]
    return []


def _block_label(block: dict[str, Any]) -> str:
    for key in ("block_label", "label", "type"):
        value = block.get(key)
        if value:
            return str(value).strip().casefold()
    return ""


def _block_content(block: dict[str, Any]) -> str:
    for key in ("block_content", "content", "text", "html"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def html_rows(source: str) -> list[list[str]]:
    """Rows from the model's HTML table.

    The tag stripping is the parser the PP-Structure path already relies on;
    entity decoding is added here. The model emits real HTML, so a company
    named "Bags &amp; Cases" reaches the sheet with the escape still in it —
    observed on a real document, where cells read ``Handbags &amp; Leather``
    and ``Ladies&#x27; Garments``.
    """
    import html as html_module

    from invoice_ai import _html_table_to_rows

    return [
        [html_module.unescape(cell) for cell in row]
        for row in _html_table_to_rows(source)
    ]


def _numeric(value: Any) -> float | str | None:
    """A numeric cell as a number.

    Text that will not parse is kept verbatim rather than blanked, so the shape
    gate reports "not a number" and the reviewer sees what was actually printed.
    An empty cell is genuinely absent.
    """
    from verify import to_number

    text = str(value or "").strip()
    if not text:
        return None
    number = to_number(text)
    return number if number is not None else text


def _cell(text: str) -> dict[str, Any]:
    # The VL model reports no per-cell confidence, so cells start trusted and
    # are demoted only by the gates in ai_extract.
    return {"text": str(text or "").strip(), "conf": 100.0, "alternatives": []}


def _looks_like_header(row: list[str]) -> bool:
    from verify import to_number

    filled = [value for value in row if str(value).strip()]
    if len(filled) < 2:
        return False
    return not any(to_number(value) is not None for value in filled)


def _pick_table(tables: list[list[list[str]]]) -> list[list[str]]:
    """The item grid is the widest, tallest table on the page."""
    if not tables:
        return []
    return max(tables, key=lambda rows: (len(rows), max((len(row) for row in rows), default=0)))


def _roles_for(columns: list[str], rows: list[list[dict[str, Any]]]) -> list[str]:
    """Map each column index to a role name using the arithmetic-backed resolver."""
    from verify import resolve_roles

    width = max((len(row) for row in rows), default=len(columns))
    resolved = resolve_roles(columns, rows)
    roles = ["other"] * max(width, len(columns))
    for role in ("description", "qty", "unit_price", "line_total"):
        index = resolved.get(role)
        if isinstance(index, int) and 0 <= index < len(roles):
            roles[index] = role
    return roles


def _direction(text: str) -> str:
    """Sheet direction for a page that is very often bilingual.

    A plain majority vote gets this wrong on exactly the documents this product
    sees: a UAE licence or a Gulf tax invoice prints every field twice, and the
    English half is longer because Latin words need more letters than the Arabic
    words beside them. Counting letters therefore reports "English" for a form
    an Arabic speaker reads right-to-left.

    So Arabic only has to be *present in earnest* — a fifth of the letters — to
    win. Below that the page is genuinely English with an odd Arabic stamp on it.
    """
    arabic = len(re.findall(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    letters = arabic + latin
    if letters == 0:
        return "ltr"
    return "rtl" if arabic / letters >= 0.20 else "ltr"


# A party printed as a heading with its details underneath and no colon between
# them — which is how nearly every shipping document sets out its shipper and
# consignee boxes.
_PARTY_HEADING = re.compile(
    r"^(shipper|consignor|sender|consignee|receiver|recipient|ship\s*to|deliver\s*to"
    r"|bill\s*to|sold\s*to|المرسل\s*إليه|المرسل|الشاحن|المستلم|المورد|العميل)"
    r"\s*(?:details|information|info|بيانات)?\s*[:：]?$",
    re.I,
)

# A line that is a field in its own right, and so the end of the block above it.
_LABELLED_LINE = re.compile(r"[:：]\s*\S")

# ``Label: value`` inside one run of text.
_LABEL_SPLIT = re.compile(r"^([^:：]{2,40}?)\s*[:：]\s*(.+)$")


def labelled_fields(
    lines: list[str],
) -> tuple[list[tuple[str, str]], dict[str, float]]:
    """``Label: value`` printed in the page's running text, as fields and totals.

    :func:`geometry.label_value_pairs` does this properly, from the word boxes,
    and it is what the geometric reader uses. The VL model returns text with no
    coordinates, so the same job is done on the text alone — the page separates
    fields printed side by side with a run of white space, which is the one
    signal that survives into a line of characters.

    Splitting the label off first is what makes the amounts reachable. The
    totals patterns are written to match a label, not a sentence: ``^total$``
    recognises the word "Total" and cannot see it inside "Total: 900.00", so a
    plainly printed grand total went unread until the two were separated.
    """
    from geometry import canonical_field, to_number, total_field

    pairs: list[tuple[str, str]] = []
    totals: dict[str, float] = {}
    for line in lines:
        for chunk in re.split(r"\s{2,}|\t|\|", line):
            match = _LABEL_SPLIT.match(chunk.strip())
            if not match:
                continue
            label, value = match.group(1).strip(), match.group(2).strip()
            if not label or not value:
                continue
            name = total_field(label)
            if name is not None:
                amount = to_number(value)
                if amount is not None:
                    totals.setdefault(name, amount)
                continue
            pairs.append((canonical_field(label) or label, value))
    return pairs, totals


def party_blocks(lines: list[str], limit: int = 4) -> list[tuple[str, str]]:
    """Address blocks that sit under a bare heading, as ``(field, value)``.

    The label/value reader needs a colon to pair two pieces of text, and a
    shipping document does not print one: it prints ``Shipper`` in bold and the
    name, street and phone underneath. That was invisible to every pattern here
    and survived only because the loose page text used to be dumped under the
    table. With that section gone the block would be lost outright, so it is
    read properly instead — the heading names the field, and the lines beneath
    it are its value, up to the next heading or the next labelled line.
    """
    from geometry import canonical_field, total_field

    pairs: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        match = _PARTY_HEADING.match(line.strip())
        if not match:
            continue
        field = canonical_field(match.group(1))
        if field is None:
            continue
        collected: list[str] = []
        for follower in lines[index + 1: index + 1 + limit]:
            text = follower.strip()
            if (
                not text
                or _PARTY_HEADING.match(text)
                or _LABELLED_LINE.search(text)
                or total_field(text) is not None
            ):
                break
            collected.append(text)
        if collected:
            pairs.append((field, " / ".join(collected)))
    return pairs


def _totals_from_lines(lines: list[str]) -> dict[str, float]:
    """Labelled amounts printed outside the grid (Subtotal / VAT / Total).

    The labels genuinely overlap — ``TOTAL_LABEL`` matches the ``الإجمالي`` inside
    ``الإجمالي قبل الضريبة`` — so a line claimed by the more specific label is
    removed from the pool before the broader one is tried. Without that, the
    subtotal line gets read a second time as the grand total and the arithmetic
    gate then reports a mismatch that is not in the document.
    """
    from verify import SUBTOTAL_LABEL, TAX_LABEL, TOTAL_LABEL, to_number

    totals: dict[str, float] = {}
    remaining = list(lines)
    for key, pattern in (
        ("subtotal", SUBTOTAL_LABEL),
        ("tax_amount", TAX_LABEL),
        ("grand_total", TOTAL_LABEL),
    ):
        for line in remaining:
            match = pattern.search(line)
            if not match:
                continue
            # The amount is whatever number follows the label on that line.
            value = to_number(line[match.end():])
            if value is None:
                continue
            totals[key] = value
            remaining.remove(line)
            break
    return totals


def to_payload(page: dict[str, Any]) -> dict[str, Any]:
    """Turn one PaddleOCR-VL page into the schema the gates expect."""
    from invoice import extract_invoice_fields

    result = page.get("result") if isinstance(page.get("result"), dict) else page
    markdown = str(page.get("markdown") or "")

    tables: list[list[list[str]]] = []
    lines: list[str] = []
    title = ""
    for block in blocks(result):
        label = _block_label(block)
        content = _block_content(block)
        if not content or label in _SKIP_LABELS:
            continue
        if label in _TABLE_LABELS or "<table" in content.casefold():
            rows = html_rows(content)
            if rows:
                tables.append(rows[:MAX_TABLE_ROWS])
            continue
        if label in _TITLE_LABELS and not title:
            title = re.sub(r"\s+", " ", content).strip()
            continue
        for line in content.splitlines():
            if line.strip():
                lines.append(line.strip())

    if not tables and not lines and markdown:
        # Older or degraded results only carry markdown; keep the text rather
        # than reporting an empty page.
        lines = [line.strip() for line in markdown.splitlines() if line.strip()]

    grid = _pick_table(tables)
    columns: list[str] = []
    items: list[dict[str, Any]] = []
    roles: list[str] = []
    if grid:
        body = grid
        if _looks_like_header(grid[0]):
            columns = [str(value).strip() for value in grid[0]]
            body = grid[1:]
        cell_rows = [[_cell(value) for value in row] for row in body]
        roles = _roles_for(columns, cell_rows)
        for row in body:
            item: dict[str, Any] = {}
            for index, value in enumerate(row):
                role = roles[index] if index < len(roles) else "other"
                key = role if role != "other" else (
                    columns[index].strip() if index < len(columns) and columns[index].strip()
                    else f"column_{index + 1}"
                )
                item[key] = _numeric(value) if role in _NUMERIC_ROLES else value
            if any(str(value).strip() for value in item.values() if value is not None):
                items.append(item)
        if not columns:
            columns = [
                role if role != "other" else f"column_{index + 1}"
                for index, role in enumerate(roles)
            ]

    # Any table that is not the item grid carries page detail, not silently
    # dropped. A two-column one is a list of label/value pairs — that is what
    # invoices use for "Invoice No", "VAT", "Total" — so it is kept as pairs
    # rather than pasted into one string. Cells joined with " | " used to arrive
    # in the workbook as a single unreadable cell, and the field patterns then
    # had to guess where one value ended and the next label began.
    side_fields: list[tuple[str, str]] = []
    for table in tables:
        if table is grid:
            continue
        for row in table:
            cells = [str(value).strip() for value in row if str(value).strip()]
            if not cells:
                continue
            if len(cells) == 2:
                side_fields.append((cells[0], cells[1]))
            else:
                lines.extend(cells)

    header = extract_invoice_fields(lines)
    currency = header.pop("currency", "")
    totals = _totals_from_lines(lines)
    for key in ("subtotal", "tax_amount", "grand_total"):
        if key in header:
            header.pop(key, None)

    # A two-column table already says which value belongs to which label, so it
    # is believed over anything a pattern guessed from running text.
    from geometry import canonical_field, to_number, total_field

    for label, value in side_fields:
        clean_label = re.sub(r"[:：]\s*$", "", label).strip()
        total_name = total_field(clean_label)
        if total_name is not None:
            amount = to_number(value)
            if amount is not None:
                totals[total_name] = amount
            continue
        field = canonical_field(clean_label)
        if field is not None:
            header[field] = value
        elif clean_label and len(clean_label) <= 40:
            header.setdefault(clean_label, value)

    # Then the page's own running text, and last of all the heading blocks. The
    # order is the strength of the evidence: a table that pairs a label with a
    # value beats a label printed beside one, which beats a heading with text
    # underneath it.
    line_fields, line_totals = labelled_fields(lines)
    for field, value in line_fields:
        header.setdefault(field, value)
    for field, value in party_blocks(lines):
        header.setdefault(field, value)
    for name, amount in line_totals.items():
        totals.setdefault(name, amount)

    # The currency is a property of the document, not a column of it. It is
    # popped once above, before the header has been filled from the page text —
    # so a page that prints "Currency: USD" in a line rather than beside a
    # canonical label would otherwise leave the amounts unformatted and put the
    # word in a column of its own.
    for key in [key for key in header if str(key).strip().casefold() == "currency"]:
        # Popped whether or not it is needed: an ``or`` here would short-circuit
        # the pop as soon as a currency had been found and leave the column in.
        found = str(header.pop(key) or "").strip()
        currency = currency or found

    return {
        "document_type": "invoice" if (items and totals) else ("table" if items else "other"),
        "direction": _direction(" ".join(lines) + " " + title),
        "currency": currency,
        "title": title,
        "header": header,
        "columns": columns,
        "column_roles": roles,
        "items": items,
        "totals": totals,
        "notes": lines,
    }
