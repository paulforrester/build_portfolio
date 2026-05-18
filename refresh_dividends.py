#!/usr/bin/env python3
"""Refresh dividend data in an Apple Numbers portfolio document.

Replicates dividend-refresher.html but runs natively on macOS using
AppleScript/JXA instead of NumBridge, and requests instead of fetch.

Usage:
    python3 refresh_dividends.py --doc "Portfolio May 2026"
    python3 refresh_dividends.py --doc "Portfolio May 2026" --force
    python3 refresh_dividends.py --doc "Portfolio May 2026" --preview
    python3 refresh_dividends.py --doc "Portfolio May 2026" --no-claude
    python3 refresh_dividends.py --doc "Portfolio May 2026" --amounts-only

Configuration (env vars or ~/.dividend_refresher/config.json):
    AV_KEY             Alpha Vantage API key  (1st source, 25 req/day)
    FMP_KEY            FMP API key            (2nd source, 250 req/day)
    ANTHROPIC_API_KEY  Anthropic API key      (4th fallback, web search)

Dependencies:
    pip install requests anthropic
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("ERROR: requests is required.  pip install requests")

try:
    import yfinance as yf  # Yahoo source — pip install yfinance
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────

MODEL        = "claude-sonnet-4-6"
TOKEN_LIMIT  = 30_000
WINDOW_S     = 60
HEADROOM     = 0.95
OUTLIER_CAP  = 25_000
MAX_RETRIES  = 4
AV_MIN_S     = 13.0   # 5 req/min → 12 s apart + buffer

MONEY_MARKETS = {"SPAXX", "FZDXX", "FZROX", "FZILX", "FCASH"}
TABLE         = "My Portfolio"


# ── AppleScript / JXA runners ──────────────────────────────────────────────────

def run_applescript_file(script: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".applescript",
                                     delete=False, encoding="utf-8") as f:
        f.write(script)
        tmp = f.name
    try:
        result = subprocess.run(["osascript", tmp],
                                capture_output=True, text=True, encoding="utf-8")
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
        result = subprocess.run(["osascript", "-l", "JavaScript", tmp],
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        return result.stdout.strip()
    finally:
        os.unlink(tmp)


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    config: dict = {}
    cfg_path = Path.home() / ".dividend_refresher" / "config.json"
    if cfg_path.exists():
        try:
            with open(cfg_path) as f:
                config = json.load(f)
        except Exception as e:
            print(f"WARNING: Could not read {cfg_path}: {e}", file=sys.stderr)
    # Env vars override config file
    config["av_key"]            = os.environ.get("AV_KEY")            or config.get("av_key", "")
    config["fmp_key"]           = os.environ.get("FMP_KEY")           or config.get("fmp_key", "")
    config["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY") or config.get("anthropic_api_key", "")
    return config


# ── CUSIP detection ────────────────────────────────────────────────────────────

def is_cusip(sym: str) -> bool:
    if not sym:
        return False
    return bool(
        re.match(r"^\d{9}[A-Z]\d$", sym)
        or re.match(r"^\d{6}[A-Z]{2}\d$", sym)
        or (re.match(r"^\d+[A-Z]+\d+$", sym) and len(sym) >= 8)
    )


# ── Numbers helpers (JXA) ──────────────────────────────────────────────────────

def get_portfolio_sheets(doc_name: str) -> list:
    """Return sheet names starting with 'Portfolio', excluding 'Portfolio-Cash'."""
    script = f"""
ObjC.import('Foundation');
const app = Application("Numbers");
const docs = app.documents.whose({{name: {{_equals: {json.dumps(doc_name)}}}}});
if (docs.length === 0) throw new Error("Document not found: " + {json.dumps(doc_name)});
const names = docs[0].sheets().map(s => s.name());
JSON.stringify(names);
"""
    raw = run_jxa_file(script)
    all_sheets = json.loads(raw)
    return [s for s in all_sheets
            if s.startswith("Portfolio") and s != "Portfolio-Cash"]


def read_col_map(doc_name: str, sheet_name: str) -> dict:
    """Read header row and return 0-based column index map."""
    script = f"""
