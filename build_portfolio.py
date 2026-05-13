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

DESKTOP_DIR = os.path.expanduser("~/Desktop")
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

# Regex matching any _templateN-style sheet name (handles typos like _templat6)
TEMPLATE_SHEET_RE = re.compile(r"^_templa\w*(\d+)$")


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
    s = str(v) if v is not None else ""
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")
    return f'"{s}"'


def _as_val(v: object) -> str:
    if v is None or v == "":
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(float(v))
    s = str(v)
    if s.startswith("="):
        return '""'  # formula placeholder — written in formula pass
    return _as_str(s)


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


def rename_sheet_as(doc: str, old_name: str, new_name: str):
    script = f'''tell application "Numbers"
  tell document {_as_str(doc)}
    set name of sheet {_as_str(old_name)} to {_as_str(new_name)}
  end tell
end tell'''
    run_applescript_file(script)


# ── Template Reading (JXA) ─────────────────────────────────────────────────────

def _resolve_template_path(template_doc: str) -> str:
    if os.path.isabs(template_doc):
        return template_doc
    if os.path.exists(template_doc):
        return os.path.abspath(template_doc)
    return os.path.join(NUMBERS_DIR, os.path.basename(template_doc))


def _ensure_template_open(template_doc: str):
    abs_path = _resolve_template_path(template_doc)
    if not os.path.exists(abs_path):
        sys.exit(f"ERROR: Template file not found at '{abs_path}'")
    subprocess.run(["open", "-a", "Numbers", abs_path])
    time.sleep(3)


def read_template(template_doc: str):
    """Return (template_doc_name, col_map, headers, formula_row, num_cols, col_widths).

    Reads from _template1 sheet in the template document.
    """
    _ensure_template_open(template_doc)

    jxa = f'''
var app = Application("Numbers");
var doc = null, sheet = null;
var docs = app.documents();
outer: for (var i = 0; i < docs.length; i++) {{
  var sheets = docs[i].sheets();
  for (var j = 0; j < sheets.length; j++) {{
    var sname = sheets[j].name();
    if (sname === "_template1" || sname === "_template") {{
      doc = docs[i]; sheet = sheets[j]; break outer;
    }}
  }}
}}
if (!doc) throw new Error("No open Numbers document has a _template1 (or _template) sheet.");

var tables = sheet.tables();
var table = null;
for (var i = 0; i < tables.length; i++) {{
  if (tables[i].name() === "My Portfolio") {{ table = tables[i]; break; }}
}}
if (!table) throw new Error("Table My Portfolio not found in template sheet");

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
    _validate_formula_refs(formula_row, col_map)

    return template_doc_name, col_map, headers, formula_row, num_cols, col_widths


# ── Batch Write Helpers ────────────────────────────────────────────────────────

def _write_rows_as_batch(doc: str, sheet: str, table: str, start_row: int, rows: list):
    """Write rows in 2 osascript calls: one for all static values, one for all formulas.

    Uses `set value of cells of row N to {list}` (per-row, not range) because
    Numbers does not accept mixed-type 2D lists with `set value of range`.
    All rows are batched into a single AppleScript script for each pass.
    """
    if not rows:
        return

    # Pass 1: all static values — one `set value of cell` per non-empty, non-formula cell,
    # all batched into a single AppleScript script. Numbers rejects mixed-type list literals
    # so we cannot use `set value of cells of row N to {list}` or `set value of range`.
    cell_stmts = []
    for ri, row in enumerate(rows):
        r = start_row + ri
        for ci, cell in enumerate(row):
            if isinstance(cell, str) and cell.startswith("="):
                continue
            if cell == "" or cell is None:
                continue
            cell_stmts.append(f"set value of cell {ci + 1} of row {r} to {_as_val(cell)}")

    body1 = "\n        ".join(cell_stmts)
    script1 = f'''tell application "Numbers"
  tell document {_as_str(doc)}
    tell sheet {_as_str(sheet)}
      tell table {_as_str(table)}
        {body1}
      end tell
    end tell
  end tell
