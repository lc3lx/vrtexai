"""Understanding a printed table well enough to trust the values under its headings.

Every customer's invoice is a different shape. There is no template to match, so
nothing here is allowed to assume one: the grid is rebuilt exactly as printed,
and each column's meaning is argued for from evidence the document itself
carries — what the heading says, what is actually in the cells, and whether the
arithmetic between columns holds.

Three failures this replaces, each of which produced *correct headings above
wrong values* — the worst kind of output, because it looks right:

* **Merged cells were ignored.** The old reader collected ``<td>`` elements in
  order and never read ``colspan`` or ``rowspan``. One merged cell makes the row
  short, and every value after it slides a column to the left while the last
  falls off the end. Real invoices merge constantly: a totals label spanning the
  description columns, a two-line heading, a description spanning two rows.
* **Totals rows were read as line items.** ``Total | | | 820.00`` printed inside
  the item grid became an item whose description was "Total" and whose quantity
  was 820. That poisons the arithmetic check and loses the actual total.
* **Column roles were assigned first-match-wins down a fixed list.** "Product
  ID" claimed the description role from "Product Name" because the word
  "product" appeared in it; "Inward Quantity" claimed the quantity role from
  "Quantity In Stock", which is the column that actually multiplies. A single
  ordered pass cannot weigh two candidates against each other, and weighing them
  is the whole problem.

The functions here are pure and take plain strings, so every shape is testable
without a model, an image, or PaddleOCR.
"""
from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

# A cell is written by the tag that opens it; the closing tag is optional
# because transcribing models leave it out. Scanning open tags and taking the
# text up to the next one parses both the well-formed and the sloppy case.
_ROW = re.compile(r"<tr[^>]*>(.*?)(?:</tr>|(?=<tr[^>]*>)|$)", re.I | re.S)
_CELL_OPEN = re.compile(r"<(t[dh])([^>]*?)/?>", re.I)
_CELL_CLOSE = re.compile(r"</t[dh]\s*>\s*$", re.I)
_SPAN = re.compile(r"\b(col|row)span\s*=\s*[\"']?\s*(\d+)", re.I)
_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"<br\s*/?>|</p>|</div>", re.I)

# A table is not allowed to grow without bound: a runaway parse on a malformed
# page would otherwise build a grid of thousands of empty columns.
MAX_COLUMNS = 60
MAX_SPAN = 60


@dataclass
class Cell:
    """One position in the grid, after merges have been resolved."""

    text: str = ""
    col_span: int = 1
    row_span: int = 1
    # This position is covered by a merge that began elsewhere. Kept rather than
    # flattened away because it is the evidence that a row is "one label across
    # the table and one amount" — which is what a totals row looks like.
    spanned: bool = False
    header: bool = False

    @property
    def filled(self) -> bool:
        return bool(self.text.strip())


@dataclass
class Grid:
    """A rectangular table. Every row has the same number of cells."""

    cells: list[list[Cell]] = field(default_factory=list)

    @property
    def width(self) -> int:
        return len(self.cells[0]) if self.cells else 0

    @property
    def height(self) -> int:
        return len(self.cells)

    def text_rows(self) -> list[list[str]]:
        return [[cell.text for cell in row] for row in self.cells]

    def __bool__(self) -> bool:
        return bool(self.cells)