ObjC.import('Foundation');
const app = Application("Numbers");
const doc = app.documents.whose({{name: {{_equals: {json.dumps(doc_name)}}}}});
if (!doc.length) throw new Error("Document not found");
const sheet = doc[0].sheets.whose({{name: {{_equals: {json.dumps(sheet_name)}}}}});
if (!sheet.length) throw new Error("Sheet not found");
const tbl = sheet[0].tables.whose({{name: {{_equals: {json.dumps(TABLE)}}}}});
if (!tbl.length) throw new Error("Table not found");
const t = tbl[0];
const n = t.columnCount();
const headers = [];
for (let c = 0; c < n; c++) {{
    try {{ headers.push(String(t.rows[0].cells[c].value() || "")); }}
    catch(e) {{ headers.push(""); }}
}}
JSON.stringify(headers);
"""
    headers = json.loads(run_jxa_file(script))

    def find(*names):
        for name in names:
            for i, h in enumerate(headers):
                if h and name.lower() in h.lower():
                    return i
        return -1

    divs_idx   = find("divs/year", "divs year")
    month_start = divs_idx + 1 if divs_idx >= 0 else max(0, len(headers) - 4)

    col_map = {
        "_num_cols":   len(headers),
        "_headers":    headers,
        "SYMBOL":      find("symbol"),
        "SHARES":      find("shares"),
        "ANN_DIV":     find("ann div / sh", "ann div/sh"),
        "EX_DIV":      find("ex-div date", "ex-div"),
        "PAY_DATE":    find("div pay date", "pay date"),
        "DIVS_YEAR":   find("divs/year"),
        "MONTH_START": month_start,
    }
    print(f"  Col map — Symbol:{col_map['SYMBOL']+1} Shares:{col_map['SHARES']+1} "
          f"AnnDiv:{col_map['ANN_DIV']+1} ExDiv:{col_map['EX_DIV']+1} "
          f"PayDate:{col_map['PAY_DATE']+1} DivsYear:{col_map['DIVS_YEAR']+1} "
          f"MonthStart:{col_map['MONTH_START']+1}")
    return col_map


def read_positions(doc_name: str, sheet_name: str, col_map: dict) -> list:
    """Batch-read all equity rows from the sheet via a single JXA call."""
    sym_col  = col_map["SYMBOL"]
    shr_col  = col_map["SHARES"]
    adiv_col = col_map["ANN_DIV"]
    exd_col  = col_map["EX_DIV"]
    pay_col  = col_map["PAY_DATE"]
    dpyr_col = col_map["DIVS_YEAR"]

    script = f"""
ObjC.import('Foundation');
const app = Application("Numbers");
const doc = app.documents.whose({{name: {{_equals: {json.dumps(doc_name)}}}}});
if (!doc.length) throw new Error("Document not found");
const sheet = doc[0].sheets.whose({{name: {{_equals: {json.dumps(sheet_name)}}}}});
if (!sheet.length) throw new Error("Sheet not found");
const tbl = sheet[0].tables.whose({{name: {{_equals: {json.dumps(TABLE)}}}}});
if (!tbl.length) throw new Error("Table not found");
const t = tbl[0];
const nRows = t.rowCount();
const SYM  = {sym_col};
const SHR  = {shr_col};
const ADIV = {adiv_col};
const EXD  = {exd_col};
const PAY  = {pay_col};
const DPYR = {dpyr_col};
const getV = (cells, i) => {{
    if (i < 0) return "";
    try {{
        const v = cells[i].value();
        if (v === null || v === undefined) return "";
        if (v instanceof Date) return v.toISOString().split("T")[0];
        return String(v);
    }} catch(e) {{ return ""; }}
}};
const rows = [];
for (let r = 1; r < nRows; r++) {{  // 0-based: row index 0 = header, skip it
    const cells = t.rows[r].cells;
    const sym = SYM >= 0 ? getV(cells, SYM).trim() : "";
    if (!sym || sym === "Portfolio" || sym.startsWith("Portfolio")) break;
    rows.push({{
        row:           r + 1,  // 1-based Numbers row number
        symbol:        sym,
        shares:        getV(cells, SHR),
        ann_div:       getV(cells, ADIV),
        ex_div_date:   getV(cells, EXD),
        pay_date:      getV(cells, PAY),
        divs_per_year: getV(cells, DPYR),
    }});
}}
JSON.stringify(rows);
"""
    raw_rows = json.loads(run_jxa_file(script))
    # Filter out CUSIPs and money markets (display symbols may be "SPAXX - Account Name")
    def _skip(sym: str) -> bool:
        if is_cusip(sym):
            return True
        su = sym.upper()
        if su in MONEY_MARKETS:
            return True
        for mm in MONEY_MARKETS:
            if su.startswith(mm + " ") or su.startswith(mm + "-"):
                return True
        return False

    return [r for r in raw_rows if not _skip(r["symbol"])]


def write_ticker(doc_name: str, sheet_name: str, row: int,
                 col_map: dict, data: dict, months: list) -> None:
    """Write dividend fields back to a Numbers row via a single JXA call."""
    adiv_col = col_map["ANN_DIV"]
    exd_col  = col_map["EX_DIV"]
    pay_col  = col_map["PAY_DATE"]
    dpyr_col = col_map["DIVS_YEAR"]
    mstart   = col_map["MONTH_START"]

    # Build (0-based-col, value) pairs
    writes: list = []
    if data.get("new_ann_div") is not None and adiv_col >= 0:
        writes.append((adiv_col, data["new_ann_div"]))
    if data.get("new_divs_py") is not None and dpyr_col >= 0:
        writes.append((dpyr_col, data["new_divs_py"]))
    if data.get("new_ex_div") and exd_col >= 0:
        writes.append((exd_col, data["new_ex_div"]))
    if data.get("new_pay_date") and pay_col >= 0:
        writes.append((pay_col, data["new_pay_date"]))
    for i, month in enumerate(months):
        val = data.get("month_amounts", {}).get(month["name"])
        if mstart >= 0:
            writes.append((mstart + i, val if val is not None else ""))

    if not writes:
        return

    # Build JXA assignment statements
    cmds = []
    row_idx = row - 1  # 0-based
    for col_0, val in writes:
        if val == "" or val is None:
            cmds.append(f"t.rows[{row_idx}].cells[{col_0}].value = '';")
        elif isinstance(val, (int, float)):
            cmds.append(f"t.rows[{row_idx}].cells[{col_0}].value = {val};")
        elif isinstance(val, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", val):
            # Noon UTC to avoid timezone day-shift (mirrors HTML T12:00:00 trick)
            cmds.append(f"t.rows[{row_idx}].cells[{col_0}].value = new Date('{val}T12:00:00Z');")
        else:
            safe = str(val).replace("\\", "\\\\").replace("'", "\\'")
            cmds.append(f"t.rows[{row_idx}].cells[{col_0}].value = '{safe}';")

    cmds_str = "\n".join(cmds)
    script = f"""
