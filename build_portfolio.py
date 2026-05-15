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

# Three buckets checked in priority order: ROTH > IRA > BROKERAGE (catch-all).
# IRA includes traditional IRA, rollover IRA, IRA BDA, and 401k accounts.
# BROKERAGE is the catch-all for trust, CMA, joint, and any other non-tax-advantaged account.

# Regex matching any _templateN-style sheet name (handles typos like _templat6)
TEMPLATE_SHEET_RE = re.compile(r"^_templa\w*(\d+)$")

# Dynamic Numbers formulas for the four monthly dividend header cells.
# MOD(...,12) handles year rollover; IFERROR catches MONTHNAME(0) for December.
MONTH_HEADER_FORMULAS = [
    '=IFERROR(MONTHNAME(MOD(MONTH(NOW()),12)),MONTHNAME(12)) & " " & YEAR(NOW())',
    '=IFERROR(MONTHNAME(MOD(MONTH(NOW())+1,12)),MONTHNAME(12)) & " " & YEAR(NOW())',
    '=IFERROR(MONTHNAME(MOD(MONTH(NOW())+2,12)),MONTHNAME(12)) & " " & YEAR(NOW())',
    '=IFERROR(MONTHNAME(MOD(MONTH(NOW())+3,12)),MONTHNAME(12)) & " " & YEAR(NOW())',
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


# ── Basis Override Reader ──────────────────────────────────────────────────────

def read_basis_overrides(template_doc_name: str) -> dict:
    """Read the _basis sheet from the already-open template document.

    Returns {(SYMBOL_UPPER, account_name_exact): avg_cost_basis_float}.
    Skips rows where Symbol or Account is empty. Skips the header row (index 0).
    Returns an empty dict if the sheet or table is missing (no error).
    """
    jxa = f'''
var app = Application("Numbers");
var result = [];
var docs = app.documents();
var doc = null;
for (var i = 0; i < docs.length; i++) {{
  if (docs[i].name() === {json.dumps(template_doc_name)}) {{ doc = docs[i]; break; }}
}}
if (doc) {{
  var sheets = doc.sheets();
  var sheet = null;
  for (var j = 0; j < sheets.length; j++) {{
    if (sheets[j].name() === "_basis") {{ sheet = sheets[j]; break; }}
  }}
  if (sheet) {{
    var tables = sheet.tables();
    var table = null;
    for (var k = 0; k < tables.length; k++) {{
      if (tables[k].name() === "Basis") {{ table = tables[k]; break; }}
    }}
    if (table) {{
      var rowCount = table.rowCount();
      for (var r = 1; r < rowCount; r++) {{
        try {{
          var sym = String(table.rows[r].cells[0].value() || "").trim();
          var acc = String(table.rows[r].cells[1].value() || "").trim();
          var cb  = String(table.rows[r].cells[2].value() || "").trim();
          if (sym && acc && cb) result.push([sym, acc, cb]);
        }} catch(e) {{}}
      }}
    }}
  }}
}}
JSON.stringify(result);
'''
    try:
        raw = run_jxa_file(jxa)
        rows = json.loads(raw)
    except Exception as e:
        print(f"  WARNING: Could not read _basis sheet: {e}")
        return {}

    overrides = {}
    for row in rows:
        sym, acc, cb_str = row
        cb_str = re.sub(r"[$,\s]", "", cb_str)
        try:
            overrides[(sym.upper(), acc)] = float(cb_str)
        except ValueError:
            pass
    return overrides


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
    if "roth" in normalized:
        return "ROTH"
    if "ira" in normalized or "401" in normalized:
        return "IRA"
    return "BROKERAGE"


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


def is_cash_position(pos) -> bool:
    """Return True if the position belongs in Portfolio-Cash rather than Portfolio."""
    return (
        pos["is_money_market"]
        or pos["is_tbill"]
        or pos["is_cd"]
        or "direct deposit" in pos["description"].lower()
        or "money market" in pos["description"].lower()
    )


def order_cash_positions(positions: list) -> list:
    """Order Portfolio-Cash rows: MMs by value desc, other cash, T-Bills by maturity."""
    mm     = [p for p in positions if p["is_money_market"]]
    tbills = [p for p in positions if p["is_tbill"] or p["is_cd"]]
    other  = [p for p in positions
              if not p["is_money_market"] and not p["is_tbill"] and not p["is_cd"]]
    mm.sort(key=lambda p: p["current_value"], reverse=True)
    tbills.sort(key=lambda p: p["maturity_date"] or datetime.max)
    return mm + other + tbills


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

def parse_csv(path, basis_overrides=None):
    if basis_overrides is None:
        basis_overrides = {}

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

        # _basis sheet override: highest-priority fallback for missing or zero avg cost basis.
        # Applies to equities only (not T-Bills/CDs, which have their own derivation above).
        if (avg_cb is None or avg_cb <= 0) and not (tbill or cd):
            key = (symbol.upper(), account_name)
            if key in basis_overrides:
                avg_cb = basis_overrides[key]
                cost_basis = fmt_money(avg_cb * quantity)
                print(f"  ✓ {symbol} ({account_name}): cost basis from _basis sheet (${avg_cb:.2f})")

        # When avg_cost_basis is missing for an equity with a known price, use last_price as a
        # temporary placeholder so the gain column shows ~$0 rather than the full current value.
        # TODO: replace with actual cost basis when available
        if avg_cb is None and not (tbill or cd or mm) and last_price > 0 and quantity > 0:
            avg_cb = last_price
            if cost_basis is None:
                cost_basis = fmt_money(avg_cb * quantity)

        # 401k positions from IRA-bucket accounts have no ticker symbol.
        is_401k = (not symbol) and bucket == "IRA"

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
            "is_401k": is_401k,
            "maturity_date": parse_maturity_date(description),
        })

    return positions