# --------------------------------------------------------------------------
# Rebuilding the grid
# --------------------------------------------------------------------------
def _cell_text(chunk: str) -> str:
    """The visible text of one cell, line breaks and all.

    A break inside a cell is kept as one: an exporter's address is printed on
    four lines inside a single box, and flattening it to "M/S HOME DECOR NEAR
    ALISHAN PALACE MANSOOR COLONY SAHARANPUR 247001 UTTAR PRADESH INDIA" turns a
    readable block into a smear. Excel wraps on the break, so the cell reads the
    way the box does.
    """
    text = _BREAK.sub("\n", chunk)
    text = _TAG.sub(" ", text)
    # Entities are decoded here. A company printed as "Bags &amp; Cases" reached
    # the workbook with the escape still in it.
    text = html_module.unescape(text)
    text = re.sub(r"[^\S\n]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def _span_of(attributes: str, name: str) -> int:
    for kind, value in _SPAN.findall(attributes or ""):
        if kind.casefold() == name:
            try:
                return max(1, min(MAX_SPAN, int(value)))
            except ValueError:
                return 1
    return 1


def _cells_in(row_html: str) -> list[tuple[str, str, str]]:
    """(tag, attributes, text) for every cell opened in this row."""
    opens = list(_CELL_OPEN.finditer(row_html))
    found: list[tuple[str, str, str]] = []
    for position, match in enumerate(opens):
        start = match.end()
        end = opens[position + 1].start() if position + 1 < len(opens) else len(row_html)
        chunk = _CELL_CLOSE.sub("", row_html[start:end])
        found.append((match.group(1).casefold(), match.group(2), _cell_text(chunk)))
    return found


def parse_html_table(source: str) -> Grid:
    """Rebuild a printed table from HTML, merges and all.

    Merged cells are resolved in the two different ways the two directions
    actually mean:

    * A cell spanning **columns** keeps its text in the first column it covers
      and leaves the rest empty. "Total" written across three columns is one
      label, not three, and copying it sideways would make a totals row look
      like a row of text.
    * A cell spanning **rows** repeats its text down the rows it covers. A
      description merged over two rows describes both of them, and the second
      row would otherwise arrive with no description at all.

    Either way the position is marked :attr:`Cell.spanned`, so a later pass can
    still see where the merge was.
    """
    rows: list[list[Cell]] = []
    # column -> (cell, rows still to fill)
    carried: dict[int, tuple[Cell, int]] = {}

    for row_html in _ROW.findall(source or ""):
        placed: dict[int, Cell] = {}
        for column, (cell, remaining) in sorted(carried.items()):
            placed[column] = Cell(
                text=cell.text, col_span=cell.col_span, row_span=cell.row_span,
                spanned=True, header=cell.header,
            )
            for extra in range(1, cell.col_span):
                placed[column + extra] = Cell(spanned=True, header=cell.header)
            if remaining > 1:
                carried[column] = (cell, remaining - 1)
            else:
                carried.pop(column, None)

        column = 0
        for tag, attributes, text in _cells_in(row_html):
            while column in placed:
                column += 1
            if column >= MAX_COLUMNS:
                break
            col_span = _span_of(attributes, "col")
            row_span = _span_of(attributes, "row")
            cell = Cell(text=text, col_span=col_span, row_span=row_span, header=tag == "th")
            placed[column] = cell
            for extra in range(1, col_span):
                placed[column + extra] = Cell(spanned=True, header=cell.header)
            if row_span > 1:
                carried[column] = (cell, row_span - 1)
            column += col_span

        if placed:
            width = min(max(placed) + 1, MAX_COLUMNS)
            rows.append([placed.get(index) or Cell() for index in range(width)])

    if not rows:
        return Grid()

    # Rectangular, so that a column index means the same thing on every row —
    # which is the property the whole rest of this module depends on.
    width = min(max(len(row) for row in rows), MAX_COLUMNS)
    for row in rows:
        del row[width:]
        row.extend(Cell() for _ in range(width - len(row)))
    return Grid([row for row in rows if any(cell.filled for cell in row)])


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------
def as_number(text: Any) -> float | None:
    """A number found anywhere in the text. Use for a labelled amount."""
    from verify import to_number

    return to_number(str(text or ""))


# A cell that is a number and nothing else, give or take a currency mark. The
# distinction matters more than it looks: ``verify.to_number`` finds a number
# anywhere in a string, which is right for pulling an amount out of
# "Subtotal: 820.00" and wrong for asking whether a column is numeric — a
# description reading "SAKAR GAS STONE 1," would otherwise count as the number 1
# and take the whole column with it.
# The currency a Gulf invoice prints in every money cell — "AED 25 000" — is
# part of the decoration, not part of the number. Listed rather than matched as
# "two to four letters", which would read "Dell 5664" as the number 5664 and
# turn a monitor into an amount.
_CODES = (
    "AED|SAR|QAR|KWD|BHD|OMR|JOD|EGP|IQD|LYD|TND|MAD|DZD|SYP|LBP|YER|SDG"
    "|USD|EUR|GBP|CHF|CAD|AUD|JPY|CNY|INR|PKR|TRY|RUB"
)
_MARKS = r"[$€£¥₹﷼]|ر\.?\s?س|د\.?\s?إ|ر\.?\s?ق|د\.?\s?ك|ج\.?\s?م|د\.?\s?أ|د\.?\s?ب|ر\.?\s?ع"
_WORDS = r"درهم|ريال|دينار|جنيه|ليرة|دولار|يورو"
_CURRENCY = rf"(?:{_CODES}|{_MARKS}|{_WORDS})"

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_MONEY = re.compile(
    rf"^[\s(\[]*(?:{_CURRENCY}\s*)?[-+]?\s*"
    rf"(\d[\d,٬\s]*(?:[.٫]\d+)?)"
    rf"\s*(?:{_CURRENCY})?\s*%?\s*[)\]]*$",
    re.I,
)


def _grouped_properly(digits: str) -> bool:
    """Are the separators in this figure thousands separators?

    A phone number printed "222 555 7777" is not two-billion-something. The
    difference is the grouping: a thousands separator always leaves groups of
    exactly three, and 7777 is four. Getting this wrong cost far more than a
    misformatted cell — the phone counted as a figure, which made the recipient
    box look like a table with data in it, which let its three label rows be
    eaten as a stacked heading. One misread cell collapsed the whole block.
    """
    groups = re.split(r"[,٬\s]", digits)
    if len(groups) == 1:
        return True
    if not 1 <= len(groups[0]) <= 3:
        return False
    if all(len(group) == 3 for group in groups[1:]):
        return True
    # South Asian grouping: thousands, then pairs — 12,34,567 is twelve lakh
    # thirty-four thousand. Allowed because the product reads invoices from
    # exporters who write amounts that way, and refusing it would turn every one
    # of their figures into text.
    return len(groups[-1]) == 3 and all(len(group) == 2 for group in groups[1:-1])


def numeric_cell(text: Any) -> float | None:
    """The value of a cell that holds a number and nothing else.

    Deliberately stricter than ``verify.to_number``, which finds a number
    anywhere in a string — right for pulling an amount out of "Subtotal: 820.00"
    and wrong for deciding whether a column is numeric: a description reading
    "SAKAR GAS STONE 1," would otherwise count as the number 1 and take its
    whole column with it.

    A currency the page prints beside the figure is allowed, because a column of
    "AED 25000" is a column of money however it is written. Without that, every
    money column on a Gulf invoice measured as non-numeric, no column could be
    the line total, and the sheet came back with no prices and no totals.
    """
    body = str(text or "").strip().translate(_ARABIC_DIGITS)
    if not body:
        return None
    match = _MONEY.match(body)
    if not match:
        return None
    figure = match.group(1)
    whole = re.split(r"[.٫]", figure)[0]
    if not _grouped_properly(whole):
        return None
    try:
        return float(re.sub(r"[,٬\s]", "", figure).replace("٫", "."))
    except ValueError:
        return None


_CURRENCY_IN_CELL = re.compile(rf"({_CURRENCY})", re.I)
_SYMBOL_CODES = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "﷼": "SAR"}