ObjC.import('Foundation');
const app = Application("Numbers");
const doc = app.documents.whose({{name: {{_equals: {json.dumps(doc_name)}}}}});
if (!doc.length) throw new Error("Document not found");
const sheet = doc[0].sheets.whose({{name: {{_equals: {json.dumps(sheet_name)}}}}});
if (!sheet.length) throw new Error("Sheet not found");
const tbl = sheet[0].tables.whose({{name: {{_equals: {json.dumps(TABLE)}}}}});
if (!tbl.length) throw new Error("Table not found");
const t = tbl[0];
{cmds_str}
"ok";
"""
    run_jxa_file(script)


def sort_sheet(doc_name: str, sheet_name: str, pay_date_col: int) -> None:
    """Sort My Portfolio by pay date (1-based col) ascending via AppleScript."""
    # Numbers AppleScript "sort by column N" sorts ascending by default.
    # "in order ascending" triggers a -2741 syntax error in this context.
    script = f"""
tell application "Numbers"
    tell document {json.dumps(doc_name)}
        tell sheet {json.dumps(sheet_name)}
            tell table {json.dumps(TABLE)}
                sort by column {pay_date_col}
            end tell
        end tell
    end tell
end tell
"""
    run_applescript_file(script)


# ── Rolling months ─────────────────────────────────────────────────────────────

def get_rolling_months() -> list:
    """4 rolling months starting from the current month."""
    result = []
    now = datetime.now()
    for i in range(4):
        month_num = (now.month - 1 + i) % 12 + 1
        year = now.year + ((now.month - 1 + i) // 12)
        name = datetime(year, month_num, 1).strftime("%B %Y")
        result.append({"name": name, "index": i})
    return result


# ── Shared helpers ─────────────────────────────────────────────────────────────

def parse_num(v) -> float:
    try:
        return float(str(v or "0").replace("$", "").replace(",", "").strip()) or 0.0
    except Exception:
        return 0.0


def norm_date(s) -> Optional[str]:
    """Normalise to YYYY-MM-DD or None."""
    if not s or str(s) in ("None", "0000-00-00", "–", "-"):
        return None
    try:
        if isinstance(s, (int, float)):
            return datetime.fromtimestamp(float(s), tz=timezone.utc).strftime("%Y-%m-%d")
        s = str(s)
        if "T" in s:
            s = s.split("T")[0]
        datetime.fromisoformat(s)  # validate
        return s
    except Exception:
        return None


def infer_frequency(annual_rate: float, single_payment: float) -> int:
    if not annual_rate or not single_payment or annual_rate <= 0 or single_payment <= 0:
        return 4
    ratio = annual_rate / single_payment
    if   11   <= ratio <= 13:   return 12
    elif 3.5  <= ratio <= 4.5:  return 4
    elif 1.75 <= ratio <= 2.25: return 2
    elif 0.8  <= ratio <= 1.2:  return 1
    return 4


def result_usable(r: Optional[dict]) -> bool:
    if not r:
        return False
    today_str = date.today().isoformat()
    fom_str   = date.today().replace(day=1).isoformat()
    ex_ok  = r.get("ex_div")   and r["ex_div"]   >= today_str
    pay_ok = r.get("pay_date") and r["pay_date"]  >= fom_str
    div_ok = (r.get("ann_div_per_share") or 0) > 0
    return bool(ex_ok or pay_ok or div_ok)


def compute_month_amounts_from_values(ann_div_per_share, divs_per_year,
                                      pay_date: Optional[str],
                                      shares, months: list) -> dict:
    """Exact port of HTML computeMonthAmountsFromValues. noon trick avoids TZ shift."""
    dpy    = int(divs_per_year) if divs_per_year else 4
    adp    = parse_num(ann_div_per_share)
    result = {m["name"]: None for m in months}
    if not adp:
        return result
    amt = round(adp / dpy * parse_num(shares), 2)
    if dpy == 12:
        for m in months:
            result[m["name"]] = amt
    elif pay_date:
        try:
            d = datetime.fromisoformat(pay_date + "T12:00:00")  # local noon
            k = d.strftime("%B %Y")
            if k in result:
                result[k] = amt
        except Exception:
            pass
    return result


def sheet_label(sheet: str) -> str:
    if sheet == "Portfolio":
        return "Brokerage"
    return re.sub(r"^Portfolio[-\s]*", "", sheet, flags=re.IGNORECASE) or sheet


# ── Token-aware rate limiter ───────────────────────────────────────────────────

_token_log:     list = []
_recent_tokens: list = []
_token_estimate: float = 20_000.0


def _token_log_clean():
    cutoff = time.time() - WINDOW_S
    while _token_log and _token_log[0]["ts"] < cutoff:
        _token_log.pop(0)


def _tokens_used_in_window() -> float:
    _token_log_clean()
    return sum(e["tokens"] for e in _token_log)


def _record_tokens(tokens: int):
    global _token_estimate
    _token_log.append({"ts": time.time(), "tokens": tokens})
    if tokens <= OUTLIER_CAP:
        _recent_tokens.append(tokens)
        if len(_recent_tokens) > 8:
            _recent_tokens.pop(0)
        s = sorted(_recent_tokens)
        mid = len(s) // 2
        median = (s[mid-1] + s[mid]) // 2 if len(s) % 2 == 0 else s[mid]
        _token_estimate = round(median * 1.1)
    else:
        print(f"  [Claude] outlier call: {tokens} tokens — excluded from estimate")


def wait_for_token_budget(ticker: str):
    budget = TOKEN_LIMIT * HEADROOM
    while True:
        _token_log_clean()
        used      = _tokens_used_in_window()
        available = budget - used
        if available >= _token_estimate:
            return
        ms_to_wait = None
        running_used = used
        for entry in _token_log:
            running_used -= entry["tokens"]
            if budget - running_used >= _token_estimate:
                ms_to_wait = (entry["ts"] + WINDOW_S) - time.time() + 0.2
                break
        if not ms_to_wait or ms_to_wait <= 0:
            return
        print(f"  {ticker}: pacing ({int(used)}/{int(budget)} tokens, "
              f"est {int(_token_estimate)}/call) — waiting {ms_to_wait:.1f}s")
        time.sleep(ms_to_wait)


# ── Alpha Vantage throttle ─────────────────────────────────────────────────────

_av_last_call: float = 0.0


def _av_throttle(label: str):
    global _av_last_call
    wait = AV_MIN_S - (time.time() - _av_last_call)
    if _av_last_call > 0 and wait > 0:
        print(f"  {label} [AV]: throttling {wait:.1f}s")
        time.sleep(wait)
    _av_last_call = time.time()


# ── Source 1: Alpha Vantage ────────────────────────────────────────────────────

def fetch_from_alpha_vantage(ticker: str, av_key: str) -> Optional[dict]:
    if not av_key:
        return None
    try:
        _av_throttle(ticker)
        resp = requests.get("https://www.alphavantage.co/query",
                            params={"function": "OVERVIEW", "symbol": ticker, "apikey": av_key},
                            timeout=15)
        if not resp.ok:
            return None
        o = resp.json()
        if not o.get("Symbol") or o.get("Note") or o.get("Information"):
            if o.get("Note") or o.get("Information"):
                print(f"  {ticker} [AV]: quota/rate limit hit")
            return None

        ann_div = (
            float(o.get("ForwardAnnualDividendRate") or 0)
            or float(o.get("DividendPerShare") or 0)
            or None
        )
        ex_div   = norm_date(o.get("ExDividendDate"))
        pay_date = None
        if ex_div:
            try:
                pay_date = norm_date((datetime.fromisoformat(ex_div) + timedelta(days=14)).isoformat())
            except Exception:
                pass
        freq = None

        _av_throttle(ticker)
        dresp = requests.get("https://www.alphavantage.co/query",
                             params={"function": "DIVIDENDS", "symbol": ticker, "apikey": av_key},
                             timeout=15)
        if dresp.ok:
            history = dresp.json().get("data", [])
            if history:
                today_str = date.today().isoformat()
                upcoming  = next((e for e in history if e.get("ex_dividend_date", "") >= today_str), None)
                best      = upcoming or history[0]
                if best.get("ex_dividend_date"):
                    ex_div   = norm_date(best["ex_dividend_date"])
                if best.get("payment_date"):
                    pay_date = norm_date(best["payment_date"])
                if ann_div and best.get("amount"):
                    freq = infer_frequency(ann_div, float(best["amount"]))

        if not ann_div and not ex_div:
            return None
        return {"ex_div": ex_div, "pay_date": pay_date,
                "ann_div_per_share": ann_div, "divs_per_year": freq or 4,
                "source": "alphavantage"}
    except Exception as e:
        print(f"  {ticker} [AV]: {e}")
        return None


# ── Source 2: FMP ──────────────────────────────────────────────────────────────

def fetch_from_fmp(ticker: str, fmp_key: str) -> Optional[dict]:
    if not fmp_key:
        return None
    try:
        presp = requests.get(
            f"https://financialmodelingprep.com/api/v3/profile/{ticker}",
            params={"apikey": fmp_key}, timeout=15)
        if not presp.ok:
            return None
        profiles = presp.json()
        if not profiles or not isinstance(profiles, list):
            return None
        p = profiles[0]
        if p.get("Error Message") or p.get("error"):
            return None

        dresp = requests.get(
            f"https://financialmodelingprep.com/api/v3/historical-price-full/stock_dividend/{ticker}",
            params={"apikey": fmp_key}, timeout=15)
        if not dresp.ok:
            return None
        history = dresp.json().get("historical", [])
        if not history:
            return None

        today_str = date.today().isoformat()
        fom_str   = date.today().replace(day=1).isoformat()
        upcoming  = next((e for e in history if e.get("date", "") >= today_str), None)
        recent    = next((e for e in history if (e.get("paymentDate") or "") >= fom_str), None)
        best      = upcoming or recent or history[0]

        ex_div   = norm_date(best.get("date"))
        pay_date = norm_date(best.get("paymentDate"))
        div_amt  = float(best.get("dividend") or 0)

        annual_div = None
        if div_amt > 0 and len(history) >= 2:
            one_year_ago = (date.today() - timedelta(days=365)).isoformat()
            year_payments = [e for e in history if e.get("date", "") >= one_year_ago]
            if year_payments:
                annual_div = round(sum(float(e.get("dividend") or 0) for e in year_payments), 2)

        freq = infer_frequency(annual_div, div_amt) if annual_div else 4

        if not annual_div and not ex_div:
            return None
        return {"ex_div": ex_div, "pay_date": pay_date,
                "ann_div_per_share": annual_div, "divs_per_year": freq,
                "source": "fmp"}
    except Exception as e:
        print(f"  {ticker} [FMP]: {e}")
        return None


# ── Source 3: Yahoo Finance (via yfinance) ────────────────────────────────────
# yfinance handles crumb/cookie authentication that the raw API now requires.
# Falls through silently when yfinance is not installed.

def fetch_from_yahoo(ticker: str) -> Optional[dict]:
    if not _YF_AVAILABLE:
        return None
    try:
        t    = yf.Ticker(ticker)
        info = t.info or {}

        # Forward annual div rate; fall back to trailing if not available
        ann_div = (info.get("dividendRate")
                   or info.get("trailingAnnualDividendRate")
                   or None)

        # Calendar gives both dates as date objects (most accurate)
        ex_div   = None
        pay_date = None
        try:
            cal      = t.calendar or {}
            ex_d     = cal.get("Ex-Dividend Date")
            pay_d    = cal.get("Dividend Date")
            ex_div   = str(ex_d)  if ex_d  else None
            pay_date = str(pay_d) if pay_d else None
        except Exception:
            pass

        # Fallback ex-div from info (Unix timestamp)
        if not ex_div:
            ex_ts  = info.get("exDividendDate")
            ex_div = norm_date(ex_ts) if ex_ts else None

        # Infer frequency from dividend history; also compute annual if still missing
        freq = 4
        try:
            divs = t.dividends
            if len(divs) >= 2:
                last_amt = float(divs.iloc[-1])
                if ann_div:
                    freq = infer_frequency(ann_div, last_amt)
                else:
                    # Sum last 12 months of payments
                    cutoff = (date.today() - timedelta(days=365)).isoformat()
                    recent = [float(v) for d, v in divs.items()
                              if str(d.date()) >= cutoff]
                    if recent:
                        ann_div = round(sum(recent), 4)
                        freq    = infer_frequency(ann_div, last_amt)
        except Exception:
            pass

        if not ann_div and not ex_div:
            return None
        return {"ex_div": ex_div, "pay_date": pay_date,
                "ann_div_per_share": ann_div, "divs_per_year": freq,
                "source": "yahoo"}
    except Exception as e:
        print(f"  {ticker} [Yahoo]: {e}")
        return None


# ── Source 4: Claude API ───────────────────────────────────────────────────────

def fetch_from_claude(ticker: str, api_key: str) -> Optional[dict]:
    if not api_key:
        return None

    wait_for_token_budget(ticker)

    today   = date.today().isoformat()
    headers = {"Content-Type": "application/json",
               "x-api-key": api_key,
               "anthropic-version": "2023-06-01"}
    body = {
        "model": MODEL,
        "max_tokens": 500,
        "system": ("You are a financial data assistant. Use web search to find current dividend "
                   "information. Return ONLY a raw JSON object, no markdown, no preamble."),
        "messages": [{"role": "user", "content":
            f"Find current dividend information for {ticker} as of {today}. "
            "Search for: annual dividend per share in USD, payments per year, "
            "next ex-dividend date, next pay date. "
            "The pay date may be earlier in the current month — include if it falls in the current month. "
            'Return ONLY: {"ann_div_per_share": number_or_null, "divs_per_year": integer_or_null, '
            '"ex_div_date": "YYYY-MM-DD_or_null", "pay_date": "YYYY-MM-DD_or_null"}'
        }],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }

    retry_delay = 10.0
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.post("https://api.anthropic.com/v1/messages",
                             headers=headers, json=body, timeout=60)
        if resp.status_code in (429, 529):
            reason = "Overloaded" if resp.status_code == 529 else "Rate limited"
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"{reason} after {MAX_RETRIES} attempts")
            print(f"  {ticker} [Claude]: {resp.status_code} — retrying in {retry_delay:.0f}s")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
            continue

        data = resp.json()
        if not resp.ok or data.get("error"):
            raise RuntimeError(
                f"Claude {resp.status_code}: {(data.get('error') or {}).get('message', 'unknown')}")

        tokens = (data.get("usage") or {}).get("input_tokens") or int(_token_estimate)
        _record_tokens(tokens)
        print(f"  {ticker} [Claude]: {tokens} tokens")

        text = "\n".join(b["text"] for b in (data.get("content") or [])
                         if b.get("type") == "text")
        if not text:
            raise RuntimeError("No text in Claude response")

        try:
            clean = text.replace("```json", "").replace("```", "").strip()
            j = json.loads(clean[clean.index("{"):clean.rindex("}")+1])
            return {
                "ex_div":            norm_date(j.get("ex_div_date")),
                "pay_date":          norm_date(j.get("pay_date")),
                "ann_div_per_share": j["ann_div_per_share"] if (j.get("ann_div_per_share") or 0) > 0 else None,
                "divs_per_year":     j["divs_per_year"]     if (j.get("divs_per_year") or 0)     > 0 else None,
                "source": "claude",
            }
        except Exception:
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
            return {"ex_div": dates[0] if dates else None,
                    "pay_date": dates[1] if len(dates) > 1 else None,
                    "ann_div_per_share": None, "divs_per_year": None, "source": "claude"}

    raise RuntimeError(f"Claude failed after {MAX_RETRIES} attempts")


# ── Cascade ────────────────────────────────────────────────────────────────────

def fetch_div_dates(ticker: str, config: dict, no_claude: bool = False) -> dict:
    sources = [
        ("AV",    lambda: fetch_from_alpha_vantage(ticker, config.get("av_key", ""))),
        ("FMP",   lambda: fetch_from_fmp(ticker, config.get("fmp_key", ""))),
        ("Yahoo", lambda: fetch_from_yahoo(ticker)),
    ]
    if not no_claude:
        sources.append(("Claude", lambda: fetch_from_claude(ticker, config.get("anthropic_api_key", ""))))

    for name, fn in sources:
        result = None
        try:
            result = fn()
        except Exception as e:
            print(f"  {ticker} [{name}]: error — {e}")
            if name == "Claude":
                raise
            continue
        if result and result_usable(result):
            print(f"  {ticker} [{name}]: "
                  f"ex-div={result.get('ex_div','–')} pay={result.get('pay_date','–')}"
                  + (f" ann=${result['ann_div_per_share']}" if result.get("ann_div_per_share") else "")
                  + (f" freq={result['divs_per_year']}x"   if result.get("divs_per_year")     else ""))
            return result
        if result:
            print(f"  {ticker} [{name}]: returned data but dates are stale — trying next")
        else:
            print(f"  {ticker} [{name}]: no data — trying next")

    return {"ex_div": None, "pay_date": None,
            "ann_div_per_share": None, "divs_per_year": None, "source": "none"}


# ── Apply result to one position row ──────────────────────────────────────────

def _apply_result(pos: dict, sym: str, ex_div, pay_date,
                  ann_div_per_share, divs_per_year, source,
                  today_d: date, fom_d: date, stats: dict):
    def try_date(s):
        try: return date.fromisoformat(s) if s else None
        except Exception: return None

    fetched_pay_d  = try_date(pay_date)
    fetched_ex_d   = try_date(ex_div)
    existing_pay_d = try_date(pos.get("pay_date"))
    existing_ex_d  = try_date(pos.get("ex_div_date"))

    fetched_pay_ok  = fetched_pay_d  and fetched_pay_d  >= fom_d
    fetched_ex_ok   = fetched_ex_d   and fetched_ex_d   >= today_d
    existing_pay_ok = existing_pay_d and existing_pay_d >= fom_d
    existing_ex_ok  = existing_ex_d  and existing_ex_d  >= today_d

    lbl = sheet_label(pos["sheet"])

    if (not ex_div and not pay_date
            and not pos.get("pay_date") and not pos.get("ex_div_date")
            and not ann_div_per_share):
        pos["status"] = "skipped"; stats["skipped"] += 1
        print(f"  {sym} [{lbl}]: no data — skipping")

    elif not ex_div and not pay_date and not pos.get("pay_date") and not pos.get("ex_div_date"):
        pos["new_ann_div"] = ann_div_per_share
        pos["new_divs_py"] = divs_per_year
        pos["status"] = "updated"; stats["updated"] += 1
        print(f"  {sym} [{lbl}]: writing ann_div=${ann_div_per_share or '–'}")

    elif not fetched_pay_ok and not fetched_ex_ok:
        if existing_pay_ok or existing_ex_ok:
            pos["new_ex_div"]   = pos.get("ex_div_date")
            pos["new_pay_date"] = pos.get("pay_date")
            pos["new_ann_div"]  = ann_div_per_share
            pos["new_divs_py"]  = divs_per_year
            pos["status"] = "updated"; stats["updated"] += 1
            print(f"  {sym} [{lbl}]: stale fetch — keeping existing dates")
        else:
            pos["status"] = "skipped"; stats["skipped"] += 1
            print(f"  {sym} [{lbl}]: stale — skipping")

    else:
        pos["new_ex_div"]   = ex_div   if fetched_ex_ok  else (pos.get("ex_div_date")  if existing_ex_ok  else ex_div)
        pos["new_pay_date"] = pay_date if fetched_pay_ok else (pos.get("pay_date")      if existing_pay_ok else pay_date)
        pos["new_ann_div"]  = ann_div_per_share
        pos["new_divs_py"]  = divs_per_year
        pos["status"] = "updated"; stats["updated"] += 1
        print(f"  {sym} [{lbl}] [{source or '?'}]: "
              f"ex-div={pos['new_ex_div'] or '–'} pay={pos['new_pay_date'] or '–'}"
              + (f" ann=${ann_div_per_share}" if ann_div_per_share else "")
              + (f" freq={divs_per_year}x"   if divs_per_year     else ""))


# ── Write one position back to Numbers ────────────────────────────────────────

def _write_pos(pos: dict, doc_name: str, months: list, sym: str):
    ann_div  = pos.get("new_ann_div") if pos.get("new_ann_div") is not None else parse_num(pos.get("ann_div"))
    divs_py  = pos.get("new_divs_py") if pos.get("new_divs_py") is not None else (int(pos.get("divs_per_year") or 0) or 4)
    pay_date = pos.get("new_pay_date") or pos.get("pay_date")

    month_amounts = compute_month_amounts_from_values(
        ann_div, divs_py, pay_date, pos.get("shares"), months)

    data = {
        "new_ann_div":   pos.get("new_ann_div"),
        "new_divs_py":   pos.get("new_divs_py"),
        "new_ex_div":    pos.get("new_ex_div"),
        "new_pay_date":  pos.get("new_pay_date"),
        "month_amounts": month_amounts,
    }
    try:
        write_ticker(doc_name, pos["sheet"], pos["row"], pos["col_map"], data, months)
        print(f"  {sym} → {pos['sheet']} ✓")
    except Exception as e:
        print(f"  {sym} [{pos['sheet']}]: write error — {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    global _token_log, _recent_tokens, _token_estimate

    parser = argparse.ArgumentParser(
        description="Refresh dividend data in Apple Numbers portfolio document")
    parser.add_argument("--doc", required=True,
                        help="Numbers document name as shown in title bar (without .numbers)")
    parser.add_argument("--force",        action="store_true",
                        help="Re-fetch even if existing dates are current")
    parser.add_argument("--preview",      action="store_true",
                        help="Fetch and print results but do not write to Numbers")
    parser.add_argument("--no-claude",    action="store_true",
                        help="Skip Claude API fallback")
    parser.add_argument("--amounts-only", action="store_true",
                        help="Skip fetching; recalculate and write monthly amounts only")
    args = parser.parse_args()

    config = load_config()

    # Reset token state per run (mirrors HTML tokenEstimate = 1300 reset)
    _token_log.clear()
    _recent_tokens.clear()
    _token_estimate = 1300.0

    doc_name = args.doc
    # Numbers stores document names with the .numbers extension internally
    if not doc_name.lower().endswith(".numbers"):
        doc_name += ".numbers"
    print(f"Reading portfolio sheets from: {doc_name}")
    try:
        sheets = get_portfolio_sheets(doc_name)
    except Exception as e:
        sys.exit(f"ERROR: {e}")

    if not sheets:
        sys.exit("ERROR: No Portfolio sheets found (Portfolio-Cash is excluded).")
    print(f"Portfolio sheets: {', '.join(sheets)}")

    # Collect all equity positions across all sheets
    all_positions: list = []
    sheet_col_maps: dict = {}
    for sheet in sheets:
        print(f"\nReading sheet: {sheet}...")
        try:
            col_map = read_col_map(doc_name, sheet)
        except Exception as e:
            print(f"  WARNING: Could not read col map for {sheet}: {e}")
            continue
        if col_map["SYMBOL"] < 0:
            print(f"  WARNING: {sheet} missing Symbol column — skipping")
            continue
        sheet_col_maps[sheet] = col_map
        try:
            positions = read_positions(doc_name, sheet, col_map)
        except Exception as e:
            print(f"  WARNING: Could not read positions from {sheet}: {e}")
            continue
        for pos in positions:
            pos["sheet"]   = sheet
            pos["col_map"] = col_map
        print(f"  {len(positions)} equity positions found")
        all_positions.extend(positions)

    if not all_positions:
        sys.exit("No equity positions found across any Portfolio sheet.")
    print(f"\nTotal: {len(all_positions)} positions across all sheets")

    months     = get_rolling_months()
    today_d    = date.today()
    fom_d      = today_d.replace(day=1)

    # Classify: current (already up-to-date) vs needs fetch
    to_fetch: list = []
    current:  list = []

    if args.amounts_only:
        current = list(all_positions)
    else:
        for pos in all_positions:
            ep = pos.get("pay_date")
            ee = pos.get("ex_div_date")
            try: pay_d  = date.fromisoformat(ep) if ep else None
            except Exception: pay_d = None
            try: exdiv_d = date.fromisoformat(ee) if ee else None
            except Exception: exdiv_d = None

            pay_ok   = pay_d   and pay_d   >= fom_d
            exdiv_ok = exdiv_d and exdiv_d >= today_d

            if not args.force and (pay_ok or exdiv_ok):
                reason = (f"ex-div {ee} not yet passed" if exdiv_ok
                          else f"pay date {ep} still current")
                print(f"  {pos['symbol']} [{sheet_label(pos['sheet'])}]: {reason} — skipping fetch")
                pos["status"]       = "current"
                pos["new_ex_div"]   = ee
                pos["new_pay_date"] = ep
                current.append(pos)
            else:
                pos["status"] = "pending"
                to_fetch.append(pos)

    stats = {"updated": len(current), "skipped": 0, "errors": 0}
    print(f"\n{len(current)} positions already current, {len(to_fetch)} to fetch")

    # Fetch: group by symbol so each unique ticker is fetched once
    if to_fetch:
        by_symbol: dict = {}
        for pos in to_fetch:
            by_symbol.setdefault(pos["symbol"], []).append(pos)
        unique = list(by_symbol.keys())
        print(f"\nFetching dates for {len(unique)} unique ticker(s) across {len(to_fetch)} rows...")

        for i, sym in enumerate(unique):
            rows_for_sym = by_symbol[sym]
            lbl_str = ", ".join({sheet_label(r["sheet"]) for r in rows_for_sym})
            print(f"\n[{i+1}/{len(unique)}] {sym} [{lbl_str}]")

            try:
                res = fetch_div_dates(sym, config, no_claude=args.no_claude)
            except Exception as e:
                for pos in rows_for_sym:
                    pos["status"] = "error"
                stats["errors"] += len(rows_for_sym)
                print(f"  {sym} ERROR: {e}")
                continue

            for pos in rows_for_sym:
                _apply_result(pos, sym,
                              res["ex_div"], res["pay_date"],
                              res["ann_div_per_share"], res["divs_per_year"],
                              res["source"], today_d, fom_d, stats)

            if not args.preview:
                for pos in rows_for_sym:
                    if pos.get("status") == "updated":
                        _write_pos(pos, doc_name, months, sym)

            if len(rows_for_sym) > 1:
                print(f"  {sym}: applied to {len(rows_for_sym)} rows across sheets")

    # Write monthly amounts for current (already-valid) tickers
    if not args.preview:
        to_write_current = [p for p in current if parse_num(p.get("ann_div")) > 0]
        if to_write_current:
            print(f"\nWriting monthly amounts for {len(to_write_current)} current positions...")
            for pos in to_write_current:
                _write_pos(pos, doc_name, months, pos["symbol"])

    # Sort each Portfolio sheet by pay date
    if not args.preview:
        print("\nSorting Portfolio sheets by pay date...")
        for sheet in sheets:
            try:
                cm = sheet_col_maps.get(sheet)
                if not cm:
                    cm = read_col_map(doc_name, sheet)
                if cm["PAY_DATE"] < 0:
                    print(f"  {sheet}: no Pay Date column — skipping sort")
                    continue
                sort_sheet(doc_name, sheet, cm["PAY_DATE"] + 1)
                print(f"  {sheet}: sorted ✓")
            except Exception as e:
                print(f"  {sheet}: sort error — {e}")

    print(f"\nDone — {stats['updated']} updated, {stats['skipped']} skipped, {stats['errors']} errors")


if __name__ == "__main__":
    main()
