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
    """The text of a model's HTML table, as a rectangular grid.

    Merges are resolved rather than ignored — see
    :func:`table_shape.parse_html_table`. Reading ``<td>`` elements in order and
    trusting their count is what put a row's values one column to the left of
    their headings whenever anything on the page was merged, which on a real
    invoice is always.
    """
    from table_shape import parse_html_table

    return parse_html_table(source).text_rows()


_PERCENT_CELL = re.compile(r"^[\d.,٫٬\s]+%$")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _numeric(value: Any) -> float | str | None:
    """A numeric cell as a number.

    Text that will not parse is kept verbatim rather than blanked, so the shape
    gate reports "not a number" and the reviewer sees what was actually printed.
    An empty cell is genuinely absent.

    A cell printed as a percentage becomes the fraction it means, so that the
    workbook can show it back as "5%" and still calculate with it. Storing the
    5 alone loses the sign that it was ever a rate, and a tax column of 5s beside
    a tax column of amounts is a trap for whoever opens the file next.
    """
    from verify import to_number

    text = str(value or "").strip()
    if not text:
        return None
    number = to_number(text)
    if number is not None and _PERCENT_CELL.match(text.translate(_ARABIC_DIGITS)):
        return number / 100.0
    return number if number is not None else text


def _looks_like_header(row: list[str]) -> bool:
    """Whether a row of plain text is naming the columns."""
    from table_shape import Cell, looks_like_header

    return looks_like_header([Cell(text=str(value or "").strip()) for value in row])


def _item_keys(headings: list[str], roles: list[str]) -> list[str]:
    """The key each column takes in an item object.

    A column whose role is known is keyed by the role, because that is what
    ``excel_builder`` and the arithmetic gate look for; anything else keeps the
    heading the document printed, which is what the customer asked for. The
    result is made unique either way — two columns sharing a key would mean the
    second silently overwrote the first.
    """
    keys: list[str] = []
    seen: dict[str, int] = {}
    for index, role in enumerate(roles):
        name = role if role != "other" else (
            headings[index].strip() if index < len(headings) and headings[index].strip()
            else f"column_{index + 1}"
        )
        count = seen.get(name.casefold(), 0) + 1
        seen[name.casefold()] = count
        keys.append(name if count == 1 else f"{name} ({count})")
    return keys


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