def currency_of(texts: Sequence[Any]) -> str:
    """The currency the figures themselves are printed in.

    Read off the cells rather than hunted for in the page's prose. A manifest
    that prints "$25,000" in every amount has said what its currency is more
    plainly than any sentence could, and taking it from there is what puts the
    sign back beside the numbers in the workbook.
    """
    counts: dict[str, int] = {}
    for text in texts:
        match = _CURRENCY_IN_CELL.search(str(text or ""))
        if not match:
            continue
        mark = match.group(1).strip()
        code = _SYMBOL_CODES.get(mark, mark.upper())
        counts[code] = counts.get(code, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda entry: entry[1])[0]


def _matrix(rows: Sequence[Sequence[str]]) -> list[list[float | None]]:
    return [[numeric_cell(value) for value in row] for row in rows]


def _close(left: float, right: float, tolerance: float = 0.02) -> bool:
    return abs(left - right) <= max(tolerance, abs(right) * 0.005)


def agreement(values: Sequence[Sequence[float | None]], qty: int, price: int, total: int) -> float:
    """Fraction of rows where quantity x price really is the line total."""
    checked = matched = 0
    for row in values:
        if max(qty, price, total) >= len(row):
            continue
        a, b, c = row[qty], row[price], row[total]
        if a is None or b is None or c is None:
            continue
        checked += 1
        if _close(a * b, c):
            matched += 1
    return matched / checked if checked else 0.0


# --------------------------------------------------------------------------
# The header band
# --------------------------------------------------------------------------
def _header_texts(row: Sequence[Cell]) -> list[str]:
    """A header row's labels, with a group heading spread over what it covers.

    The opposite of the body rule: "Charges" printed across three sub-columns
    belongs to all three, because the reader needs "Charges Freight" and
    "Charges Insurance" to tell them apart.
    """
    texts = [""] * len(row)
    for index, cell in enumerate(row):
        if not cell.filled or cell.spanned:
            continue
        for extra in range(cell.col_span):
            if index + extra < len(texts):
                texts[index + extra] = cell.text
    return texts


# A column name is short. Past this a cell is a paragraph — an exporter's
# address block, a declaration — and a row of those is the document's contents,
# not a heading over them. "Description Artistic, wooden iron & handicrafts" is
# a real heading at 46 characters, so the line is drawn well clear of it.
MAX_HEADING_LENGTH = 60


def looks_like_header(row: Sequence[Cell]) -> bool:
    """Is this row naming the columns rather than carrying data?"""
    filled = [cell for cell in row if cell.filled]
    if len(filled) < 2:
        # A caption sitting above the grid must not be eaten as column names.
        return False
    if any(len(cell.text) > MAX_HEADING_LENGTH or "\n" in cell.text for cell in filled):
        return False
    if any(cell.header for cell in filled):
        return True
    return not any(numeric_cell(cell.text) is not None for cell in filled)