# ── Position Aggregation ───────────────────────────────────────────────────────

def aggregate_positions(all_positions, bucket):
    """Aggregate positions for a single bucket into sheet rows.

    Equities and funds are combined by symbol (weighted avg cost basis).
    Money market funds are kept as individual rows with account name appended
    to display_symbol (e.g. "SPAXX - Trust: Under Agreement").
    T-Bills and CDs are kept as individual rows, sorted by maturity date.
    """
    equities_by_symbol = {}
    money_markets = []
    tbills_cds = []

    for pos in all_positions:
        if pos["bucket"] != bucket:
            continue

        if pos["is_tbill"] or pos["is_cd"]:
            tbills_cds.append(dict(pos))
            continue

        if pos["is_money_market"]:
            mm_pos = dict(pos)
            # Each money market account gets its own row; append account name to symbol
            # so "SPAXX" from two accounts appears as distinct rows.
            mm_pos["display_symbol"] = f"{pos['symbol']} - {pos['account_name']}"
            money_markets.append(mm_pos)
            continue

        # Regular equity / fund (including 401k) — aggregate by symbol within bucket
        sym = pos["symbol"]
        if sym in equities_by_symbol:
            ex = equities_by_symbol[sym]
            ex["quantity"] += pos["quantity"]
            # Missing cost basis is treated as 0 to avoid aggregation errors.
            pos_cb = pos["cost_basis_total"] or 0.0
            ex["cost_basis_total"] = (ex["cost_basis_total"] or 0.0) + pos_cb
            ex["current_value"] += pos["current_value"]
        else:
            new_pos = dict(pos)
            if new_pos["cost_basis_total"] is None:
                print(f"  ⚠ {pos['symbol']} ({pos['account_name']}): no cost basis — using 0")
                new_pos["cost_basis_total"] = 0.0
                new_pos["avg_cost_basis"] = 0.0
            equities_by_symbol[sym] = new_pos

    result = []
    for pos in equities_by_symbol.values():
        q = pos["quantity"]
        cb = pos["cost_basis_total"]
        if q and cb:
            pos["avg_cost_basis"] = round(cb / q, 2)
        else:
            pos["avg_cost_basis"] = 0.0
        result.append(pos)

    equities = sorted(result, key=lambda p: p["current_value"], reverse=True)
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
    set_col("Shares", fmt_qty(pos["quantity"]) if pos["quantity"] else "")
    set_col("Price Paid in US Dollars", 1.0)

    avg_cb = pos["avg_cost_basis"]
    set_col("Price Paid", fmt_money(avg_cb) if avg_cb is not None else "")

    # 401k positions have no ticker; hardcode price/value columns and use description as name.
    # Formula columns (Gain, % Gain, etc.) keep their substituted formulas — they reference
    # the hardcoded static cells in the same row and will evaluate correctly.
    if pos.get("is_401k"):
        set_col("Symbol", "")
        set_col("Name", pos["description"])
        set_col("Price", pos["last_price"])
        set_col("Price Change", pos["last_price_change"])
        price = pos["last_price"] or 0.0
        chg   = pos["last_price_change"] or 0.0
        set_col("% Change", chg / price if price else 0.0)
        set_col("Market Value", pos["current_value"])
        qty   = pos["quantity"] or 0.0
        a_cb  = pos.get("avg_cost_basis") or 0.0
        cb    = fmt_money(a_cb * qty) if (a_cb and qty) else (pos.get("cost_basis_total") or 0.0)
        set_col("Cost Basis", cb)
        return row

    set_col("Symbol", pos["display_symbol"] if pos["display_symbol"] != "" else pos["symbol"])
    set_col("Name", pos["description"])

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
        # Hardcode price — don't let STOCK() formula run (it picks up a % format from template)
        set_col("Price", 1.0)
        set_col("Price Change", 0.0)
        set_col("% Change", 0.0)
        mv  = pos.get("current_value") or 0.0
        qty = pos.get("quantity") or 0.0
        d   = get_letter("Price", 4)
        g   = get_letter("Shares", 7)
        if qty > 0:
            set_col("Market Value", f"={d}{r}*{g}{r}")
        elif mv:
            # Quantity is blank/zero but we still have a dollar balance — write it statically
            set_col("Market Value", fmt_money(mv))
        # Cost basis = market value so gain shows as $0
        if mv:
            set_col("Cost Basis", fmt_money(mv))
        for col_name in ("Gain", "% Gain", "Gain %"):
            set_col(col_name, "")

    # Non-MM, non-T-Bill cash instruments on Portfolio-Cash (direct deposit etc.)
    if pos.get("is_cash_instrument") and not pos["is_money_market"] and not pos["is_tbill"] and not pos["is_cd"]:
        set_col("Price", 1.0)
        set_col("Price Change", 0.0)
        set_col("% Change", 0.0)
        mv  = pos.get("current_value") or 0.0
        qty = pos.get("quantity") or 0.0
        d   = get_letter("Price", 4)
        g   = get_letter("Shares", 7)
        if qty > 0:
            set_col("Market Value", f"={d}{r}*{g}{r}")
        elif mv:
            set_col("Market Value", fmt_money(mv))
        if mv:
            set_col("Cost Basis", fmt_money(mv))
        for col_name in ("Gain", "% Gain", "Gain %"):
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
                is_ira: bool = False, has_tax_rows: bool = None,
                template_sheet: str = "_template1") -> int:
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
            # Write a dynamic formula so the header self-updates each month;
            # col_map still tracks this position under the static mh key for totals.
            header_row[ci] = MONTH_HEADER_FORMULAS[i] if i < len(MONTH_HEADER_FORMULAS) else mh
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
    label = sheet_name
    a_idx = col_map.get("idx", 1)
    if a_idx:
        totals[a_idx - 1] = label
    if is_ira and pct_idx:
        totals[pct_idx - 1] = 1.0
    _write_single_row_as(doc, template_sheet, TABLE, tot_row, totals)

    # ── Tax rows (Portfolio only; not Portfolio-Cash, not IRA/ROTH sheets) ──
    write_tax = (not is_ira) if has_tax_rows is None else has_tax_rows
    if write_tax:
        _write_tax_rows_as(doc, template_sheet, TABLE, tot_row + 2)

    rename_sheet_as(doc, template_sheet, sheet_name)
    print(f"  ✓ Sheet '{sheet_name}' complete.")

    spot_check_sheet(doc, sheet_name, positions, col_map, tot_row)

    return tot_row


