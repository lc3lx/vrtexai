"""Show how a page was read, without running the model.

Every customer sends a differently shaped invoice, and the failures that matter
are quiet ones: a value under the wrong heading looks perfectly fine in the
workbook. This prints what the reader actually understood, so the answer to "why
is the price column empty" is a command rather than an afternoon.

Run the job with ``VERTEX_DUMP_PAGES=some/dir`` to save the model's raw answer
for each page, then::

    python table_probe.py some/dir/invoice.p1.json

It also accepts a file of raw HTML, which is what a transcribing model returns —
so a page can be pasted into a file and examined without a job at all.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _blocks_from_html(text: str) -> dict:
    """Raw transcribed HTML, split into the blocks the converter expects.

    Deliberately a local copy of what the web service's adapter does, rather
    than an import of it: this tool ships beside the reader in both trees, and a
    diagnostic that only runs where the web backend happens to sit is no use on
    the machine where the problem is.
    """
    import html as html_module
    import re

    body = re.sub(r"^\s*```(?:html)?|```\s*$", "", text.strip(), flags=re.MULTILINE)
    blocks: list[dict] = []
    position = 0

    def add_text(chunk: str) -> None:
        for paragraph in re.split(r"</?p[^>]*>|<br\s*/?>|\n{2,}", chunk):
            # Runs of spaces are kept: they are how a page separates two fields
            # printed on one line, and the splitter downstream reads them.
            plain = html_module.unescape(re.sub(r"<[^>]+>", " ", paragraph))
            stripped = re.sub(r"[^\S ]+", " ", plain).strip()
            if stripped:
                blocks.append({"block_label": "text", "block_content": stripped})

    for match in re.finditer(r"<table[\s\S]*?</table>", body, re.IGNORECASE):
        add_text(body[position:match.start()])
        blocks.append({"block_label": "table", "block_content": match.group(0)})
        position = match.end()
    add_text(body[position:])
    return {"parsing_res_list": blocks}


def _page_from(path: Path) -> dict:
    """The page payload, whether the file is a saved dump or plain HTML."""
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("{"):
        page = json.loads(text)
        if "pages" in page:
            page = (page["pages"] or [{}])[0]
        return page
    return {"result": _blocks_from_html(text), "markdown": ""}


def probe(path: Path) -> int:
    import paddle_vl
    import table_shape as ts

    page = _page_from(path)
    result = page.get("result") if isinstance(page.get("result"), dict) else page

    grids = [
        ts.parse_html_table(paddle_vl._block_content(block))
        for block in paddle_vl.blocks(result)
        if paddle_vl._block_label(block) in paddle_vl._TABLE_LABELS
        or "<table" in paddle_vl._block_content(block).casefold()
    ]
    grids = [grid for grid in grids if grid]
    print(f"{len(grids)} table(s) read from {path.name}")

    items, totals, others = ts.assemble(grids)
    print(f"  item grid {items.height} x {items.width}, "
          f"{len(totals)} totals table(s), {len(others)} other")

    headings, body = ts.split_header(items) if items else ([], [])
    kinds = [ts.classify_row(row) for row in body]
    rows = [[cell.text for cell in row]
            for row, (kind, *_rest) in zip(body, kinds) if kind == ts.ITEM]

    stated = [(label, amount) for kind, label, amount in kinds
              if kind == ts.TOTAL and amount is not None]
    stated.extend(ts.read_totals(totals))
    reconciled = ts.reconcile_totals(stated)

    found = ts.assign_roles(headings, rows, totals=reconciled)
    named = {index: role for role, index in found.columns.items()}

    print("\ncolumns:")
    for index, heading in enumerate(headings):
        print(f"  {index:>2}  {named.get(index, '-'):<12} {heading}")
    print(f"\nagreement: {found.agreement:.2f}")
    for note in found.notes:
        print(f"  {note}")

    print(f"\nrows: {len(rows)} item, "
          f"{sum(1 for kind, *_ in kinds if kind == ts.TOTAL)} total, "
          f"{sum(1 for kind, *_ in kinds if kind == ts.SECTION)} section")
    for row in rows[:8]:
        print("  " + " | ".join(row))
    if len(rows) > 8:
        print(f"  … {len(rows) - 8} more")

    print(f"\ntotals printed: {stated}")
    print(f"totals read:    {reconciled}")

    payload = paddle_vl.to_payload(page)
    print(f"\nheader fields:  {payload['header']}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    return probe(Path(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