def split_header(grid: Grid, limit: int = 3) -> tuple[list[str], list[list[Cell]]]:
    """Column names and the rows underneath them.

    The header band can be more than one row. Invoices stack a heading over two
    lines to keep the column narrow — "Inward" above "Quantity", "Quantity In"
    above "Stock" — and reading only the first line gives every such column the
    same useless name.
    """
    # A table with no figures anywhere is not a data table with a heading — it
    # is a box of label/value pairs, which is how invoices print the customer
    # and payment details. Reading its first three rows as a stacked heading
    # collapsed the whole box into one row of column names.
    if not any(numeric_cell(cell.text) is not None for row in grid.cells for cell in row):
        return [], list(grid.cells)

    band = 0
    while band < min(limit, grid.height) and looks_like_header(grid.cells[band]):
        band += 1
    # The band may not swallow the table. And a heading only stacks over more
    # than one row when real data follows it, so a run of text rows above the
    # figures is not folded into the column names.
    if band >= grid.height:
        return [], list(grid.cells)
    while band > 1 and not any(
        numeric_cell(cell.text) is not None for cell in grid.cells[band]
    ):
        band -= 1
    if band == 0:
        return [], list(grid.cells)

    width = grid.width
    parts: list[list[str]] = [[] for _ in range(width)]
    for row in grid.cells[:band]:
        for index, text in enumerate(_header_texts(row)):
            # A group heading repeated over its sub-columns is written once.
            if text and text not in parts[index]:
                parts[index].append(text)
    return _unique([" ".join(part).strip() for part in parts]), list(grid.cells[band:])