# ── Summary Sheet ──────────────────────────────────────────────────────────────

def find_totals_row(doc: str, sheet_name: str) -> int:
    """Return the 1-based row number of the totals row in sheet_name's 'My Portfolio' table.

    Reads column A until it finds a cell whose value starts with 'Portfolio'.
    Returns 0 if the sheet or table is not found or no matching row exists.
    """
    result = run_jxa_file(f'''(function() {{
  var app = Application("Numbers");
  var sheets = app.documents[{json.dumps(doc)}].sheets;
  var sheet = null;
  for (var i = 0; i < sheets.length; i++) {{
    if (sheets[i].name() === {json.dumps(sheet_name)}) {{ sheet = sheets[i]; break; }}
  }}
  if (!sheet) return "0";
  var tables = sheet.tables;
  var tbl = null;
  for (var j = 0; j < tables.length; j++) {{
    if (tables[j].name() === "My Portfolio") {{ tbl = tables[j]; break; }}
  }}
  if (!tbl) return "0";
  var n = tbl.rowCount();
  for (var r = 1; r <= n; r++) {{
    var v = tbl.rows[r - 1].cells[0].value();
    if (v !== null && String(v).indexOf("Portfolio") === 0) return String(r);
  }}
  return "0";
}})()''')
    try:
        return int(result)
    except (ValueError, TypeError):
        return 0