end tell'''
    run_applescript_file(script1)

    # Pass 2: all formula cells in one script.
    # Use `set value of cell C of row R to "=formula"` — Numbers parses strings
    # that start with "=" as formulas. _as_str handles " → \" which AppleScript
    # unescapes correctly when reading from a file.
    formula_stmts = []
    for ri, row in enumerate(rows):
        r = start_row + ri
        for ci, cell in enumerate(row):
            if isinstance(cell, str) and cell.startswith("="):
                formula_stmts.append(
                    f"set value of cell {ci + 1} of row {r} to {_as_str(cell)}"
                )

    if formula_stmts:
        body2 = "\n        ".join(formula_stmts)
        script2 = f'''tell application "Numbers"
  tell document {_as_str(doc)}
    tell sheet {_as_str(sheet)}
      tell table {_as_str(table)}
        {body2}
      end tell
    end tell
  end tell
end tell'''
        run_applescript_file(script2)


def _write_single_row_as(doc: str, sheet: str, table: str, actual_row: int, row: list):
    """Write one row via a single osascript call (used for header and totals)."""
    stmts = []
    for ci, cell in enumerate(row):
        c = ci + 1
        if isinstance(cell, str) and cell.startswith("="):
            # `set value to "=formula"` — Numbers parses "=..." strings as formulas
            stmts.append(f"set value of cell {c} of row {actual_row} to {_as_str(cell)}")
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


def _write_tax_rows_as(doc: str, sheet: str, table: str, tax_row: int):
    # Numbers Creator Studio doesn't reliably expose a 'percentage' enum constant for
    # `set format of cell`, and column-level format can override cell-level JXA format.
    # Write the rates as pre-formatted percentage strings — display is always correct.
    fed   = 0.1644
    state = 0.0917
    total = fed + state
    script = f'''tell application "Numbers"
  tell document {_as_str(doc)}
    tell sheet {_as_str(sheet)}
      tell table {_as_str(table)}
        set value of cell 1 of row {tax_row} to "Federal Rate"
        set value of cell 2 of row {tax_row} to {_as_str(f"{fed:.2%}")}
        set value of cell 4 of row {tax_row} to "State Rate"
        set value of cell 5 of row {tax_row} to {_as_str(f"{state:.2%}")}
        set value of cell 7 of row {tax_row} to "Total Tax Rate"
        set value of cell 8 of row {tax_row} to {_as_str(f"{total:.2%}")}
      end tell
    end tell
  end tell
end tell'''
    run_applescript_file(script)


# ── Output Document Setup ──────────────────────────────────────────────────────

def setup_output_document(template_path: str, doc_name: str, output_dir: str) -> str:
    """Copy the template to output_dir, open it, and return the settled document name."""
    os.makedirs(output_dir, exist_ok=True)
    dest = os.path.join(output_dir, f"{doc_name}.numbers")

    if os.path.exists(dest):
        suffix = 2
        while os.path.exists(os.path.join(output_dir, f"{doc_name} ({suffix}).numbers")):
            suffix += 1
        dest = os.path.join(output_dir, f"{doc_name} ({suffix}).numbers")
        effective_name = f"{doc_name} ({suffix})"
        print(f"  WARNING: '{doc_name}.numbers' already exists; using '{os.path.basename(dest)}'")
    else:
        effective_name = doc_name

    shutil.copy2(template_path, dest)

    before_set = set(list_documents_as())
    subprocess.run(["open", "-a", "Numbers", dest])

    actual_doc = effective_name
    for _ in range(20):
        time.sleep(0.5)
        after = list_documents_as()
        new = [d for d in after if d not in before_set]
        if new:
            actual_doc = new[0]
            break

    time.sleep(1)
    confirm_jxa = f'''
var app = Application("Numbers");
var docs = app.documents();
var base = {json.dumps(effective_name)};
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


def clear_data_rows_as(doc: str, sheet: str, table: str,
                        num_cols: int, from_row: int, to_row: int):
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
    jxa = f'''
var app = Application("Numbers");
var tbl = app.documents[{json.dumps(doc)}].sheets[{json.dumps(sheet)}].tables[{json.dumps(table)}];
try {{ tbl.rowCount = {num_rows}; }} catch(e) {{}}
"ok"
'''
    run_jxa_file(jxa)


# ── Helpers ────────────────────────────────────────────────────────────────────

def fmt_money(v):
    return round(v, 2) if v is not None else v