# ``Label: value`` inside one run of text. The label has to read like a label:
# at least one letter, and never a digit on either side of the colon — that
# colon belongs to a clock. "Time 3:00 PM" was arriving in the workbook as a
# column headed "Time 3" holding the value "00 PM".
_LABEL_SPLIT = re.compile(r"^([^:：]{2,40}?)\s*[:：]\s*(.+)$")
_CLOCK = re.compile(r"\d\s*[:：]\s*\d")
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def _split_pair(text: str) -> tuple[str, str] | None:
    """``label: value`` out of one run of text, or ``None`` if it is not one."""
    body = str(text or "").strip()
    if _CLOCK.search(body):
        return None
    match = _LABEL_SPLIT.match(body)
    if not match:
        return None
    label, value = match.group(1).strip(), match.group(2).strip()
    if not label or not value or not _HAS_LETTER.search(label):
        return None
    return label, value


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
    from geometry import canonical_field, total_field
    from table_shape import numeric_cell

    pairs: list[tuple[str, str]] = []
    totals: dict[str, float] = {}
    for line in lines:
        for chunk in re.split(r"\s{2,}|\t|\|", line):
            chunk = chunk.strip()
            if _CLOCK.search(chunk):
                continue
            match = _LABEL_SPLIT.match(chunk)
            if not match:
                continue
            label, value = match.group(1).strip(), match.group(2).strip()
            if not label or not value or not _HAS_LETTER.search(label):
                continue
            name = total_field(label)
            if name is not None:
                # Strictly a number, not "a number somewhere in the text": the
                # date in "Due: 2025/11/10" would otherwise be banked as a
                # grand total of 2025.
                amount = numeric_cell(value)
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
    """Turn one PaddleOCR-VL page into the schema the gates expect.

    This function no longer parses anything itself. It collects the page's
    blocks and hands them to :mod:`table_shape`, which rebuilds each table as
    printed — merges resolved, header band folded into one label per column,
    totals rows separated from line items — and then argues each column's role
    from the heading, the content and the arithmetic together.

    The order matters and is deliberate: the totals are read **before** the
    roles, because "this column adds up to the printed subtotal" is often the
    only evidence there is for which column is the line total. A services
    invoice has nothing to multiply.
    """
    import table_shape
    from invoice import extract_invoice_fields

    result = page.get("result") if isinstance(page.get("result"), dict) else page
    markdown = str(page.get("markdown") or "")

    grids: list[table_shape.Grid] = []
    lines: list[str] = []
    diagnostics: list[str] = []
    title = ""

    # The page itself, block by block, in the order it is printed. Kept whole
    # and separately from the interpretation below: what the reader understood
    # about an invoice is one thing, and what is actually on the paper is
    # another, and the customer asked for the paper.
    sections: list[dict[str, Any]] = []

    for block in blocks(result):
        label = _block_label(block)
        content = _block_content(block)
        if not content or label in _SKIP_LABELS:
            continue
        if label in _TABLE_LABELS or "<table" in content.casefold():
            grid = table_shape.parse_html_table(content)
            if grid:
                del grid.cells[MAX_TABLE_ROWS:]
                grids.append(grid)
                sections.append({"kind": "table", "index": len(grids) - 1})
            continue
        if label in _TITLE_LABELS:
            heading = re.sub(r"\s+", " ", content).strip()
            if not title:
                title = heading
            sections.append({"kind": "title", "text": heading})
            continue
        block_lines = [line.strip() for line in content.splitlines() if line.strip()]
        lines.extend(block_lines)
        if block_lines:
            sections.append({"kind": "text", "lines": block_lines})

    if not grids and not lines and markdown:
        # Older or degraded results only carry markdown; keep the text rather
        # than reporting an empty page.
        lines = [line.strip() for line in markdown.splitlines() if line.strip()]
        if lines:
            sections.append({"kind": "text", "lines": list(lines)})

    # ---- the tables ------------------------------------------------------
    kinds = table_shape.grid_kinds(grids)
    grid, totals_grids, other_grids = table_shape.assemble(grids)
    if len(grids) > 1:
        diagnostics.append(
            f"tables: {len(grids)} read, {grid.height} item rows, "
            f"{len(totals_grids)} totals, {len(other_grids)} other"
        )

    headings, body = table_shape.split_header(grid) if grid else ([], [])
    item_rows: list[list[table_shape.Cell]] = []
    stated_totals: list[tuple[str, float]] = []
    # A totals line that carries one figure per column — "مجموع | 20 | 144,400 |
    # 148,060" — cannot be read until the columns have meanings, so it waits.
    column_totals: list[tuple[str, list[table_shape.Cell]]] = []
    for row in body:
        kind, label, amount = table_shape.classify_row(row)
        if kind == table_shape.TOTAL:
            # A totals line printed inside the item grid. Counted as an item it
            # became a product called "Total" whose quantity was the amount due.
            if amount is not None:
                stated_totals.append((label, amount))
            else:
                column_totals.append((label, list(row)))
        elif kind == table_shape.ITEM:
            item_rows.append(row)
    stated_totals.extend(table_shape.read_totals(totals_grids))

    # ---- the page's other tables and text --------------------------------
    # A table that is neither the item grid nor a totals box still carries page
    # detail. A two-column one is a list of label/value pairs — that is what
    # invoices use for "Invoice No", "VAT", "Total" — so it is kept as pairs
    # rather than pasted into one string.
    side_fields: list[tuple[str, str]] = []
    unplaced = 0
    for other in other_grids:
        for row in other.cells:
            cells = [cell.text.strip() for cell in row if cell.filled]
            if not cells:
                continue
            # A cell that already carries its own "label: value" is a field in
            # its own right. Invoices print the customer box and the payment box
            # side by side, and the reader returns them as one two-column table —
            # so pairing the two cells of a row produced fields like
            # "اسم: أجهزة كمبيوتر الأسمنت" = "أيام: 15", which is one box's label
            # against the other box's value.
            pairs = [_split_pair(text) for text in cells]
            if any(pairs):
                for text, pair in zip(cells, pairs):
                    if pair is not None:
                        side_fields.append(pair)
                    else:
                        lines.append(text)
                continue
            if len(cells) == 2:
                side_fields.append((cells[0], cells[1]))
            else:
                # A wider table that is neither the item grid nor a totals box.
                # Its cells still go into the page text, but they no longer have
                # columns, so say so: this is the one path by which a reading can
                # still lose structure, and a silent loss is what made the last
                # missing price column so hard to explain.
                lines.extend(cells)
                unplaced += len(cells)
    if unplaced:
        diagnostics.append(f"{unplaced} cells from another table were read as text, not columns")

    header = extract_invoice_fields(lines)
    currency = header.pop("currency", "")
    # What the tables state outranks what a pattern found in running text: the
    # table said which amount belongs to which label, and the text was guessed
    # at. Anything the tables did not mention is still filled in from the lines.
    totals = table_shape.reconcile_totals(stated_totals)
    for key, amount in _totals_from_lines(lines).items():
        totals.setdefault(key, amount)
    for key in ("subtotal", "tax_amount", "grand_total"):
        if key in header:
            header.pop(key, None)

    # ---- what each column means -----------------------------------------
    columns: list[str] = []
    roles: list[str] = []
    items: list[dict[str, Any]] = []
    item_totals: list[dict[str, Any]] = []
    if grid:
        texts = [[cell.text for cell in row] for row in item_rows]
        found = table_shape.assign_roles(headings, texts, totals=totals)
        width = max(grid.width, len(headings))
        roles = found.role_list(width)
        columns = list(headings) + [
            f"column_{index + 1}" for index in range(len(headings), width)
        ]
        diagnostics.extend(found.notes)
        if "line_total" not in found.columns:
            diagnostics.append("column roles could not be resolved from this table")

        # Now that the columns mean something, the figure a totals row printed
        # under the amounts column is the document's total.
        total_column = found.columns.get("line_total")
        keys = _item_keys(columns, roles)
        for label, cells in column_totals:
            amounts = table_shape.row_amounts(cells)
            name = table_shape.totals_label(label) or "subtotal"
            under = next(
                (value for index, value in amounts if index == total_column), None
            )
            if under is None and amounts:
                # No amounts column to sit under: the last figure on the line is
                # the one a reader's eye lands on as the total.
                under = amounts[-1][1]
            if under is not None:
                totals.setdefault(name, under)
            # Kept cell by cell as well as read: the page prints this line as
            # the last row of its table, and a workbook that reproduces the page
            # has to show it there rather than only bank its figures.
            item_totals.append({
                "label": label,
                "values": {
                    key: cells[index].text if index < len(cells) else ""
                    for index, key in enumerate(keys)
                },
            })

        for row in item_rows:
            item: dict[str, Any] = {}
            for index, key in enumerate(keys):
                value = row[index].text if index < len(row) else ""
                item[key] = _numeric(value) if roles[index] in _NUMERIC_ROLES else value
            if any(str(value).strip() for value in item.values() if value is not None):
                items.append(item)

    # ---- the page, resolved ----------------------------------------------
    # Each table section now knows what it turned out to be. The item grid is
    # written from the verified items so it keeps its numbers and its formulas;
    # everything else is written as it was printed.
    resolved: list[dict[str, Any]] = []
    for section in sections:
        if section.get("kind") != "table":
            resolved.append(section)
            continue
        index = int(section.get("index", -1))
        kind = kinds[index] if 0 <= index < len(kinds) else table_shape.OTHER_GRID
        if kind == table_shape.ITEMS_GRID:
            resolved.append({"kind": "items"})
        elif kind == table_shape.MORE_GRID:
            # Its rows are already part of the item table above.
            continue
        elif kind == table_shape.TOTALS_GRID:
            resolved.append({"kind": "totals"})
        else:
            other = grids[index]
            other_headings, other_body = table_shape.split_header(other)
            resolved.append({
                "kind": "table",
                "columns": other_headings,
                "rows": [[cell.text for cell in row] for row in other_body],
            })
    # A totals block the page printed inside the item grid rather than in a box
    # of its own still has to appear, and it belongs under the items.
    if stated_totals and not any(s.get("kind") == "totals" for s in resolved):
        position = next(
            (number + 1 for number, s in enumerate(resolved) if s.get("kind") == "items"),
            len(resolved),
        )
        resolved.insert(position, {"kind": "totals"})
    sections = resolved

    # A two-column table already says which value belongs to which label, so it
    # is believed over anything a pattern guessed from running text.
    from geometry import canonical_field, total_field
    from table_shape import numeric_cell

    for label, value in side_fields:
        clean_label = re.sub(r"[:：]\s*$", "", label).strip()
        total_name = total_field(clean_label)
        if total_name is not None:
            amount = numeric_cell(value)
            if amount is not None:
                # Behind the tables that were read as totals: those had their
                # labels reconciled against each other, and this one did not.
                totals.setdefault(total_name, amount)
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
        printed = str(header.pop(key) or "").strip()
        currency = currency or printed

    return {
        "document_type": "invoice" if (items and totals) else ("table" if items else "other"),
        "direction": _direction(" ".join(lines) + " " + title),
        "currency": currency,
        "title": title,
        "header": header,
        "columns": columns,
        "column_roles": roles,
        "items": items,
        # The table's own footing rows, as printed, to be written under it.
        "item_totals": item_totals,
        "totals": totals,
        "notes": lines,
        # The page as printed, block by block, so the workbook can reproduce it
        # rather than only report what was understood about it.
        "sections": sections,
        # How the table was read, in the reader's own words. Carried through the
        # gates as advisory notes so a wrong column on a customer's invoice can
        # be diagnosed from the job's warnings instead of by guesswork.
        "diagnostics": diagnostics,
    }
