"""Turn a bank's statement export into the rows the pipeline expects.

    parse_statement(data, filename, statement_id=None)
        -> [{txn_id, statement_id, date, narration, debit, credit, ref}]

Bank-agnostic on purpose. Netbanking downloads (HDFC, ICICI, SBI, Axis, Kotak, ...) differ in
file type, title rows above the table, column names and amount layout, so instead of one
parser per bank:
  1. read the file as grids of cells: CSV/TSV/TXT (delimiter sniffed), XLSX, XLS, or an HTML
     table saved as .xls (some banks do that); every sheet / table is tried
  2. find the header row: the first row that names a date, a narration and amount columns
  3. map columns by synonyms ("Narration" / "Description" / "Particulars" / "Transaction
     Remarks"; "Withdrawal Amt." / "Debit" / "DR"; or one "Amount" column with a Dr/Cr flag)
  4. read rows until the table ends; a row holding only narration text continues the row above
  5. parse "1,23,456.78", "Rs. 500.00 Dr" and day-first dates, and give every row a stable
     txn_id (a hash of its content, so re-importing the same file updates instead of duplicating)

The pipeline's own CSV (txn_id, narration, debit, credit) takes the same path and keeps its
txn_ids. PDF is not supported: download the CSV / Excel version of the statement instead.

Preview a file locally (nothing leaves your machine): python -m ingest path/to/statement.xls
"""
import csv
import hashlib
import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path


class StatementError(ValueError):
    """The file could not be read as a bank statement; the message says why."""


def _norm(cell) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(cell).lower()).strip()


# (field, pattern on the normalised header text). The first matching rule wins for a cell, and
# the left-most column wins for a field (Kotak has two "Dr / Cr" columns: amount, then balance).
HEADER_RULES = [
    ("balance", re.compile(r"\bbalance\b|^bal$")),
    ("drcr", re.compile(r"^(dr cr|cr dr|dr or cr|d c|type|txn type|transaction type|debit credit( dr cr)?)$")),
    ("value_date", re.compile(r"^value (date|dt)$")),
    ("date", re.compile(r"^((txn|tran|transaction|posting|trans) )?(date|dt)$")),
    ("narration", re.compile(r"^(narration|description|particulars|remarks|details|"
                             r"transaction (remarks|details|description|particulars|narration))\b")),
    ("debit", re.compile(r"^(withdrawal|withdrawals|debit|debits|dr)( amt| amount)?( in)?( inr| rs)?$")),
    ("credit", re.compile(r"^(deposit|deposits|credit|credits|cr)( amt| amount)?( in)?( inr| rs)?$")),
    ("amount", re.compile(r"^(transaction |txn )?amount( in)?( inr| rs)?$")),
    ("ref", re.compile(r"\b(chq|cheque|ref|reference|utr|instrument)\b")),
]
STOP_WORDS = re.compile(r"statement summary|opening balance|end of statement|computer generated|"
                        r"generated on|this is a system", re.I)
DATE_FORMATS = ["%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y", "%d %b %Y",
                "%d-%b-%Y", "%d-%b-%y", "%d %b %y", "%d %B %Y", "%d/%b/%Y", "%Y-%m-%d", "%Y/%m/%d"]
AMOUNT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _classify(cell) -> str | None:
    raw = str(cell).strip()
    if raw.lower() in ("txn_id", "statement_id"):  # the pipeline's own CSV
        return raw.lower()
    text = _norm(raw)
    if not text or len(text) > 40:
        return None
    for field, pat in HEADER_RULES:
        if pat.search(text):
            return field
    return None


def _find_header(grid: list[list]) -> tuple[int, dict[str, int]] | None:
    for i, row in enumerate(grid[:80]):
        cols: dict[str, int] = {}
        for j, cell in enumerate(row):
            field = _classify(cell)
            if field and field not in cols:
                cols[field] = j
        if "date" not in cols and "value_date" in cols:
            cols["date"] = cols["value_date"]
        has_amounts = ("debit" in cols and "credit" in cols) or "amount" in cols
        if "date" in cols and "narration" in cols and has_amounts:
            return i, cols
    return None


def parse_date(cell) -> str | None:
    if isinstance(cell, datetime):
        return cell.date().isoformat()
    if isinstance(cell, date):
        return cell.isoformat()
    if isinstance(cell, (int, float)) and not isinstance(cell, bool) and 20000 < cell < 80000:
        return (date(1899, 12, 30) + timedelta(days=int(cell))).isoformat()  # Excel serial date
    s = str(cell).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d", s):
        s = s[:10]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_amount(cell) -> tuple[float | None, str | None]:
    """(value, side) where side is 'dr' / 'cr' when the cell itself says so (suffix or sign)."""
    if cell is None or isinstance(cell, bool):
        return None, None
    if isinstance(cell, (int, float)):
        if cell != cell or cell == 0:  # NaN or zero
            return None, None
        return abs(float(cell)), ("dr" if cell < 0 else None)
    s = str(cell).strip()
    if not s or s.lower() in ("-", "--", "nan", "none", "null"):
        return None, None
    side = "dr" if re.search(r"\bdr\b", s, re.I) else "cr" if re.search(r"\bcr\b", s, re.I) else None
    if side is None and (s.startswith("-") or (s.startswith("(") and s.endswith(")"))):
        side = "dr"
    m = AMOUNT_RE.search(s)
    if not m:
        return None, None
    value = float(m.group(0).replace(",", ""))
    return (value, side) if value else (None, None)


