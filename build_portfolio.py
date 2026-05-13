#!/usr/bin/env python3
"""Build an Apple Numbers portfolio document from a Fidelity brokerage CSV via AppleScript."""

import argparse
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from calendar import month_name as MONTH_NAMES
from datetime import datetime

NUMBERS_DIR = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~Numbers/Documents"
)

MONEY_MARKET_SYMBOLS = {"SPAXX", "FZDXX", "FZROX", "FZILX", "FCASH"}

# Order matters: more-specific rules first (schwab before trust)
ACCOUNT_BUCKETS = [
    ("SCHWAB",    ["schwab"]),
    ("BROKERAGE", ["individual", "trust", "brokerage", "tod"]),
    ("IRA",       ["traditional ira", "rollover ira", "ira bda", "inherited"]),
    ("ROTH",      ["roth"]),
    ("401K",      ["401k", "401 k"]),
    ("CMA",       ["cma", "cash management"]),
]


# ── AppleScript / JXA Runners ──────────────────────────────────────────────────

def run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def run_applescript_file(script: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".applescript",
                                     delete=False, encoding="utf-8") as f:
        f.write(script)
        tmp = f.name
    try:
        result = subprocess.run(
            ["osascript", tmp],
            capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        return result.stdout.strip()
    finally:
        os.unlink(tmp)


def run_jxa_file(script: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js",
                                     delete=False, encoding="utf-8") as f:
        f.write(script)
        tmp = f.name
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", tmp],
            capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        return result.stdout.strip()
    finally:
        os.unlink(tmp)


# ── AppleScript value encoding ─────────────────────────────────────────────────

def _as_str(v: object) -> str:
    """Encode a Python value as a quoted AppleScript string literal."""
    s = str(v) if v is not None else ""
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")
    return f'"{s}"'


def _as_val(v: object) -> str:
    """Encode a Python value as an AppleScript value literal (number or string)."""
    if v is None or v == "":
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(float(v))
    s = str(v)
    if s.startswith("="):
        return '""'  # formula placeholder — written separately
    return _as_str(s)


def _as_row(row: list) -> str:
    """Encode a list as an AppleScript list literal."""
    return "{" + ", ".join(_as_val(v) for v in row) + "}"


# ── Numbers document management ────────────────────────────────────────────────

def list_documents_as() -> list:
    script = '''tell application "Numbers"
  set out to {}
  repeat with d in documents
    set end of out to name of d
  end repeat
  return out
end tell'''
    try:
        raw = run_applescript(script)
        return [n.strip() for n in raw.split(",") if n.strip()] if raw else []
    except RuntimeError:
        return []


def close_document_as(name: str):
    script = f'''tell application "Numbers"
  try
    close document {_as_str(name)} saving no
  end try
end tell'''
    try:
        run_applescript(script)
    except RuntimeError:
        pass


def create_document_as(name: str):
    script = f'''tell application "Numbers"
  make new document with properties {{name: {_as_str(name)}}}
end tell'''
    run_applescript(script)


def list_sheets_as(doc: str) -> list:
    script = f'''tell application "Numbers"
  tell document {_as_str(doc)}
    set out to {{}}
    repeat with s in sheets
      set end of out to name of s
    end repeat
    return out
  end tell
end tell'''
    try:
        raw = run_applescript(script)
        return [n.strip() for n in raw.split(",") if n.strip()] if raw else []
    except RuntimeError:
        return []


def delete_sheet_as(doc: str, sheet_name: str):
    script = f'''tell application "Numbers"
  tell document {_as_str(doc)}
    try
      delete sheet {_as_str(sheet_name)}
    end try
  end tell
end tell'''
    try:
        run_applescript(script)
    except RuntimeError:
        pass


# ── Template Reading (JXA) ─────────────────────────────────────────────────────

def _resolve_template_path(template_doc: str) -> str:
    """Resolve a template doc argument to an absolute .numbers file path."""
    if os.path.isabs(template_doc):
        return template_doc
    if os.path.exists(template_doc):
        return os.path.abspath(template_doc)
    return os.path.join(NUMBERS_DIR, os.path.basename(template_doc))


def _ensure_template_open(template_doc: str):
    """Open the template file in Numbers (idempotent — brings existing window to front)."""
    abs_path = _resolve_template_path(template_doc)
    if not os.path.exists(abs_path):
        sys.exit(f"ERROR: Template file not found at '{abs_path}'")
    subprocess.run(["open", "-a", "Numbers", abs_path])
    time.sleep(3)


def read_template(template_doc: str):
    """Return (template_doc_name, col_map, headers, formula_row, num_cols, col_widths).

    Reads headers (row 1), formula patterns (row 2), and column widths from the
    _template sheet in the template document.
    """
    _ensure_template_open(template_doc)

    jxa = f'''
var app = Application("Numbers");
var doc = null, sheet = null;
var docs = app.documents();
outer: for (var i = 0; i < docs.length; i++) {{
  var sheets = docs[i].sheets();
  for (var j = 0; j < sheets.length; j++) {{
    if (sheets[j].name() === "_template") {{
      doc = docs[i]; sheet = sheets[j]; break outer;
    }}
  }}
}}
if (!doc) throw new Error("No open Numbers document has a _template sheet. Open: " + {json.dumps(template_doc)});

var tables = sheet.tables();
var table = null;
for (var i = 0; i < tables.length; i++) {{
  if (tables[i].name() === "My Portfolio") {{ table = tables[i]; break; }}
}}
if (!table) throw new Error("Table My Portfolio not found in _template");

var colCount = table.columnCount();
var headers = [], formulas = [], widths = [];
for (var c = 0; c < colCount; c++) {{
  var hval = null;
  try {{ hval = table.rows[0].cells[c].value(); }} catch(e) {{}}
  headers.push((hval === null || hval === undefined) ? "" : String(hval));

  var f = null, fval = null;
  try {{ f = table.rows[1].cells[c].formula(); }} catch(e) {{}}
  try {{ fval = table.rows[1].cells[c].value(); }} catch(e) {{}}
  if (f && f.length > 0) {{
    formulas.push(f.charAt(0) === "=" ? f : "=" + f);
  }} else {{
    formulas.push((fval === null || fval === undefined) ? "" : String(fval));
  }}

  var w = null;
  try {{ w = table.columns[c].width(); }} catch(e) {{}}
  widths.push(w);
}}
JSON.stringify({{ docName: doc.name(), colCount: colCount, headers: headers, formulas: formulas, widths: widths }});
'''
    try:
        out = run_jxa_file(jxa)
    except RuntimeError as e:
        sys.exit(f"ERROR: Could not read template '{template_doc}': {e}")

    data = json.loads(out)
    num_cols = data["colCount"]
    template_doc_name = data["docName"]

    def pad(lst):
        lst = [str(v) if v is not None else "" for v in lst]
        return lst + [""] * (num_cols - len(lst))

    headers     = pad(data.get("headers", []))
    formula_row = pad(data.get("formulas", []))
    col_widths  = data.get("widths", [None] * num_cols)

    col_map = {}
    for i, h in enumerate(headers):
        h = str(h).strip()
        if h:
            col_map[h] = i + 1  # 1-based

    formula_row = [_resolve_named_refs(f, col_map) for f in formula_row]

    return template_doc_name, col_map, headers, formula_row, num_cols, col_widths


# ── Bulk Write Helpers ─────────────────────────────────────────────────────────

def _write_single_row_as(doc: str, sheet: str, table: str, actual_row: int, row: list):
    """Write one row: static values + formulas in a single osascript call."""
    stmts = []
    for ci, cell in enumerate(row):
        c = ci + 1
        if isinstance(cell, str) and cell.startswith("="):
            # Pass the full formula string (with "=") as the cell value.
            # Numbers parses strings beginning with "=" as formula expressions.
            f_esc = (cell.replace("\\", "\\\\").replace('"', '\\"')
                        .replace("\n", " ").replace("\r", " "))
            stmts.append(f'set value of cell {c} of row {actual_row} to "{f_esc}"')
        elif cell != "" and cell is not None:
            stmts.append(f"set value of cell {c} of row {actual_row} to {_as_val(cell)}")
    if not stmts:
        return
    body = "\n        ".join(stmts)
    script = f'''tell application "Numbers"
  tell document {_as_str(doc)}
    tell sheet {_as_str(sheet)}
      tell table {_as_str(table)}
        {body}
      end tell
    end tell
  end tell
end tell'''
    run_applescript_file(script)


def _write_rows_as(doc: str, sheet: str, table: str, start_row: int, rows: list):
    """Write rows one at a time to avoid Apple Event connection drops on large batches."""
    for ri, row in enumerate(rows):
        _write_single_row_as(doc, sheet, table, start_row + ri, row)


def _write_tax_rows_as(doc: str, sheet: str, table: str, tax_row: int):
    # Values + formula via AppleScript; number format via JXA (avoids compile-time
    # "bold/format not in dictionary" errors for this Numbers build).
    values_script = f'''tell application "Numbers"
  tell document {_as_str(doc)}
    tell sheet {_as_str(sheet)}
      tell table {_as_str(table)}
        set value of cell 1 of row {tax_row} to "Federal Rate"
        set value of cell 2 of row {tax_row} to 0.1644
        set value of cell 4 of row {tax_row} to "State Rate"
        set value of cell 5 of row {tax_row} to 0.0917
        set value of cell 7 of row {tax_row} to "Total Tax Rate"
        set value of cell 8 of row {tax_row} to "=B{tax_row}+E{tax_row}"
      end tell
    end tell
  end tell
end tell'''
    run_applescript_file(values_script)

    # Format the rate cells as percentage via JXA
    jxa = f'''
var app = Application("Numbers");
var tbl = app.documents[{json.dumps(doc)}].sheets[{json.dumps(sheet)}].tables[{json.dumps(table)}];
var r = {tax_row - 1};  // 0-based
try {{ tbl.rows[r].cells[1].format = "percentage"; }} catch(e) {{}}
try {{ tbl.rows[r].cells[4].format = "percentage"; }} catch(e) {{}}
try {{ tbl.rows[r].cells[7].format = "percentage"; }} catch(e) {{}}
"ok"
'''
    run_jxa_file(jxa)


# ── Template-duplication Sheet Helpers ────────────────────────────────────────

def add_sheet_and_table_as(doc: str, sheet_name: str, table_name: str,
                            num_rows: int, num_cols: int):
    """Create a new sheet containing a single table with the given dimensions."""
    script = f'''tell application "Numbers"
  tell document {_as_str(doc)}
    make new sheet with properties {{name: {_as_str(sheet_name)}}}
    tell sheet {_as_str(sheet_name)}
      set existing_tables to every table
      make new table with properties {{name: {_as_str(table_name)}, row count: {num_rows}, column count: {num_cols}}}
      repeat with t in existing_tables
        try
          delete t
        end try
      end repeat
    end tell
  end tell
end tell'''
    run_applescript_file(script)


def rename_sheet_as(doc: str, old_name: str, new_name: str):
    """Rename a sheet within a document."""
    script = f'''tell application "Numbers"
  tell document {_as_str(doc)}
    set name of sheet {_as_str(old_name)} to {_as_str(new_name)}
  end tell
end tell'''
    run_applescript_file(script)


def setup_output_document(template_path: str, doc_name: str) -> str:
    """Create the output document by copying the template file.

    Returns the actual document name as Numbers sees it (may differ from doc_name
    if Numbers appended a numeric suffix).
    """
    dest = os.path.join(NUMBERS_DIR, f"{doc_name}.numbers")

    # Delete any existing file with the target name so Numbers doesn't suffix it
    pattern = os.path.join(NUMBERS_DIR, glob.escape(doc_name) + "*.numbers")
    for old_file in glob.glob(pattern):
        try:
            os.remove(old_file)
            print(f"  Removed '{os.path.basename(old_file)}'")
        except OSError as e:
            print(f"  WARNING: Could not remove '{old_file}': {e}")

    shutil.copy2(template_path, dest)

    before_set = set(list_documents_as())
    subprocess.run(["open", "-a", "Numbers", dest])

    # Poll until the new document appears
    actual_doc = doc_name
    for _ in range(20):
        time.sleep(0.5)
        after = list_documents_as()
        new = [d for d in after if d not in before_set]
        if new:
            actual_doc = new[0]
            break

    # Numbers may finalise the document name differently (e.g. appends ".numbers").
    # Re-query via JXA to get the exact settled name.
    time.sleep(1)
    confirm_jxa = f'''
var app = Application("Numbers");
var docs = app.documents();
var base = {json.dumps(doc_name)};
var found = {json.dumps(actual_doc)};
for (var i = 0; i < docs.length; i++) {{
  var n = docs[i].name();
  if (n === base || n === base + ".numbers") {{
    found = n; break;
  }}
}}
found;
'''
    try:
        confirmed = run_jxa_file(confirm_jxa).strip()
        if confirmed:
            actual_doc = confirmed
    except RuntimeError:
        pass

    return actual_doc


def _apply_column_formatting_jxa(doc: str, sheet: str, table: str,
                                   col_map: dict, month_headers: list,
                                   is_ira: bool, tot_row: int, col_widths: list):
    """Apply column widths, number formats, and bold to header/totals via JXA.

    Used when a sheet is created blank (not from the template), so formatting
    must be applied explicitly.  Column widths are copied from the template.
    """
    currency_cols = [
        "Price", "Price Change", "Price Paid", "Cost Basis", "Market Value",
        "Gain", "Ann Div", "Next Div Ttl",
    ] + month_headers
    pct_cols = ["% Change", "% Gain", "Gain %", "Div %", "Cost Div %"]
    if is_ira:
        pct_cols.append("% of Portfolio")
    num_cols_list = ["Shares"]

    currency_idxs = [i for h in currency_cols if (i := col_map.get(h))]
    pct_idxs      = [i for h in pct_cols       if (i := col_map.get(h))]
    num_idxs      = [i for h in num_cols_list   if (i := col_map.get(h))]

    widths_js = json.dumps([float(w) if w is not None else 0 for w in col_widths])

    jxa = f'''
var app = Application("Numbers");
var tbl = app.documents[{json.dumps(doc)}].sheets[{json.dumps(sheet)}].tables[{json.dumps(table)}];
var nCols = tbl.columnCount();

// Bold header and totals rows
for (var c = 0; c < nCols; c++) {{
  try {{ tbl.rows[0].cells[c].bold = true; }} catch(e) {{}}
  try {{ tbl.rows[{tot_row - 1}].cells[c].bold = true; }} catch(e) {{}}
}}

// Number formats
var currencyIdxs = {json.dumps(currency_idxs)};
var pctIdxs      = {json.dumps(pct_idxs)};
var numIdxs      = {json.dumps(num_idxs)};
for (var i = 0; i < currencyIdxs.length; i++) {{
  try {{ tbl.columns[currencyIdxs[i] - 1].format = "currency"; }} catch(e) {{}}
}}
for (var i = 0; i < pctIdxs.length; i++) {{
  try {{ tbl.columns[pctIdxs[i] - 1].format = "percentage"; }} catch(e) {{}}
}}
for (var i = 0; i < numIdxs.length; i++) {{
  try {{ tbl.columns[numIdxs[i] - 1].format = "number"; }} catch(e) {{}}
}}

// Column widths from template
var widths = {widths_js};
for (var i = 0; i < widths.length && i < nCols; i++) {{
  if (widths[i] > 0) {{
    try {{ tbl.columns[i].width = widths[i]; }} catch(e) {{}}
  }}
}}

"ok"
'''
    run_jxa_file(jxa)


def clear_data_rows_as(doc: str, sheet: str, table: str,
                        num_cols: int, from_row: int, to_row: int):
    """Clear cell values in rows from_row..to_row (1-based, inclusive). Preserves formatting."""
    jxa = f'''
var app = Application("Numbers");
var tbl = app.documents[{json.dumps(doc)}].sheets[{json.dumps(sheet)}].tables[{json.dumps(table)}];
for (var r = {from_row - 1}; r < {to_row}; r++) {{
  for (var c = 0; c < {num_cols}; c++) {{
    try {{ tbl.rows[r].cells[c].value = ""; }} catch(e) {{}}
  }}
}}
"ok"
'''
    run_jxa_file(jxa)


def resize_table_as(doc: str, sheet: str, table: str, num_rows: int):
    """Set the row count of the given table via JXA (AppleScript row count is read-only)."""
    jxa = f'''
var app = Application("Numbers");
var tbl = app.documents[{json.dumps(doc)}].sheets[{json.dumps(sheet)}].tables[{json.dumps(table)}];
try {{ tbl.rowCount = {num_rows}; }} catch(e) {{}}
"ok"
'''
    run_jxa_file(jxa)


# ── Helpers ────────────────────────────────────────────────────────────────────

def parse_float(s):
    if not s or str(s).strip() in ("--", "-", "N/A", ""):
        return None
    cleaned = re.sub(r"[$%+,\s]", "", str(s))
    try:
        return float(cleaned)
    except ValueError:
        return None


def clean_symbol(sym):
    return re.sub(r"\*+$", "", sym.strip()).strip() if sym else ""


def classify_account(account_name):
    normalized = re.sub(r"[^a-z0-9]+", " ", account_name.lower()).strip()
    for bucket, keywords in ACCOUNT_BUCKETS:
        for kw in keywords:
            if kw in normalized:
                return bucket
    return "OTHER"


def is_tbill(symbol, description):
    if re.match(r"^\d{9}[A-Z]\d$", symbol):
        return True
    lower = description.lower()
    return any(k in lower for k in ("treasury bill", "t-bill", "treas bills", "treas bill"))


def is_cd(description):
    lower = description.lower()
    return "cd " in lower or "certificate of deposit" in lower


def is_money_market(symbol, description):
    return (
        symbol in MONEY_MARKET_SYMBOLS
        or "money market" in description.lower()
        or "held in money market" in description.lower()
    )


def parse_maturity_date(description):
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", description)
    if m:
        try:
            return datetime.strptime(m.group(1), "%m/%d/%Y")
        except ValueError:
            pass
    return None


def col_letter(n):
    """1-based column index → letter(s): 1→A, 27→AA."""
    result = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def _resolve_named_refs(formula: str, col_map: dict) -> str:
    """Convert Numbers internal named column refs like 'Shares 16' → 'G2'.

    Numbers stores formulas using header-name refs ('Shares 16', 'Price 16') where
    the trailing number is an internal table ID, not a column index.  subst_row()
    only handles cell-letter refs like 'G2', so we normalise here after reading the
    template so that all formulas use the portable letter form.
    """
    if not formula or not formula.startswith("="):
        return formula
    header_to_letter = {h: col_letter(i) for h, i in col_map.items() if h and h.strip()}
    # Longest headers first so 'Market Value' matches before 'Value'
    headers_sorted = sorted(header_to_letter.keys(), key=len, reverse=True)
    result = formula
    for header in headers_sorted:
        letter = header_to_letter[header]
        result = re.sub(re.escape(header) + r"\s+\d+", letter + "2", result)
    # Normalise Unicode math operators Numbers uses in formula display
    result = result.replace("×", "*").replace("−", "-").replace("÷", "/")
    return result


def derive_doc_name(csv_path):
    stem = csv_path.replace("\\", "/").split("/")[-1]
    stem = re.sub(r"\.csv$", "", stem, flags=re.IGNORECASE)
    m = re.search(r"([A-Za-z]+)-(\d{2})-(\d{4})$", stem)
    return f"Portfolio {m.group(1)} {m.group(3)}" if m else "Portfolio"


def derive_month_headers(csv_path):
    stem = csv_path.replace("\\", "/").split("/")[-1]
    m = re.search(r"([A-Za-z]+)-\d{2}-(\d{4})", stem)
    if not m:
        return ["Month 1", "Month 2", "Month 3", "Month 4"]
    try:
        month_num = list(MONTH_NAMES).index(m.group(1).capitalize())
    except ValueError:
        return ["Month 1", "Month 2", "Month 3", "Month 4"]
    year = int(m.group(2))
    return [
        f"{MONTH_NAMES[(month_num - 1 + i) % 12 + 1]} {year + (month_num - 1 + i) // 12}"
        for i in range(4)
    ]


def subst_row(formula_row, r):
    """Replace template row-2 column references with actual row r."""
    result = []
    for cell in formula_row:
        if cell and str(cell).startswith("="):
            result.append(re.sub(r"([A-Z]+)2\b", lambda m: f"{m.group(1)}{r}", str(cell)))
        else:
            result.append(cell)
    return result


# ── CSV Parsing ────────────────────────────────────────────────────────────────

def parse_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        lines = f.readlines()

    start = 0
    if lines and not lines[0].strip().startswith("Account Name"):
        start = 1

    reader = csv.DictReader(lines[start:])
    reader.fieldnames = [n.strip() for n in (reader.fieldnames or [])]

    positions = []
    for row in reader:
        row = {k.strip(): (v.strip() if v else "") for k, v in row.items() if k}
        symbol_raw = row.get("Symbol", "").strip()
        symbol = clean_symbol(symbol_raw)
        description = row.get("Description", "").strip()
        account_name = row.get("Account Name", "").strip()

        if not account_name:
            continue

        lower_sym = symbol_raw.lower()
        if lower_sym in ("--", "pending activity") or description.lower() == "pending activity":
            continue

        if not symbol and not description:
            continue

        bucket = classify_account(account_name)
        tbill = is_tbill(symbol, description)
        cd = is_cd(description)
        mm = is_money_market(symbol, description)

        effective_symbol = symbol if symbol else re.sub(r"\s+", "_", description[:20]).upper()

        positions.append({
            "account_name": account_name,
            "bucket": bucket,
            "symbol": effective_symbol,
            "display_symbol": symbol,
            "description": description,
            "quantity": parse_float(row.get("Quantity")) or 0.0,
            "last_price": parse_float(row.get("Last Price")) or 0.0,
            "last_price_change": parse_float(row.get("Last Price Change")) or 0.0,
            "current_value": parse_float(row.get("Current Value")) or 0.0,
            "cost_basis_total": parse_float(row.get("Cost Basis Total")),
            "avg_cost_basis": parse_float(row.get("Average Cost Basis")),
            "is_tbill": tbill,
            "is_cd": cd,
            "is_money_market": mm,
            "maturity_date": parse_maturity_date(description),
        })

    return positions


# ── Position Aggregation ───────────────────────────────────────────────────────

def aggregate_positions(all_positions, buckets):
    by_symbol = {}
    tbills_cds = []

    for pos in all_positions:
        if pos["bucket"] not in buckets:
            continue

        if pos["is_tbill"] or pos["is_cd"]:
            tbills_cds.append(dict(pos))
            continue

        sym = pos["symbol"]
        if sym in by_symbol:
            ex = by_symbol[sym]
            ex["quantity"] += pos["quantity"]
            if pos["cost_basis_total"] is not None:
                ex["cost_basis_total"] = (ex["cost_basis_total"] or 0.0) + pos["cost_basis_total"]
            ex["current_value"] += pos["current_value"]
        else:
            by_symbol[sym] = dict(pos)

    result = []
    for pos in by_symbol.values():
        q = pos["quantity"]
        cb = pos["cost_basis_total"]
        if q and cb:
            pos["avg_cost_basis"] = cb / q
        result.append(pos)

    equities = sorted(
        [p for p in result if not p["is_money_market"]],
        key=lambda p: p["current_value"], reverse=True,
    )
    money_markets = [p for p in result if p["is_money_market"]]
    tbills_cds.sort(key=lambda p: p["maturity_date"] or datetime.max)

    return equities + money_markets + tbills_cds


# ── Data Row Builder ───────────────────────────────────────────────────────────

def build_data_row(pos, formula_row, col_map, r, is_ira=False):
    row = subst_row(formula_row, r)

    def set_col(header, value):
        idx = col_map.get(header)
        if idx is not None:
            row[idx - 1] = value

    def get_letter(header, fallback_col):
        return col_letter(col_map.get(header, fallback_col))

    set_col("idx", r - 1)
    set_col("Symbol", pos["display_symbol"] or pos["symbol"])
    set_col("Name", pos["description"])
    set_col("Shares", pos["quantity"] if pos["quantity"] else "")
    set_col("Price Paid in US Dollars", 1.0)

    avg_cb = pos["avg_cost_basis"]
    set_col("Price Paid", avg_cb if avg_cb is not None else "")

    if pos["is_tbill"] or pos["is_cd"]:
        set_col("Price", pos["last_price"])
        set_col("Price Change", pos["last_price_change"])

        d = get_letter("Price", 4)
        e = get_letter("Price Change", 5)
        set_col("% Change", f'=IFERROR({e}{r}/{d}{r},"–")')

        g = get_letter("Shares", 7)
        set_col("Market Value", f'=IFERROR({d}{r}*({g}{r}/100),"–")')

        for col_name in ("Ann Div / sh", "Ann Div", "Div %", "Cost Div %",
                         "Ex-Div Date", "Div Pay Date", "Next Div Ttl", "Divs/year"):
            set_col(col_name, "")

    if pos["is_money_market"]:
        set_col("Price", 1.0)
        set_col("Price Change", 0.0)
        set_col("% Change", 0.0)
        for col_name in ("Gain", "% Gain", "Gain %"):
            set_col(col_name, "")

    return row


# ── Totals Builder ─────────────────────────────────────────────────────────────

def _build_fallback_totals(col_map, num_cols, last_data, tot_row):
    row = [""] * num_cols

    def sf(header, formula):
        idx = col_map.get(header)
        if idx:
            row[idx - 1] = formula

    def L(header, fallback):
        return col_letter(col_map.get(header, fallback))

    g = L("Shares", 7);        sf("Shares",      f"=SUM({g}2:{g}{last_data})")
    j = L("Cost Basis", 10);   sf("Cost Basis",   f"=SUM({j}2:{j}{last_data})")
    k = L("Market Value", 11); sf("Market Value", f"=SUM({k}2:{k}{last_data})")
    sf("Gain",   f'=IFERROR({k}{tot_row}-{j}{tot_row},"–")')
    sf("% Gain", f'=IFERROR(({k}{tot_row}-{j}{tot_row})/{j}{tot_row},"–")')
    q = L("Ann Div", 17);      sf("Ann Div",      f"=SUM({q}2:{q}{last_data})")
    w = L("Next Div Ttl", 23); sf("Next Div Ttl", f"=SUM({w}2:{w}{last_data})")

    return row


# ── Sheet Builder ─────────────────────────────────────────────────────────────

def build_sheet(doc: str, sheet_name: str, positions: list,
                col_map_orig: dict, headers_orig: list, formula_row_orig: list,
                num_cols: int, col_widths: list, month_headers: list,
                is_ira: bool = False, use_template: bool = False) -> int:
    """Populate a Numbers sheet with portfolio data.

    use_template=True  — The output document was created by copying the template
      file, so the _template sheet already exists with full formatting.  Data is
      written directly to that sheet, which is renamed to sheet_name at the end.

    use_template=False — A blank sheet is created and column widths + number
      formats are copied from the template before writing data.
    """
    TABLE = "My Portfolio"
    n = len(positions)
    last_data = n + 1
    tot_row   = n + 2
    extra     = 5 if not is_ira else 3
    total_rows = 1 + n + extra

    col_map    = dict(col_map_orig)
    headers    = list(headers_orig)
    formula_row = list(formula_row_orig)

    # IRA: rename "Gain %" → "% of Portfolio"
    gain_pct_idx = col_map.get("Gain %")
    if is_ira and gain_pct_idx:
        col_map["% of Portfolio"] = gain_pct_idx
        del col_map["Gain %"]
        headers[gain_pct_idx - 1] = "% of Portfolio"

    print(f"\n  Creating sheet '{sheet_name}' ({n} positions)...")

    if use_template:
        # ── Template path: write directly into the _template sheet ──
        # The sheet and table already exist with full formatting from the template.
        working_sheet = "_template"

        # Resize the existing table, then clear rows 2+
        resize_table_as(doc, working_sheet, TABLE, total_rows)
        clear_data_rows_as(doc, working_sheet, TABLE, num_cols, 2, total_rows)
        print(f"    Using _template sheet (full formatting preserved)")
    else:
        # ── Blank path: create a new sheet + table, copy formatting ──
        working_sheet = sheet_name
        add_sheet_and_table_as(doc, sheet_name, TABLE, total_rows, num_cols)
        _apply_column_formatting_jxa(doc, sheet_name, TABLE, col_map,
                                     month_headers, is_ira, tot_row, col_widths)
        print(f"    Created blank sheet with column widths and formats applied")

    # ── Build header row with month-name overrides ──
    header_row = list(headers)
    month_col_start = None
    for key in sorted(col_map, key=lambda k: col_map[k]):
        if re.match(r"^[A-Z][a-z]+ \d{4}$", key):
            month_col_start = col_map[key]
            break
    if month_col_start:
        for i, mh in enumerate(month_headers):
            ci = month_col_start - 1 + i
            old_key = header_row[ci] if ci < len(header_row) else None
            header_row[ci] = mh
            if old_key and old_key in col_map:
                col_map[mh] = col_map.pop(old_key)
    _write_single_row_as(doc, working_sheet, TABLE, 1, header_row)

    # ── Build and write data rows ──
    k_letter = col_letter(col_map.get("Market Value", 11))
    pct_idx  = col_map.get("% of Portfolio") if is_ira else None

    data_rows = []
    for i, pos in enumerate(positions):
        r = i + 2
        row = build_data_row(pos, formula_row, col_map, r, is_ira)
        if is_ira and pct_idx:
            row[pct_idx - 1] = f'=IFERROR({k_letter}{r}/{k_letter}{tot_row},"–")'
        data_rows.append(row)

    print(f"    Writing {len(positions)} data rows...")
    _write_rows_as(doc, working_sheet, TABLE, 2, data_rows)

    # ── Write totals row ──
    totals = _build_fallback_totals(col_map, num_cols, last_data, tot_row)
    label = "Portfolio-IRA" if is_ira else "Portfolio"
    a_idx = col_map.get("idx", 1)
    if a_idx:
        totals[a_idx - 1] = label
    if is_ira and pct_idx:
        totals[pct_idx - 1] = 1.0
    _write_single_row_as(doc, working_sheet, TABLE, tot_row, totals)

    # ── Tax rows (brokerage only) ──
    if not is_ira:
        _write_tax_rows_as(doc, working_sheet, TABLE, tot_row + 2)

    # ── Rename _template → sheet_name (template path only) ──
    if use_template:
        rename_sheet_as(doc, "_template", sheet_name)

    print(f"  Sheet '{sheet_name}' complete.")
    return tot_row


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build Numbers portfolio from Fidelity CSV")
    parser.add_argument("csv_file")
    parser.add_argument("--template", default="Portfolio Template.numbers")
    parser.add_argument("--doc-name")
    parser.add_argument("--brokerage-only", action="store_true")
    parser.add_argument("--ira-only", action="store_true")
    args = parser.parse_args()

    print(f"Parsing {args.csv_file}...")
    all_positions = parse_csv(args.csv_file)
    print(f"  {len(all_positions)} rows parsed")
    bucket_counts = {}
    for p in all_positions:
        bucket_counts[p["bucket"]] = bucket_counts.get(p["bucket"], 0) + 1
    for b, c in sorted(bucket_counts.items()):
        print(f"  {b}: {c}")

    print(f"\nReading template '{args.template}'...")
    _tmpl_doc_name, col_map, headers, formula_row, num_cols, col_widths = read_template(args.template)
    template_path = _resolve_template_path(args.template)
    print(f"  Template: '{template_path}'")
    print(f"  {num_cols} columns, {len(col_map)} named headers")

    doc_name      = args.doc_name or derive_doc_name(args.csv_file)
    month_headers = derive_month_headers(args.csv_file)
    print(f"\nDocument: '{doc_name}'")
    print(f"Month columns: {', '.join(month_headers)}")

    # ── Close any open document with the target name before copying ──
    print("\nPreparing document...")
    for d in list_documents_as():
        if d == doc_name or re.match(re.escape(doc_name) + r" \d+$", d):
            close_document_as(d)
            print(f"  Closed existing '{d}'")

    print(f"Creating '{doc_name}' from template...")
    actual_doc = setup_output_document(template_path, doc_name)
    print(f"  Opened: '{actual_doc}'")

    summary = {}
    template_used = False  # tracks whether _template sheet has been consumed

    if not args.ira_only:
        brokerage = aggregate_positions(all_positions, {"BROKERAGE"})
        build_sheet(actual_doc, "Portfolio", brokerage,
                    col_map, list(headers), list(formula_row),
                    num_cols, col_widths, month_headers,
                    is_ira=False, use_template=True)
        template_used = True
        summary["Portfolio"] = {
            "positions":    len(brokerage),
            "market_value": sum(p["current_value"] for p in brokerage),
            "cost_basis":   sum(p["cost_basis_total"] or 0 for p in brokerage),
        }

    if not args.brokerage_only:
        ira = aggregate_positions(all_positions, {"IRA", "ROTH"})
        build_sheet(actual_doc, "Portfolio-IRA", ira,
                    col_map, list(headers), list(formula_row),
                    num_cols, col_widths, month_headers,
                    is_ira=True, use_template=not template_used)
        summary["Portfolio-IRA"] = {
            "positions":    len(ira),
            "market_value": sum(p["current_value"] for p in ira),
            "cost_basis":   sum(p["cost_basis_total"] or 0 for p in ira),
        }

    # Remove _template if it wasn't consumed (e.g. both sheets used blank path)
    if not template_used:
        delete_sheet_as(actual_doc, "_template")

    print("\n" + "=" * 56)
    print("SUMMARY")
    print("=" * 56)
    for sheet, t in summary.items():
        gain = t["market_value"] - t["cost_basis"]
        print(f"\n{sheet}:")
        print(f"  Positions:    {t['positions']}")
        print(f"  Market Value: ${t['market_value']:>13,.2f}")
        print(f"  Cost Basis:   ${t['cost_basis']:>13,.2f}")
        print(f"  Gain/Loss:    ${gain:>13,.2f}")
    print(f"\nDocument: '{actual_doc}'")


if __name__ == "__main__":
    main()