def build_summary_sheet(doc: str, tot_rows: dict, col_map: dict):
    """Create a Summary sheet positioned first, with cross-sheet refs to each portfolio sheet.

    tot_rows: {sheet_name: totals_row_number} — only for sheets that were actually built.
    col_map: the template column map, used to derive Market Value and Cost Basis column letters.
    """
    SHEET = "Summary"
    TABLE = "Portfolio Summary"

    mv_col = col_letter(col_map.get("Market Value", 11))   # K
    cb_col = col_letter(col_map.get("Cost Basis",   10))   # J

    print(f"\n  Building sheet '{SHEET}'...")

    # ── 0. Delete any existing Summary sheet so re-runs replace it cleanly ──
    run_applescript_file(f'''tell application "Numbers"
  tell document {_as_str(doc)}
    if (name of sheets) contains {_as_str(SHEET)} then
      delete sheet {_as_str(SHEET)}
    end if
  end tell
end tell''')

    # ── 1. Add sheet at position 1 ──
    # Use `at beginning` in the make command rather than a separate `move` call.
    # `move sheet X to before sheet 1` is unreliable in Numbers: if the new sheet
    # lands at position 1 by default, AppleScript errors "can't move object before
    # itself"; in other versions `before` positional specifiers for sheets aren't
    # supported at all.
    run_applescript_file(f'''tell application "Numbers"
  tell document {_as_str(doc)}
    make new sheet at beginning of sheets with properties {{name: {_as_str(SHEET)}}}
  end tell
end tell''')

    # ── 2. Rename default table ("Table 1") and resize to 8 rows × 6 cols ──
    run_applescript_file(f'''tell application "Numbers"
  tell document {_as_str(doc)}
    tell sheet {_as_str(SHEET)}
      set name of table 1 to {_as_str(TABLE)}
    end tell
  end tell
end tell''')

    run_jxa_file(f'''
var app = Application("Numbers");
var tbl = app.documents[{json.dumps(doc)}].sheets[{json.dumps(SHEET)}].tables[{json.dumps(TABLE)}];
try {{ tbl.rowCount = 9; }} catch(e) {{}}
try {{ tbl.columnCount = 6; }} catch(e) {{}}
try {{ tbl.columns[0].width = 160; }} catch(e) {{}}
try {{ tbl.columns[1].width = 120; }} catch(e) {{}}
try {{ tbl.columns[2].width = 120; }} catch(e) {{}}
try {{ tbl.columns[3].width = 120; }} catch(e) {{}}
try {{ tbl.columns[4].width = 80; }} catch(e) {{}}
try {{ tbl.columns[5].width = 80; }} catch(e) {{}}
"ok"
''')

    # ── 3. Header row ──
    _write_single_row_as(doc, SHEET, TABLE, 1,
                         ["", "Market Value", "Cost Basis", "Gain / Loss", "% Gain", "% of Total"])

    # ── 4. Data rows 2–5 (one per portfolio sheet; fixed positions) ──
    ROW_DEFS = [
        ("Portfolio",      "Brokerage"),
        ("Portfolio-Cash", "Cash & T-Bills"),
        ("Portfolio-IRA",  "IRA / 401k"),
        ("Portfolio-ROTH", "ROTH"),
    ]
    data_rows = []
    for i, (sheet_name, label) in enumerate(ROW_DEFS):
        r = i + 2   # rows 2–5
        tot = tot_rows.get(sheet_name)
        if tot is not None:
            # Single-quote sheet names so hyphens in names are handled correctly by Numbers
            mv_ref = f"=ROUND('{sheet_name}'::My Portfolio::{mv_col}{tot},2)"
            cb_ref = f"=ROUND('{sheet_name}'::My Portfolio::{cb_col}{tot},2)"
            row = [
                label,
                mv_ref,
                cb_ref,
                f"=ROUND(B{r}-C{r},2)",
                f'=IFERROR(ROUND(D{r}/C{r},4),"–")',
                f'=IFERROR(ROUND(B{r}/B$7,4),"–")',   # B$7 = Total MV (row 7)
            ]
        else:
            row = [label, "", "", "", "", ""]
        data_rows.append(row)
    _write_rows_as_batch(doc, SHEET, TABLE, 2, data_rows)

    # ── 5. Totals row (row 7; row 6 is a blank visual separator) ──
    _write_single_row_as(doc, SHEET, TABLE, 7, [
        "Total",
        "=ROUND(SUM(B2:B5),2)",
        "=ROUND(SUM(C2:C5),2)",
        "=ROUND(SUM(D2:D5),2)",
        '=IFERROR(ROUND(D7/C7,4),"–")',
        "=1",   # always 100%
    ])

    # ── 6 & 7. Formatting + instruction cell ──
    # Use simple `set format of cell to currency / percent` — these keywords
    # work without -2740 errors. Decimal places are controlled by ROUND() in
    # the formulas above (2dp for currency, 4dp stored = 2dp displayed as %).
    try:
        run_applescript_file(f'''tell application "Numbers"
  tell document {_as_str(doc)}
    tell sheet {_as_str(SHEET)}
      tell table {_as_str(TABLE)}
        -- Currency format for cols B, C, D (rows 2-7)
        repeat with r from 2 to 7
          set format of cell 2 of row r to currency
          set format of cell 3 of row r to currency
          set format of cell 4 of row r to currency
        end repeat
        -- Percent format for cols E, F (rows 2-7)
        repeat with r from 2 to 7
          set format of cell 5 of row r to percent
          set format of cell 6 of row r to percent
        end repeat
        -- Instruction cell
        set value of cell 1 of row 9 to "To add pie chart: select A1:B5 \u2192 Insert \u2192 Chart \u2192 Pie"
      end tell
    end tell
  end tell
end tell''')
    except RuntimeError as e:
        print(f"  WARNING: Could not apply Summary formatting: {e}")

    print(f"  ✓ Sheet '{SHEET}' complete.")
    print(f"  📊 Pie chart: open Numbers → Summary sheet → select A1:B5 → Insert → Chart → Pie (2D)")
    print(f"     (Takes ~5 seconds; only needed once — the chart will reference live data after that.)")


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
    parser.add_argument("csv_file", nargs="?",
                        help="Fidelity positions CSV (required unless --summary-only is used)")
    parser.add_argument("--template", default="Portfolio Template.numbers")
    parser.add_argument("--doc-name")
    parser.add_argument("--summary-only", action="store_true",
                        help="Rebuild the Summary sheet on an already-open document; "
                             "--doc-name is required")
    parser.add_argument("--output-dir", default=DESKTOP_DIR,
                        help=f"Directory to save the output .numbers file (default: ~/Desktop)")
    parser.add_argument("--brokerage-only", action="store_true",
                        help="Build Portfolio + Portfolio-Cash (both brokerage sheets)")
    parser.add_argument("--equity-only", action="store_true",
                        help="Build Portfolio (brokerage equities) only")
    parser.add_argument("--cash-only", action="store_true",
                        help="Build Portfolio-Cash only")
    parser.add_argument("--ira-only", action="store_true")
    parser.add_argument("--roth-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse CSV and report; do not open or modify Numbers")
    parser.add_argument("--no-dividend-fill", action="store_true",
                        help="Skip the Claude API dividend gap-fill step")
    args = parser.parse_args()

    # ── --summary-only: rebuild Summary sheet on an already-open document ──
    if args.summary_only:
        if not args.doc_name:
            parser.error("--summary-only requires --doc-name (the document must already be open in Numbers)")
        doc_name = args.doc_name
        print(f"--summary-only: rebuilding Summary sheet in '{doc_name}'...")
        SHEET_NAMES = ["Portfolio", "Portfolio-Cash", "Portfolio-IRA", "Portfolio-ROTH"]
        tot_rows: dict = {}
        for sn in SHEET_NAMES:
            row = find_totals_row(doc_name, sn)
            if row:
                tot_rows[sn] = row
                print(f"  {sn}: totals row {row}")
            else:
                print(f"  {sn}: not found (skipped)")
        build_summary_sheet(doc_name, tot_rows, {})
        return

    if not args.csv_file:
        parser.error("csv_file is required (or use --summary-only to rebuild the Summary sheet only)")

    doc_name      = args.doc_name or derive_doc_name(args.csv_file)
    month_headers = derive_month_headers(args.csv_file)
    output_dir    = os.path.expanduser(args.output_dir)

    # Which sheets to build.
    # --brokerage-only builds both brokerage sheets; --equity-only / --cash-only select one.
    # Each flag excludes non-matching sheets; no flags = all four.
    build_equity = not (args.ira_only or args.roth_only or args.cash_only)
    build_cash   = not (args.ira_only or args.roth_only or args.equity_only)
    build_ira    = not (args.brokerage_only or args.equity_only or args.cash_only or args.roth_only)
    build_roth   = not (args.brokerage_only or args.equity_only or args.cash_only or args.ira_only)

    # ── Template + _basis (skipped in dry-run to avoid starting Numbers) ──
    basis_overrides: dict = {}
    col_map = headers = formula_row = num_cols = col_widths = None
    template_path = _tmpl_doc_name = None

    if not args.dry_run:
        print(f"Reading template '{args.template}'...")
        _tmpl_doc_name, col_map, headers, formula_row, num_cols, col_widths = read_template(args.template)
        template_path = _resolve_template_path(args.template)
        print(f"  Template: '{template_path}'")
        print(f"  {num_cols} columns, {len(col_map)} named headers")

        basis_overrides = read_basis_overrides(_tmpl_doc_name)
        n_ov = len(basis_overrides)
        if n_ov:
            print(f"  {n_ov} cost basis override(s) loaded from _basis sheet")

    # ── Parse CSV ──
    print(f"\nParsing {args.csv_file}...")
    all_positions = parse_csv(args.csv_file, basis_overrides)
    print(f"  {len(all_positions)} rows parsed")

    # ── Account-to-sheet assignment ──
    brokerage_equity_accs: list = []
    brokerage_cash_accs:   list = []
    ira_accs:              list = []
    roth_accs:             list = []
    for p in all_positions:
        acc = p["account_name"]
        if p["bucket"] == "BROKERAGE":
            if is_cash_position(p):
                if acc not in brokerage_cash_accs:   brokerage_cash_accs.append(acc)
            else:
                if acc not in brokerage_equity_accs: brokerage_equity_accs.append(acc)
        elif p["bucket"] == "IRA":
            if acc not in ira_accs:   ira_accs.append(acc)
        elif p["bucket"] == "ROTH":
            if acc not in roth_accs:  roth_accs.append(acc)

    print("\nAccount buckets:")
    if brokerage_equity_accs:
        print(f"  {'BROKERAGE equity':<16}: {', '.join(brokerage_equity_accs)}")
    if brokerage_cash_accs:
        print(f"  {'BROKERAGE cash':<16}: {', '.join(brokerage_cash_accs)}")
    if ira_accs:
        print(f"  {'IRA':<16}: {', '.join(ira_accs)}")
    if roth_accs:
        print(f"  {'ROTH':<16}: {', '.join(roth_accs)}")

    print(f"\nDocument: '{doc_name}'")
    print(f"Output:   {output_dir}")
    print(f"Month columns: {', '.join(month_headers)}")

    # ── Dry run ──
    if args.dry_run:
        brokerage_all = aggregate_positions(all_positions, "BROKERAGE")
        brok_eq   = [p for p in brokerage_all if not is_cash_position(p)]
        brok_cash = [p for p in brokerage_all if is_cash_position(p)]
        ira_list  = aggregate_positions(all_positions, "IRA")
        roth_list = aggregate_positions(all_positions, "ROTH")
        print()
        if build_equity: print(f"[dry-run] Portfolio:      {len(brok_eq)} positions")
        if build_cash:   print(f"[dry-run] Portfolio-Cash: {len(brok_cash)} positions")
        if build_ira:    print(f"[dry-run] Portfolio-IRA:  {len(ira_list)} positions")
        if build_roth:   print(f"[dry-run] Portfolio-ROTH: {len(roth_list)} positions")
        print(f"[dry-run] Summary:        (skipped — no sheets to reference in dry-run)")
        return

    # ── Close any existing output document ──
    print("\nPreparing document...")
    for d in list_documents_as():
        if d == doc_name or re.match(re.escape(doc_name) + r"( \(\d+\))?(.numbers)?$", d):
            close_document_as(d)
            print(f"  Closed existing '{d}'")

    print(f"Creating '{doc_name}' from template...")
    actual_doc = setup_output_document(template_path, doc_name, output_dir)
    print(f"  ✓ Opened: '{actual_doc}'")

    # ── Template sheet queue ──
    # TEMPLATE_SHEET_RE only matches _templateN sheets; _basis is excluded automatically.
    all_sheets = list_sheets_as(actual_doc)
    available_templates = sorted(
        [s for s in all_sheets if TEMPLATE_SHEET_RE.match(s)],
        key=lambda s: int(TEMPLATE_SHEET_RE.match(s).group(1)),
    )
    template_queue = list(available_templates)

    summary: dict = {}
    sheet_positions_for_divs: dict = {}

    def next_template() -> str:
        if not template_queue:
            sys.exit("ERROR: Ran out of template sheets. Add more _templateN sheets to the template.")
        return template_queue.pop(0)

    # ── Brokerage split: Portfolio (equities) + Portfolio-Cash ──
    brokerage_all = aggregate_positions(all_positions, "BROKERAGE")
    brok_equity   = [p for p in brokerage_all if not is_cash_position(p)]
    brok_cash     = [p for p in brokerage_all if is_cash_position(p)]
    for pos in brok_cash:
        pos["is_cash_instrument"] = True          # signals build_data_row for cash overrides
    brok_cash_ordered = order_cash_positions(brok_cash)

    tot_rows: dict = {}  # sheet_name → totals row number; passed to build_summary_sheet

    if build_equity:
        tmpl = next_template()
        tot_rows["Portfolio"] = build_sheet(
                    actual_doc, "Portfolio", brok_equity,
                    col_map, list(headers), list(formula_row),
                    num_cols, col_widths, month_headers,
                    is_ira=False, has_tax_rows=True, template_sheet=tmpl)
        summary["Portfolio"] = {
            "positions":    len(brok_equity),
            "market_value": sum(p["current_value"] for p in brok_equity),
            "cost_basis":   sum(p["cost_basis_total"] or 0 for p in brok_equity),
        }
        sheet_positions_for_divs["Portfolio"] = [(i + 2, p) for i, p in enumerate(brok_equity)]

    if build_cash:
        tmpl = next_template()
        tot_rows["Portfolio-Cash"] = build_sheet(
                    actual_doc, "Portfolio-Cash", brok_cash_ordered,
                    col_map, list(headers), list(formula_row),
                    num_cols, col_widths, month_headers,
                    is_ira=False, has_tax_rows=False, template_sheet=tmpl)
        summary["Portfolio-Cash"] = {
            "positions":    len(brok_cash_ordered),
            "market_value": sum(p["current_value"] for p in brok_cash_ordered),
            "cost_basis":   sum(p["cost_basis_total"] or 0 for p in brok_cash_ordered),
        }
        # Portfolio-Cash holds T-Bills and money markets — no dividend fill needed

    if build_ira:
        ira = aggregate_positions(all_positions, "IRA")
        tmpl = next_template()
        tot_rows["Portfolio-IRA"] = build_sheet(
                    actual_doc, "Portfolio-IRA", ira,
                    col_map, list(headers), list(formula_row),
                    num_cols, col_widths, month_headers,
                    is_ira=True, template_sheet=tmpl)
        summary["Portfolio-IRA"] = {
            "positions":    len(ira),
            "market_value": sum(p["current_value"] for p in ira),
            "cost_basis":   sum(p["cost_basis_total"] or 0 for p in ira),
        }
        sheet_positions_for_divs["Portfolio-IRA"] = [(i + 2, p) for i, p in enumerate(ira)]

    if build_roth:
        roth = aggregate_positions(all_positions, "ROTH")
        tmpl = next_template()
        tot_rows["Portfolio-ROTH"] = build_sheet(
                    actual_doc, "Portfolio-ROTH", roth,
                    col_map, list(headers), list(formula_row),
                    num_cols, col_widths, month_headers,
                    is_ira=True, template_sheet=tmpl)
        summary["Portfolio-ROTH"] = {
            "positions":    len(roth),
            "market_value": sum(p["current_value"] for p in roth),
            "cost_basis":   sum(p["cost_basis_total"] or 0 for p in roth),
        }
        sheet_positions_for_divs["Portfolio-ROTH"] = [(i + 2, p) for i, p in enumerate(roth)]

    # ── Delete unused template sheets (_template5, _template6, ...) ──
    # _basis is not in template_queue (TEMPLATE_SHEET_RE requires a digit suffix) — safe to skip.
    for unused in template_queue:
        print(f"  Deleting unused template sheet '{unused}'...")
        delete_sheet_as(actual_doc, unused)

    # ── Summary sheet (cross-sheet refs; must be built after all portfolio sheets are named) ──
    try:
        build_summary_sheet(actual_doc, tot_rows, col_map)
    except Exception as e:
        print(f"\n  ⚠ Summary sheet could not be created: {e}")

    # ── Dividend gap-fill (Portfolio-Cash excluded — no dividends on T-Bills/MMs) ──
    if not args.no_dividend_fill:
        fill_dividends(actual_doc, sheet_positions_for_divs, col_map)

    print("\n" + "=" * 68)
    print("Summary:")
    for sheet_name, t in summary.items():
        gain = t["market_value"] - t["cost_basis"]
        print(
            f"  {sheet_name:<16} {t['positions']:>3} positions  "
            f"MV: ${t['market_value']:>12,.0f}  "
            f"Cost: ${t['cost_basis']:>12,.0f}  "
            f"Gain: ${gain:>12,.0f}"
        )
    print(f"\nDocument: '{actual_doc}'")
    print(f"Saved to: {output_dir}")


if __name__ == "__main__":
    main()