def _unique(headings: Sequence[str]) -> list[str]:
    """Column names no two of which are the same.

    Two columns both headed "Amount" would otherwise become one key in the item
    object, and the second would silently overwrite the first.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for index, heading in enumerate(headings):
        name = (heading or "").strip() or f"column_{index + 1}"
        count = seen.get(name.casefold(), 0) + 1
        seen[name.casefold()] = count
        out.append(name if count == 1 else f"{name} ({count})")
    return out


# --------------------------------------------------------------------------
# What each row is
# --------------------------------------------------------------------------
ITEM = "item"
TOTAL = "total"
SECTION = "section"
BLANK = "blank"


_PAID = re.compile(r"\bpaid\b|\bpayment\b|المدفوع|المسدد", re.I)


def totals_label(text: Any) -> str | None:
    """The totals field a printed label names, or ``None``.

    One vocabulary, used both to keep a totals row out of the item list and to
    read it afterwards — so the two can never disagree about what "Paid" is.
    """
    from geometry import total_field

    label = re.sub(r"[:：]\s*$", "", str(text or "")).strip()
    if not label or len(label) > 40:
        return None
    if _PAID.search(label):
        return "amount_paid"
    return total_field(label)


def classify_row(row: Sequence[Cell]) -> tuple[str, str, float | None]:
    """``(kind, label, amount)`` for one body row.

    A totals row printed inside the item grid is the case that matters. It is
    recognised by shape rather than by position: a label that a totals pattern
    claims, one amount, and nothing else — usually with the label merged across
    the columns the descriptions occupy.
    """
    filled = [(index, cell) for index, cell in enumerate(row) if cell.filled]
    if not filled:
        return BLANK, "", None
    if len(filled) == 1:
        _index, cell = filled[0]
        if numeric_cell(cell.text) is None and (cell.col_span > 1 or len(row) > 2):
            return SECTION, cell.text, None
        return ITEM, "", None

    values = row_amounts(row)
    words = [cell for _index, cell in filled if numeric_cell(cell.text) is None]
    # One label the totals vocabulary claims, and nothing else but figures. The
    # count of figures is deliberately not fixed at one: an invoice's "مجموع"
    # line carries the quantity total, the taxable total and the amount total,
    # each under its own column, and demanding a single amount made that row an
    # eleventh product.
    if len(words) == 1 and values and totals_label(words[0].text) is not None:
        return TOTAL, words[0].text, values[0][1] if len(values) == 1 else None
    return ITEM, "", None


def row_amounts(row: Sequence[Cell]) -> list[tuple[int, float]]:
    """Every figure in a row, with the column it sits in."""
    found: list[tuple[int, float]] = []
    for index, cell in enumerate(row):
        if not cell.filled:
            continue
        value = numeric_cell(cell.text)
        if value is not None:
            found.append((index, value))
    return found


# --------------------------------------------------------------------------
# What each column means
# --------------------------------------------------------------------------
#
# Every rule below is weighed against every other. Nothing short-circuits, so a
# heading is never able to claim a role simply by being first on the row.
def _rules(pairs: list[tuple[str, float]]) -> list[tuple[re.Pattern[str], float]]:
    return [(re.compile(pattern, re.I), weight) for pattern, weight in pairs]


# A heading scores highest when it says exactly which role it is. "Unit price"
# outranks "price", which outranks nothing at all — so a table carrying both
# "Price" and "Unit Price" resolves the way a reader would resolve it.
_NAME_RULES: dict[str, list[tuple[re.Pattern[str], float]]] = {
    "qty": _rules([
        (r"\b(?:qty|quantity|units?|pieces?|pcs|ctns?|cartons?|count)\b", 3.0),
        (r"الكمي[ةه]|كمي[ةه]|العدد|عدد\b|القطع|الوحدات", 3.0),
        (r"\bno\.?\s*of\b|\bnos\b", 2.0),
    ]),
    "unit_price": _rules([
        (r"\bunit\s*(?:price|cost|rate|value)\b|\bprice\s*(?:per|/)\s*unit\b", 4.0),
        (r"سعر\s*(?:ال)?وحد[ةه]|السعر\s*الإفرادي|سعر\s*القطع[ةه]", 4.0),
        (r"\b(?:price|rate|cost|tariff)\b", 2.0),
        (r"\bسعر\b|السعر", 2.0),
    ]),
    "line_total": _rules([
        (r"\b(?:line\s*total|total\s*(?:price|amount|value|cost)|net\s*amount)\b", 4.0),
        (r"إجمالي\s*(?:السعر|المبلغ|البند|الصنف)|القيم[ةه]\s*الإجمالي[ةه]", 4.0),
        (r"\b(?:amount|total|value|sum)\b", 2.5),
        (r"الإجمالي|إجمالي|المجموع|القيم[ةه]|المبلغ", 2.5),
    ]),
    "description": _rules([
        (r"\b(?:description|desc|particulars?|details?|commodity)\b", 4.0),
        (r"الوصف|البيان|التفاصيل|بيان\b|وصف\s*(?:البضاع[ةه]|الصنف)", 4.0),
        (r"\b(?:item|product|goods|service|article|material)s?\s*(?:name|title)?\b", 3.0),
        (r"الصنف|الماد[ةه]|البضاع[ةه]|اسم\s*(?:الصنف|المنتج|الماد[ةه])", 3.0),
    ]),
    "sku": _rules([
        (r"\b(?:sku|code|hs\s*code|part\s*(?:no|number)|item\s*(?:no|code|id)|barcode)\b", 4.0),
        (r"رمز\s*(?:الصنف|المنتج)|كود|الرمز|رقم\s*الصنف", 4.0),
    ]),
    "unit": _rules([
        (r"\b(?:uom|unit\s*of\s*measure|units?)\b", 3.0),
        (r"الوحد[ةه]|وحد[ةه]\s*القياس", 3.0),
    ]),
    "date": _rules([
        (r"\bdates?\b", 3.0),
        (r"التاريخ|تاريخ", 3.0),
    ]),
    "discount": _rules([
        (r"\b(?:discount|disc\.?|rebate)\b", 4.0),
        (r"الخصم|خصم", 4.0),
    ]),
    "tax": _rules([
        (r"\b(?:vat|tax|gst)\b", 4.0),
        (r"الضريب[ةه]|ضريب[ةه]|القيم[ةه]\s*المضاف[ةه]", 4.0),
    ]),
}

# Digits that identify rather than measure. A heading like this is strong
# evidence *against* the arithmetic roles: a tracking number is not a quantity,
# however numeric its column looks.
_IDENTIFIER = re.compile(
    r"\b(?:no\.?|num|number|id|code|ref|serial|barcode|awb|iban|phone|tel|zip)\b"
    r"|رقم|كود|مرجع|بوليص[ةه]|هاتف|جوال",
    re.I,
)

_ARITHMETIC_ROLES = ("qty", "unit_price", "line_total")
_SECONDARY_ROLES = ("sku", "unit", "date", "discount", "tax")

# What the evidence has to add up to before a role is claimed at all. Below it
# the column is left unnamed, which costs a formula and keeps the data honest.
_FLOOR = 1.5


@dataclass
class ColumnStats:
    filled: int = 0
    numeric: float = 0.0
    integer: float = 0.0
    two_decimals: float = 0.0
    median: float = 0.0
    length: float = 0.0
    unique: float = 0.0


def column_stats(rows: Sequence[Sequence[str]], index: int) -> ColumnStats:
    """What is actually in a column, independent of what it is called."""
    texts = [
        str(row[index]).strip() for row in rows
        if index < len(row) and str(row[index]).strip()
    ]
    if not texts:
        return ColumnStats()
    numbers = [numeric_cell(text) for text in texts]
    parsed = [value for value in numbers if value is not None]
    ordered = sorted(parsed)
    return ColumnStats(
        filled=len(texts),
        numeric=len(parsed) / len(texts),
        integer=(
            sum(1 for value in parsed if float(value).is_integer()) / len(parsed)
            if parsed else 0.0
        ),
        two_decimals=(
            sum(1 for text in texts if re.search(r"[.٫]\d{2}\b", text)) / len(texts)
        ),
        median=ordered[len(ordered) // 2] if ordered else 0.0,
        length=sum(len(text) for text in texts) / len(texts),
        unique=len(set(texts)) / len(texts),
    )


def _name_score(heading: str, role: str) -> float:
    text = str(heading or "")
    if not text.strip():
        return 0.0
    best = max((weight for pattern, weight in _NAME_RULES[role] if pattern.search(text)),
               default=0.0)
    if _IDENTIFIER.search(text):
        if role in _ARITHMETIC_ROLES or role == "description":
            best -= 3.0
        elif role == "sku":
            best += 2.0
    return best


def _content_score(stats: ColumnStats, role: str) -> float:
    if not stats.filled:
        return -2.0
    score = 0.0
    if role == "qty":
        score += 1.5 if stats.numeric >= 0.7 else -2.5
        score += 1.0 if stats.integer >= 0.8 else -0.5
        # A quantity is a small number. Six figures in this column means it is
        # an amount, an identifier or a weight in grams.
        score += 0.8 if 0 < stats.median < 10_000 else -0.8
    elif role in {"unit_price", "line_total", "discount", "tax"}:
        score += 1.5 if stats.numeric >= 0.7 else -2.5
        score += 0.8 if stats.two_decimals >= 0.4 else 0.0
    elif role == "description":
        score += 2.0 if stats.numeric <= 0.2 else -2.5
        score += 1.0 if stats.length >= 6 else -0.5
        score += 0.5 if stats.unique >= 0.6 else 0.0
    elif role == "sku":
        score += 1.0 if stats.unique >= 0.8 else -0.5
        score += 0.5 if stats.length <= 16 else -0.5
    elif role == "unit":
        score += 1.0 if stats.unique <= 0.4 and stats.length <= 8 else -1.0
    elif role == "date":
        score += 1.0 if stats.length >= 6 else -1.0
    return score


@dataclass
class Roles:
    """Which column plays which part, and how sure we are."""

    columns: dict[str, int] = field(default_factory=dict)
    agreement: float = 0.0
    notes: list[str] = field(default_factory=list)

    def role_list(self, width: int) -> list[str]:
        roles = ["other"] * width
        for role, index in self.columns.items():
            if 0 <= index < width:
                roles[index] = role
        return roles


def assign_roles(
    headings: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    totals: dict[str, float] | None = None,
) -> Roles:
    """Decide what each column is, from the document's own evidence.

    Three independent kinds of evidence, weighed together rather than tried in
    order:

    * **the heading**, scored by how specifically it names a role;
    * **the content**, so a column of long unique text is the description even
      when nothing is printed above it, and a column of six-figure integers is
      not a quantity whatever it is called;
    * **the arithmetic**, which settles the cases the first two cannot — two
      columns both called "Quantity" are separated by which one actually
      multiplies the price into the total.

    The last one is also the guard against inventing structure. A trio of
    numeric columns is accepted only when their arithmetic holds or their
    headings genuinely name them; a spreadsheet of nine unrelated numeric
    columns therefore resolves to nothing at all, which is the honest answer.
    """
    width = max((len(row) for row in rows), default=0)
    width = max(width, len(headings))
    if width == 0:
        return Roles()

    names = list(headings) + [""] * (width - len(headings))
    stats = [column_stats(rows, index) for index in range(width)]
    values = _matrix(rows)
    base = {
        role: [_name_score(names[index], role) + _content_score(stats[index], role)
               for index in range(width)]
        for role in list(_NAME_RULES)
    }
    named = {
        role: [_name_score(names[index], role) for index in range(width)]
        for role in list(_NAME_RULES)
    }

    found = Roles()
    numeric = [index for index in range(width) if stats[index].numeric >= 0.5]
    # Keep the search small on a wide sheet: only the columns with any claim to
    # a numeric role can take part in a triple.
    if len(numeric) > 8:
        numeric = sorted(
            numeric,
            key=lambda index: max(base[role][index] for role in _ARITHMETIC_ROLES),
            reverse=True,
        )[:8]

    def column_sum(index: int) -> float:
        return sum(row[index] for row in values
                   if index < len(row) and row[index] is not None)

    sums = {index: column_sum(index) for index in numeric}
    stated = [amount for key, amount in (totals or {}).items()
              if key in {"subtotal", "grand_total"} and amount]

    def sums_to_a_stated_total(index: int) -> bool:
        """Does this column add up to a total printed elsewhere on the page?

        The evidence that rescues a services invoice: no quantity, no unit
        price, nothing to multiply — but the column of amounts still adds up to
        the subtotal, and that is what says it is the line total.
        """
        return any(_close(sums.get(index, 0.0), amount, tolerance=0.05) for amount in stated)

    # ---- the arithmetic trio --------------------------------------------
    best: tuple[float, tuple[int, int, int], float] | None = None
    for qty in numeric:
        for price in numeric:
            if price == qty:
                continue
            for total in numeric:
                if total in {qty, price}:
                    continue
                holds = agreement(values, qty, price, total)
                score = (
                    base["qty"][qty] + base["unit_price"][price] + base["line_total"][total]
                    + 6.0 * holds
                    + (3.0 if sums_to_a_stated_total(total) else 0.0)
                )
                # Numbers alone never make a trio. Either the arithmetic holds,
                # or the headings say what these columns are — otherwise this is
                # three unrelated numeric columns being wired together.
                trusted = holds >= 0.6 or all(
                    named[role][index] > 0
                    for role, index in (("qty", qty), ("unit_price", price), ("line_total", total))
                )
                if trusted and (best is None or score > best[0]):
                    best = (score, (qty, price, total), holds)

    if best is not None:
        _score, (qty, price, total), holds = best
        found.columns.update({"qty": qty, "unit_price": price, "line_total": total})
        found.agreement = holds
        found.notes.append(
            f"roles: qty={qty} price={price} total={total} agreement={holds:.2f}"
        )
    else:
        # No trio. A line total on its own is still worth having: it is what the
        # subtotal is checked against, and what the customer sums in Excel.
        candidates = [
            (base["line_total"][index] + (4.0 if sums_to_a_stated_total(index) else 0.0), index)
            for index in numeric
            if named["line_total"][index] > 0 or sums_to_a_stated_total(index)
        ]
        if candidates:
            score, index = max(candidates)
            if score >= _FLOOR:
                found.columns["line_total"] = index
                found.notes.append(f"roles: total={index} (no qty x price relation)")

    # ---- description -----------------------------------------------------
    taken = set(found.columns.values())
    description = [
        (base["description"][index], index)
        for index in range(width)
        if index not in taken and base["description"][index] >= _FLOOR
    ]
    if description:
        found.columns["description"] = max(description)[1]
        taken.add(found.columns["description"])

    # ---- everything else -------------------------------------------------
    for role in _SECONDARY_ROLES:
        options = [
            (named[role][index] + _content_score(stats[index], role), index)
            for index in range(width)
            if index not in taken and named[role][index] > 0
        ]
        if not options:
            continue
        score, index = max(options)
        if score >= _FLOOR:
            found.columns[role] = index
            taken.add(index)
    return found


# --------------------------------------------------------------------------
# Joining what belongs together
# --------------------------------------------------------------------------
def is_totals_grid(grid: Grid) -> bool:
    """A small table that is nothing *but* labelled amounts.

    Invoices often print the totals in their own bordered box beside or below
    the items. Read as an item table it contributes four nonsense rows; read for
    what it is, it supplies the subtotal, the tax and the total.

    Every row has to be a totals row, not merely most of them. The two mistakes
    are not equally bad: a totals box mistaken for an ordinary table still has
    its label/value pairs read out of it further down, while an item grid
    mistaken for a totals box loses every product on the invoice. A short
    receipt whose items happen to be outnumbered by its totals is exactly that
    shape, so the test is the strict one.
    """
    if not grid or grid.height > 8:
        return False
    kinds = [classify_row(row)[0] for row in grid.cells if any(cell.filled for cell in row)]
    return bool(kinds) and all(kind == TOTAL for kind in kinds)


def continues(first: Grid, second: Grid) -> bool:
    """Is the second table the rest of the first one?

    Layout detectors split a long item grid at a page break or a rule, and the
    old reader kept only the larger half — which is how a whole column of prices
    and totals could disappear from the workbook without a word. The join is
    only made on evidence: the same number of columns, and no header of its own
    (or the same one).
    """
    if not first or not second or first.width != second.width:
        return False
    if not looks_like_header(second.cells[0]):
        return True
    return _header_texts(second.cells[0]) == _header_texts(first.cells[0])


# What a table on the page turned out to be.
ITEMS_GRID = "items"
MORE_GRID = "more"      # a continuation of the item grid
TOTALS_GRID = "totals"
OTHER_GRID = "other"


def grid_kinds(grids: Sequence[Grid]) -> list[str]:
    """What each table on the page is, one answer per table, in page order.

    Returned separately from :func:`assemble` so a caller that is reproducing
    the page — rather than extracting from it — can still say where each table
    belongs without re-deciding any of this for itself.

    Order is the only geometry available: the transcribing path rebuilds blocks
    from HTML and carries no coordinates, so tables are joined by structure and
    document order rather than by where they sit on the page.
    """
    kinds = [OTHER_GRID] * len(grids)
    usable = [index for index, grid in enumerate(grids) if grid and grid.height]
    for index in usable:
        if is_totals_grid(grids[index]):
            kinds[index] = TOTALS_GRID
    rest = [index for index in usable if kinds[index] != TOTALS_GRID]
    if not rest:
        return kinds

    # Compared by position, never by value: two tables with identical contents
    # are still two tables, and identity is the only thing that says which.
    chosen = max(rest, key=lambda index: (grids[index].height, grids[index].width))
    kinds[chosen] = ITEMS_GRID
    for index in rest:
        if index > chosen and continues(grids[chosen], grids[index]):
            kinds[index] = MORE_GRID
    return kinds


def assemble(grids: Sequence[Grid]) -> tuple[Grid, list[Grid], list[Grid]]:
    """Sort the page's tables into ``(items, totals, other)``."""
    kinds = grid_kinds(grids)
    items = Grid()
    totals: list[Grid] = []
    others: list[Grid] = []
    for index, grid in enumerate(grids):
        kind = kinds[index]
        if kind == ITEMS_GRID:
            items = Grid([list(row) for row in grid.cells])
        elif kind == TOTALS_GRID:
            totals.append(grid)
        elif kind == OTHER_GRID and grid and grid.height:
            others.append(grid)
    for index, grid in enumerate(grids):
        if kinds[index] == MORE_GRID:
            body = grid.cells[1:] if looks_like_header(grid.cells[0]) else grid.cells
            items.cells.extend(list(row) for row in body)
    return items, totals, others