def _side_from_flag(cell) -> str | None:
    t = _norm(cell)
    if t in ("dr", "d", "debit", "withdrawal"):
        return "dr"
    if t in ("cr", "c", "credit", "deposit"):
        return "cr"
    return None


def _clean(cell) -> str:
    if cell is None or (isinstance(cell, float) and cell != cell):
        return ""
    return re.sub(r"\s+", " ", str(cell)).strip()


# ---- reading files into grids -----------------------------------------------------------------
def _frame_to_grid(df) -> list[list]:
    return [[None if (isinstance(v, float) and v != v) else v for v in row] for row in df.itertuples(index=False)]


def _excel_grids(data: bytes, engine: str) -> list[list[list]]:
    import pandas as pd

    sheets = pd.read_excel(io.BytesIO(data), header=None, sheet_name=None, engine=engine)
    return [_frame_to_grid(df) for df in sheets.values()]


def _html_grids(data: bytes) -> list[list[list]]:
    import pandas as pd

    text = data.decode("utf-8", errors="replace")
    return [_frame_to_grid(df) for df in pd.read_html(io.StringIO(text), header=None)]


def _delimited_grid(data: bytes) -> list[list]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    sample = "\n".join(text.splitlines()[:60])
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        delimiter = max(",\t;|", key=sample.count)
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def read_grids(data: bytes, filename: str = "") -> list[list[list]]:
    if not data:
        raise StatementError("the file is empty")
    if data[:4] == b"%PDF":
        raise StatementError("PDF statements are not supported: download the CSV or Excel (XLS/XLSX) "
                             "version of the statement from netbanking")
    try:
        if data[:2] == b"PK":                                # XLSX (a zip archive)
            return _excel_grids(data, "openpyxl")
        if data[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):     # legacy XLS (OLE2)
            return _excel_grids(data, "xlrd")
        head = data[:4096].lstrip().lower()
        if head.startswith(b"<") and (b"<table" in data[:500_000].lower() or b"<html" in head):
            return _html_grids(data)                          # an HTML table saved as .xls
    except StatementError:
        raise
    except Exception as e:
        raise StatementError(f"could not read {Path(filename).name or 'the file'}: {e}") from e
    return [_delimited_grid(data)]                            # CSV / TSV / TXT (SBI's ".xls" is TSV)


# ---- the table ----------------------------------------------------------------------------------
def _statement_id(filename: str) -> str:
    stem = Path(filename or "statement").stem
    return re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:40] or "statement"


def _parse_grid(grid: list[list], header: int, cols: dict[str, int], statement_id: str) -> list[dict]:
    def cell(row, field):
        j = cols.get(field)
        return row[j] if j is not None and j < len(row) else None

    rows: list[dict] = []
    for row in grid[header + 1:]:
        texts = [_clean(c) for c in row]
        if any(STOP_WORDS.search(t) for t in texts if t) and rows:
            break
        day = parse_date(cell(row, "date"))
        narration = _clean(cell(row, "narration"))
        if day is None:
            # Wrapped narration: the only filled cell is the narration column.
            if rows and narration and sum(bool(t) for t in texts) == 1:
                rows[-1]["narration"] = f"{rows[-1]['narration']} {narration}".strip()
            continue
        if "amount" in cols and not ("debit" in cols and "credit" in cols):
            value, side = parse_amount(cell(row, "amount"))
            side = _side_from_flag(cell(row, "drcr")) or side or "dr"
            debit, credit = (value, None) if side == "dr" else (None, value)
        else:
            debit, _ = parse_amount(cell(row, "debit"))
            credit, _ = parse_amount(cell(row, "credit"))
        if debit is None and credit is None:
            continue  # "B/F" opening-balance lines and other rows without money
        rows.append({
            "txn_id": _clean(cell(row, "txn_id")), "statement_id": _clean(cell(row, "statement_id")) or statement_id,
            "date": day, "narration": narration, "debit": debit, "credit": credit, "ref": _clean(cell(row, "ref")),
        })
    return rows


def _stable_ids(rows: list[dict]) -> None:
    seen: dict[str, int] = {}
    for r in rows:
        if r["txn_id"]:
            continue
        key = "|".join(str(r[k] or "") for k in ("date", "narration", "debit", "credit", "ref"))
        seen[key] = seen.get(key, 0) + 1  # identical rows in one file (two same-day chai payments)
        r["txn_id"] = "X" + hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()[:12].upper()


def parse_statement(data: bytes, filename: str = "statement.csv", statement_id: str | None = None) -> list[dict]:
    sid = statement_id or _statement_id(filename)
    for grid in read_grids(data, filename):
        found = _find_header(grid)
        if not found:
            continue
        rows = _parse_grid(grid, *found, statement_id=sid)
        if rows:
            _stable_ids(rows)
            return rows
    raise StatementError("could not find the transaction table: no row names a date column, a narration / "
                         "description column and amount columns (debit + credit, or amount). Supported: CSV, "
                         "XLS and XLSX statement downloads from netbanking.")