def fmt_qty(v):
    return round(v, 4) if v is not None else v


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
    result = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def _resolve_named_refs(formula: str, col_map: dict) -> str:
    """Convert Numbers internal named column refs like 'Shares 16' or 'Shares QQQM' → 'G2'.

    Numbers stores formulas using header-name refs where the suffix is either an internal
    table ID (digits) or the row's symbol/value (e.g. QQQM). Use \\w+ to match both.
    """
    if not formula or not formula.startswith("="):
        return formula
    header_to_letter = {h: col_letter(i) for h, i in col_map.items() if h and h.strip()}
    headers_sorted = sorted(header_to_letter.keys(), key=len, reverse=True)
    result = formula
    for header in headers_sorted:
        letter = header_to_letter[header]
        result = re.sub(re.escape(header) + r"\s+\w+", letter + "2", result)
    result = result.replace("×", "*").replace("−", "-").replace("÷", "/")
    return result


def _validate_formula_refs(formula_row: list, col_map: dict):
    """Raise if any formula still contains named references after resolution.

    Named refs look like 'Shares QQQM' (header + symbol). If they persist after
    _resolve_named_refs, row 2 of the template has live data that caused Numbers to
    convert column-letter refs into named refs during formula evaluation.
    """
    for f in formula_row:
        if not f or not f.startswith("="):
            continue
        for header in col_map:
            if header and re.search(re.escape(header) + r"\s+\w", f):
                sys.exit(
                    f"ERROR: Template formula patterns still contain named references "
                    f"(e.g. \"{header} ...\"). Row 2 of each _templateN sheet in the "
                    "template must be cleared of all data values before running. "
                    "Open Portfolio Template.numbers, select row 2 of the My Portfolio "
                    "table in each _templateN sheet, delete the values (not the formulas), "
                    "and save."
                )


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

        quantity     = fmt_qty(parse_float(row.get("Quantity")) or 0.0)
        last_price   = fmt_money(parse_float(row.get("Last Price")) or 0.0)
        last_chg     = fmt_money(parse_float(row.get("Last Price Change")) or 0.0)
        cur_value    = fmt_money(parse_float(row.get("Current Value")) or 0.0)
        cost_basis   = fmt_money(parse_float(row.get("Cost Basis Total")))
        avg_cb       = fmt_money(parse_float(row.get("Average Cost Basis")))

        # Bug 5/9: skip money-market rows with no real value
        if mm and cur_value == 0.0 and not cost_basis:
            continue

        # Bug 3: T-Bill avg_cost_basis = cost per $100 face value
        if (tbill or cd) and cost_basis is not None and quantity:
            avg_cb = fmt_money(cost_basis / (quantity / 100))

        positions.append({
            "account_name": account_name,
            "bucket": bucket,
            "symbol": effective_symbol,
            "display_symbol": symbol,
            "description": description,
            "quantity": quantity,
            "last_price": last_price,
            "last_price_change": last_chg,
            "current_value": cur_value,
            "cost_basis_total": cost_basis,
            "avg_cost_basis": avg_cb,
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
    set_col("Shares", fmt_qty(pos["quantity"]) if pos["quantity"] else "")
    set_col("Price Paid in US Dollars", 1.0)

    avg_cb = pos["avg_cost_basis"]
    set_col("Price Paid", fmt_money(avg_cb) if avg_cb is not None else "")

    if pos["is_tbill"] or pos["is_cd"]:
        # Bug 2/3: T-Bills quoted per $100 face value
        set_col("Price", pos["last_price"])
        set_col("Price Change", pos["last_price_change"])

        d = get_letter("Price", 4)
        e = get_letter("Price Change", 5)
        g = get_letter("Shares", 7)
        i = get_letter("Price Paid", 9)

        set_col("% Change", f'=IFERROR({e}{r}/{d}{r},"–")')
        set_col("Market Value", f'=IFERROR({d}{r}*({g}{r}/100),"–")')

        # Cost Basis: static if available (avoids rounding from formula), else formula
        cb = pos.get("cost_basis_total")
        if cb is not None:
            set_col("Cost Basis", fmt_money(cb))
        else:
            set_col("Cost Basis", f'=IFERROR({i}{r}*({g}{r}/100),"–")')

        for col_name in ("Ann Div / sh", "Ann Div", "Div %", "Cost Div %",
                         "Ex-Div Date", "Div Pay Date", "Next Div Ttl", "Divs/year"):
            set_col(col_name, "")

    if pos["is_money_market"]:
        # Bug 4: write static price and market value — don't let STOCK() formula run,
        # which would pick up a percentage format from the template cell
        set_col("Price", 1.0)
        set_col("Price Change", 0.0)
        set_col("% Change", 0.0)
        # Write market value directly from CSV so blank-Shares rows still show a value
        mv = pos.get("current_value")
        if mv:
            set_col("Market Value", fmt_money(mv))
        for col_name in ("Gain", "% Gain", "Gain %", "Cost Basis"):
            set_col(col_name, "")

    return row


# ── Totals Builder ─────────────────────────────────────────────────────────────

def _build_fallback_totals(col_map, num_cols, last_data, tot_row, month_headers=None):
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
    sf("Gain",        f'=IFERROR({k}{tot_row}-{j}{tot_row},"–")')
    sf("% Gain",      f'=IFERROR(({k}{tot_row}-{j}{tot_row})/{j}{tot_row},"–")')
    q = L("Ann Div", 17);      sf("Ann Div",      f"=SUM({q}2:{q}{last_data})")
    sf("Div %",       f'=IFERROR({q}{tot_row}/{k}{tot_row},"-")')
    sf("Cost Div %",  f'=IFERROR({q}{tot_row}/{j}{tot_row},"-")')
    w = L("Next Div Ttl", 23); sf("Next Div Ttl", f"=SUM({w}2:{w}{last_data})")

    # Monthly dividend columns
    for mh in (month_headers or []):
        mh_col = col_map.get(mh)
        if mh_col:
            ml = col_letter(mh_col)
            sf(mh, f"=SUM({ml}2:{ml}{last_data})")

    return row


# ── Spot-check ────────────────────────────────────────────────────────────────

def spot_check_sheet(doc: str, sheet: str, positions: list, col_map: dict, tot_row: int):
    """Read back key cell values from Numbers and print them for visual confirmation."""
    TABLE = "My Portfolio"
    k_idx = col_map.get("Market Value", 11) - 1   # 0-based
    j_idx = col_map.get("Cost Basis", 10) - 1
    b_idx = col_map.get("Symbol", 2) - 1
    g_idx = col_map.get("Shares", 7) - 1

    # Find T-Bill rows for the /100 sanity check
    tbill_rows = [i + 2 for i, p in enumerate(positions) if p.get("is_tbill") or p.get("is_cd")]

    jxa = f'''
var app = Application("Numbers");
var doc = app.documents[{json.dumps(doc)}];
var tbl = doc.sheets[{json.dumps(sheet)}].tables[{json.dumps(TABLE)}];
var kIdx = {k_idx}, jIdx = {j_idx}, bIdx = {b_idx}, gIdx = {g_idx};
var totRow = {tot_row - 1};  // 0-based
var result = {{}};

// Row 2 (first data row, 0-based = 1)
try {{ result.row2_sym = String(tbl.rows[1].cells[bIdx].value()); }} catch(e) {{ result.row2_sym = "ERR"; }}
try {{ result.row2_sh  = tbl.rows[1].cells[gIdx].value(); }} catch(e) {{ result.row2_sh = null; }}
try {{ result.row2_mv  = tbl.rows[1].cells[kIdx].value(); }} catch(e) {{ result.row2_mv = null; }}
try {{ result.row2_cb  = tbl.rows[1].cells[jIdx].value(); }} catch(e) {{ result.row2_cb = null; }}

// Totals row
try {{ result.tot_mv   = tbl.rows[totRow].cells[kIdx].value(); }} catch(e) {{ result.tot_mv = null; }}
try {{ result.tot_cb   = tbl.rows[totRow].cells[jIdx].value(); }} catch(e) {{ result.tot_cb = null; }}

// T-Bill rows — check for implausibly large Market Values
var tbillRows = {json.dumps(tbill_rows)};
result.tbill_mv = {{}};
for (var i = 0; i < tbillRows.length; i++) {{
  var r0 = tbillRows[i] - 1;
  try {{
    var sym = String(tbl.rows[r0].cells[bIdx].value());
    var mv  = tbl.rows[r0].cells[kIdx].value();
    result.tbill_mv[sym] = mv;
  }} catch(e) {{}}
}}

JSON.stringify(result);
'''
    try:
        raw = run_jxa_file(jxa)
        sc = json.loads(raw)
    except Exception as e:
        print(f"    [spot-check failed: {e}]")
        return

    mv2 = sc.get("row2_mv")
    cb2 = sc.get("row2_cb")
    sh2 = sc.get("row2_sh")
    sym2 = sc.get("row2_sym", "?")
    tot_mv = sc.get("tot_mv")
    tot_cb = sc.get("tot_cb")

    def fmt(v):
        try:
            return f"${float(v):,.0f}"
        except Exception:
            return str(v)

    print(f"    Spot-check row 2: {sym2}  Shares={sh2}  MV={fmt(mv2)}  Cost={fmt(cb2)}")
    if tot_mv is not None and tot_cb is not None:
        try:
            gain = float(tot_mv) - float(tot_cb)
            print(f"    Spot-check totals: MV={fmt(tot_mv)}  Cost={fmt(tot_cb)}  Gain={fmt(gain)}")
        except Exception:
            pass

    for sym, mv in sc.get("tbill_mv", {}).items():
        try:
            if float(mv) > 10_000_000:
                print(f"    WARNING: T-Bill '{sym}' market value {fmt(mv)} looks wrong — check /100 formula")
            else:
                print(f"    Spot-check T-Bill {sym}: MV={fmt(mv)}")
        except Exception:
            pass


# ── Sheet Builder ──────────────────────────────────────────────────────────────

def build_sheet(doc: str, sheet_name: str, positions: list,
                col_map_orig: dict, headers_orig: list, formula_row_orig: list,
                num_cols: int, col_widths: list, month_headers: list,
                is_ira: bool = False, template_sheet: str = "_template1") -> int:
    """Populate a Numbers sheet by writing into the pre-formatted template_sheet,
    then renaming it to sheet_name.

    Returns tot_row (the totals row index).
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

    gain_pct_idx = col_map.get("Gain %")
    if is_ira and gain_pct_idx:
        col_map["% of Portfolio"] = gain_pct_idx
        del col_map["Gain %"]
        headers[gain_pct_idx - 1] = "% of Portfolio"

    print(f"\n  Building sheet '{sheet_name}' ({n} positions) from {template_sheet}...")

    resize_table_as(doc, template_sheet, TABLE, total_rows)
    clear_data_rows_as(doc, template_sheet, TABLE, num_cols, 2, total_rows)

    # ── Header row with month-name overrides ──
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
    _write_single_row_as(doc, template_sheet, TABLE, 1, header_row)

    # ── Build and batch-write data rows ──
    k_letter = col_letter(col_map.get("Market Value", 11))
    pct_idx  = col_map.get("% of Portfolio") if is_ira else None

    data_rows = []
    for i, pos in enumerate(positions):
        r = i + 2
        row = build_data_row(pos, formula_row, col_map, r, is_ira)
        if is_ira and pct_idx:
            row[pct_idx - 1] = f'=IFERROR({k_letter}{r}/{k_letter}{tot_row},"–")'
        data_rows.append(row)

    print(f"    Writing {n} data rows (batch)...")
    _write_rows_as_batch(doc, template_sheet, TABLE, 2, data_rows)

    # ── Totals row ──
    totals = _build_fallback_totals(col_map, num_cols, last_data, tot_row, month_headers)
    label = "Portfolio-IRA" if is_ira else "Portfolio"
    a_idx = col_map.get("idx", 1)
    if a_idx:
        totals[a_idx - 1] = label
    if is_ira and pct_idx:
        totals[pct_idx - 1] = 1.0
    _write_single_row_as(doc, template_sheet, TABLE, tot_row, totals)

    # ── Tax rows (brokerage only) ──
    if not is_ira:
        _write_tax_rows_as(doc, template_sheet, TABLE, tot_row + 2)

    rename_sheet_as(doc, template_sheet, sheet_name)
    print(f"  ✓ Sheet '{sheet_name}' complete.")

    spot_check_sheet(doc, sheet_name, positions, col_map, tot_row)

    return tot_row


# ── Dividend Gap-Fill ──────────────────────────────────────────────────────────

def fill_dividends(doc: str, sheet_positions: dict, col_map: dict):
    """Use Claude API with web search to fill missing dividend data.

    sheet_positions: {sheet_name: [(row_index, position), ...]}
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return

    try:
        import anthropic
    except ImportError:
        print("  NOTE: 'anthropic' package not installed; skipping dividend fill.")
        return

    # Collect equities with potentially missing dividend data
    to_fill = []
    for sheet_name, rows in sheet_positions.items():
        for row_idx, pos in rows:
            sym = pos.get("display_symbol") or pos.get("symbol", "")
            if not sym or pos["is_tbill"] or pos["is_cd"] or pos["is_money_market"]:
                continue
            to_fill.append((sheet_name, row_idx, sym, pos["description"]))

    if not to_fill:
        return

    symbols = sorted({sym for _, _, sym, _ in to_fill})
    print(f"\n  Filling dividend data for {len(symbols)} symbols via Claude API...")

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        f"For each stock symbol, provide current dividend data. "
        f"Return ONLY valid JSON (no markdown), structured as:\n"
        f'{{"SYMBOL": {{"div_per_share": <float or null>, "yield_pct": <float or null>, '
        f'"ex_div_date": "<MM/DD/YYYY or null>", "pay_date": "<MM/DD/YYYY or null>", '
        f'"divs_per_year": <int or null>}}}}\n\n'
        f"Symbols: {', '.join(symbols)}"
    )

    try:
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=4096,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"  WARNING: Claude API call failed: {e}")
        return

    # Extract JSON from response
    raw_json = ""
    for block in response.content:
        if hasattr(block, "text"):
            raw_json = block.text.strip()
            break

    try:
        div_data = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError):
        m = re.search(r"\{.*\}", raw_json, re.DOTALL)
        if not m:
            print("  WARNING: Could not parse dividend data from Claude response.")
            return
        try:
            div_data = json.loads(m.group(0))
        except Exception:
            return

    # Write dividend data back to Numbers
    div_sh_col   = col_map.get("Ann Div / sh")
    div_pct_col  = col_map.get("Div %")
    ex_div_col   = col_map.get("Ex-Div Date")
    pay_date_col = col_map.get("Div Pay Date")
    divs_yr_col  = col_map.get("Divs/year")

    TABLE = "My Portfolio"
    filled = 0
    for sheet_name, row_idx, sym, _ in to_fill:
        info = div_data.get(sym)
        if not info:
            continue

        stmts = []
        if div_sh_col and info.get("div_per_share") is not None:
            stmts.append(f"set value of cell {div_sh_col} of row {row_idx} to {float(info['div_per_share'])}")
        if div_pct_col and info.get("yield_pct") is not None:
            stmts.append(f"set value of cell {div_pct_col} of row {row_idx} to {float(info['yield_pct']) / 100}")
        if ex_div_col and info.get("ex_div_date"):
            stmts.append(f"set value of cell {ex_div_col} of row {row_idx} to {_as_str(info['ex_div_date'])}")
        if pay_date_col and info.get("pay_date"):
            stmts.append(f"set value of cell {pay_date_col} of row {row_idx} to {_as_str(info['pay_date'])}")
        if divs_yr_col and info.get("divs_per_year") is not None:
            stmts.append(f"set value of cell {divs_yr_col} of row {row_idx} to {int(info['divs_per_year'])}")

        if stmts:
            body = "\n        ".join(stmts)
            script = f'''tell application "Numbers"
  tell document {_as_str(doc)}
    tell sheet {_as_str(sheet_name)}
      tell table {_as_str(TABLE)}
        {body}
      end tell
    end tell
  end tell
end tell'''
            try:
                run_applescript_file(script)
                filled += 1
            except RuntimeError as e:
                print(f"  WARNING: Could not write dividend data for {sym}: {e}")

    if filled:
        print(f"  ✓ Dividend data filled for {filled} positions.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build Numbers portfolio from Fidelity CSV")
    parser.add_argument("csv_file")
    parser.add_argument("--template", default="Portfolio Template.numbers")
    parser.add_argument("--doc-name")
    parser.add_argument("--output-dir", default=DESKTOP_DIR,
                        help=f"Directory to save the output .numbers file (default: ~/Desktop)")
    parser.add_argument("--brokerage-only", action="store_true")
    parser.add_argument("--ira-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse CSV and report; do not open or modify Numbers")
    parser.add_argument("--no-dividend-fill", action="store_true",
                        help="Skip the Claude API dividend gap-fill step")
    args = parser.parse_args()

    print(f"Parsing {args.csv_file}...")
    all_positions = parse_csv(args.csv_file)
    print(f"  {len(all_positions)} rows parsed")
    bucket_counts = {}
    for p in all_positions:
        bucket_counts[p["bucket"]] = bucket_counts.get(p["bucket"], 0) + 1
    for b, c in sorted(bucket_counts.items()):
        print(f"  {b}: {c}")

    doc_name      = args.doc_name or derive_doc_name(args.csv_file)
    month_headers = derive_month_headers(args.csv_file)
    output_dir    = os.path.expanduser(args.output_dir)

    print(f"\nDocument: '{doc_name}'")
    print(f"Output:   {output_dir}")
    print(f"Month columns: {', '.join(month_headers)}")

    if args.dry_run:
        brokerage = aggregate_positions(all_positions, {"BROKERAGE"})
        ira = aggregate_positions(all_positions, {"IRA", "ROTH"})
        print(f"\n[dry-run] Portfolio: {len(brokerage)} positions")
        print(f"[dry-run] Portfolio-IRA: {len(ira)} positions")
        return

    print(f"\nReading template '{args.template}'...")
    _tmpl_doc_name, col_map, headers, formula_row, num_cols, col_widths = read_template(args.template)
    template_path = _resolve_template_path(args.template)
    print(f"  Template: '{template_path}'")
    print(f"  {num_cols} columns, {len(col_map)} named headers")

    # Close any open document with the target name before copying
    print("\nPreparing document...")
    for d in list_documents_as():
        if d == doc_name or re.match(re.escape(doc_name) + r"( \(\d+\))?(.numbers)?$", d):
            close_document_as(d)
            print(f"  Closed existing '{d}'")

    print(f"Creating '{doc_name}' from template...")
    actual_doc = setup_output_document(template_path, doc_name, output_dir)
    print(f"  ✓ Opened: '{actual_doc}'")

    # Determine which template sheets are available (regex-based, tolerates typos like _templat6)
    all_sheets = list_sheets_as(actual_doc)
    available_templates = sorted(
        [s for s in all_sheets if TEMPLATE_SHEET_RE.match(s)],
        key=lambda s: int(TEMPLATE_SHEET_RE.match(s).group(1)),
    )
    template_queue = list(available_templates)

    summary = {}
    sheet_positions_for_divs = {}  # for dividend fill

    def next_template() -> str:
        if not template_queue:
            sys.exit("ERROR: Ran out of template sheets. Add more _templateN sheets to the template.")
        return template_queue.pop(0)

    if not args.ira_only:
        brokerage = aggregate_positions(all_positions, {"BROKERAGE"})
        tmpl = next_template()
        build_sheet(actual_doc, "Portfolio", brokerage,
                    col_map, list(headers), list(formula_row),
                    num_cols, col_widths, month_headers,
                    is_ira=False, template_sheet=tmpl)
        summary["Portfolio"] = {
            "positions":    len(brokerage),
            "market_value": sum(p["current_value"] for p in brokerage),
            "cost_basis":   sum(p["cost_basis_total"] or 0 for p in brokerage),
        }
        sheet_positions_for_divs["Portfolio"] = [(i + 2, p) for i, p in enumerate(brokerage)]

    if not args.brokerage_only:
        ira = aggregate_positions(all_positions, {"IRA", "ROTH"})
        tmpl = next_template()
        build_sheet(actual_doc, "Portfolio-IRA", ira,
                    col_map, list(headers), list(formula_row),
                    num_cols, col_widths, month_headers,
                    is_ira=True, template_sheet=tmpl)
        summary["Portfolio-IRA"] = {
            "positions":    len(ira),
            "market_value": sum(p["current_value"] for p in ira),
            "cost_basis":   sum(p["cost_basis_total"] or 0 for p in ira),
        }
        sheet_positions_for_divs["Portfolio-IRA"] = [(i + 2, p) for i, p in enumerate(ira)]

    # Delete unused template sheets
    for unused in template_queue:
        print(f"  Deleting unused template sheet '{unused}'...")
        delete_sheet_as(actual_doc, unused)

    # Dividend gap-fill
    if not args.no_dividend_fill:
        fill_dividends(actual_doc, sheet_positions_for_divs, col_map)

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
    print(f"Saved to: {output_dir}")


if __name__ == "__main__":
    main()