_MAX_LABEL = 40


def _is_label(cell: Cell) -> bool:
    """Short text with no figure in it — the way a page writes a field name."""
    text = cell.text.strip()
    return bool(text) and len(text) <= _MAX_LABEL and numeric_cell(text) is None


def field_pairs(grid: Grid) -> tuple[list[tuple[str, str]], list[str]]:
    """The ``label, value`` a box of details states, and the text left over.

    Boxes are laid out three ways and the difference matters, because getting
    it wrong pairs one field's name with another field's value:

    * **A cell carrying its own "label: value".** Two boxes printed side by side
      come back as one two-column table, so pairing the row's cells joined the
      left box's label to the right box's value.
    * **A row of names over a row of values.** Shipping documents print
      "Pre-Carriage By | Place of receipt | Country of Origin" and the answers
      underneath. Read row by row that is two labels paired together —
      "Port of Discharge" recorded as meaning "Place of Delivery". Only applied
      to rows of three or more, because a two-cell row is a pair already and
      reading it this way would marry each label to the next row's.
    * **A row that is simply a label and its value.**
    """
    pairs: list[tuple[str, str]] = []
    leftover: list[str] = []
    rows = grid.cells
    index = 0
    while index < len(rows):
        filled = [(column, cell) for column, cell in enumerate(rows[index]) if cell.filled]
        if not filled:
            index += 1
            continue

        split = [_split_labelled(cell.text) for _column, cell in filled]
        if any(split):
            for (_column, cell), pair in zip(filled, split):
                if pair is not None:
                    pairs.append(pair)
                else:
                    leftover.append(cell.text)
            index += 1
            continue

        below = rows[index + 1] if index + 1 < len(rows) else None
        if (
            len(filled) >= 3
            and below is not None
            and all(_is_label(cell) for _column, cell in filled)
            and all(below[column].filled for column, _cell in filled if column < len(below))
        ):
            for column, cell in filled:
                if column < len(below):
                    pairs.append((cell.text, below[column].text.strip()))
            index += 2
            continue

        if len(filled) == 2:
            pairs.append((filled[0][1].text, filled[1][1].text))
        elif len(filled) > 2 and _is_label(filled[-2][1]):
            # A block of address on the left, a field on the right — the shape
            # every export invoice uses for its exporter box.
            pairs.append((filled[-2][1].text, filled[-1][1].text))
            leftover.extend(cell.text for _column, cell in filled[:-2])
        else:
            leftover.extend(cell.text for _column, cell in filled)
        index += 1
    return pairs, leftover


def _split_labelled(text: str) -> tuple[str, str] | None:
    """``label: value`` inside one cell, or ``None``."""
    body = str(text or "").strip()
    if re.search(r"\d\s*[:：]\s*\d", body):     # a clock, not a label
        return None
    match = re.match(r"^([^:：\n]{2,40}?)\s*[:：]\s*(.+)$", body, re.S)
    if not match:
        return None
    label, value = match.group(1).strip(), match.group(2).strip()
    if not label or not value or not re.search(r"[^\W\d_]", label, re.UNICODE):
        return None
    return label, value


def read_totals(grids: Sequence[Grid]) -> list[tuple[str, float]]:
    """Every ``label, amount`` a totals table states, in printed order."""
    found: list[tuple[str, float]] = []
    for grid in grids:
        for row in grid.cells:
            kind, label, amount = classify_row(row)
            if kind == TOTAL and amount is not None:
                found.append((label, amount))
    return found


# --------------------------------------------------------------------------
# Making sense of the totals
# --------------------------------------------------------------------------
def reconcile_totals(stated: Sequence[tuple[str, float]]) -> dict[str, float]:
    """Turn printed ``label: amount`` pairs into the totals the sheet needs.

    The labels overlap, and the overlap matters. A Saudi receipt prints
    ``Total 820`` then ``VAT 123`` then ``Due 943``: taken at face value both
    "Total" and "Due" are the grand total, and whichever is read last wins by
    accident. The arithmetic settles it — 820 + 123 = 943 — so "Total" is the
    subtotal and "Due" is what the customer owes.
    """
    buckets: dict[str, list[float]] = {}
    for label, amount in stated:
        name = totals_label(label)
        # A row whose amounts could not be reduced to one figure arrives with
        # ``None``; it is resolved against the columns instead, by the caller.
        if name and amount is not None:
            buckets.setdefault(name, []).append(amount)

    totals: dict[str, float] = {}
    for name in ("subtotal", "discount", "tax_amount", "amount_paid"):
        if buckets.get(name):
            totals[name] = buckets[name][-1]

    grand = buckets.get("grand_total") or []
    tax = totals.get("tax_amount")
    if len(grand) > 1 and tax is not None:
        # Two candidates and a tax between them: the pair that reconciles tells
        # us which is which, without knowing either label's house style.
        for first in grand:
            for second in grand:
                if first is not second and _close(first + tax, second, tolerance=0.05):
                    totals.setdefault("subtotal", first)
                    totals["grand_total"] = second
                    return totals
    if grand:
        # Otherwise the last one printed is the final figure, and an earlier one
        # is the running subtotal it was built from.
        totals["grand_total"] = grand[-1]
        if len(grand) > 1:
            totals.setdefault("subtotal", grand[0])
    return totals
