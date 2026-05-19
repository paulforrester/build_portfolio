#!/usr/bin/env python3
"""
Classify portfolio positions by GICS sector and market cap tier.
Writes two summary tables (Sector Breakdown, Cap Breakdown) to the Summary sheet.

Usage:
    python3 categorize_portfolio.py
    python3 categorize_portfolio.py --doc "Portfolio May 2026 (13).numbers"
    python3 categorize_portfolio.py --preview

API keys read from env vars or ~/.dividend_refresher/config.json:
    FMP_KEY            (required)
    AV_KEY             (unused here, present for config compatibility)
    ANTHROPIC_API_KEY  (unused here, present for config compatibility)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("ERROR: requests is required.  pip install requests")


# ── Constants ──────────────────────────────────────────────────────────────────

NUMBRIDGE_URL   = "http://127.0.0.1:8765/mcp"
FMP_BASE        = "https://financialmodelingprep.com/api/v3"
PORTFOLIO_TABLE = "My Portfolio"
SUMMARY_SHEET   = "Summary"
SUMMARY_TABLE   = "Portfolio Summary"
SECTOR_TABLE    = "Sector Breakdown"
CAP_TABLE       = "Cap Breakdown"
FMP_QUOTA_WARN  = 230   # warn and stop API calls above this count
FMP_RATE_S      = 0.2   # min seconds between FMP calls (5/s)

KNOWN_ETFS = {
    "QQQM", "QQQ", "VTI", "VOO", "VGT", "VXUS",
    "SPY", "IVV", "IJH", "IJR", "VEA", "VWO",
    "BND", "AGG", "TLT", "SHV", "GLD", "IAU",
}
MONEY_MARKETS = {"SPAXX", "FZDXX", "FZROX", "FZILX", "FCASH"}

SECTOR_ORDER = [
    "Information Technology", "Health Care", "Financials",
    "Consumer Discretionary", "Communication Services", "Industrials",
    "Consumer Staples", "Energy", "Real Estate", "Materials",
    "Utilities", "Fixed Income", "Cash", "Other",
]

CAP_ORDER = [
    "Mega-Cap", "Large-Cap", "Mid-Cap", "Small-Cap", "Micro-Cap",
    "Fixed Income", "Cash", "Other",
]

# Lowercase substrings matched against fund description for 401k classification.
# Weights are normalized to 1.0 by _classify_401k().
FUND_401K_CLASSIFICATIONS: dict = {
    "s&p 500": (
        {"Information Technology": 0.32, "Financials": 0.13, "Health Care": 0.12,
         "Consumer Discretionary": 0.11, "Communication Services": 0.09,
         "Industrials": 0.08, "Consumer Staples": 0.06, "Energy": 0.04,
         "Real Estate": 0.03, "Materials": 0.02},
        {"Mega-Cap": 0.55, "Large-Cap": 0.40, "Mid-Cap": 0.05},
    ),
    "total market": (
        {"Information Technology": 0.30, "Health Care": 0.12, "Financials": 0.13,
         "Consumer Discretionary": 0.11, "Communication Services": 0.08,
         "Industrials": 0.09, "Consumer Staples": 0.05, "Energy": 0.03,
         "Real Estate": 0.04, "Materials": 0.03, "Utilities": 0.02},
        {"Mega-Cap": 0.45, "Large-Cap": 0.35, "Mid-Cap": 0.12, "Small-Cap": 0.08},
    ),
    "2055": (
        {"Information Technology": 0.30, "Health Care": 0.13, "Financials": 0.12,
         "Consumer Discretionary": 0.10, "Communication Services": 0.08,
         "Industrials": 0.08, "Fixed Income": 0.07, "Other": 0.12},
        {"Mega-Cap": 0.50, "Large-Cap": 0.34, "Mid-Cap": 0.08, "Fixed Income": 0.07, "Other": 0.01},
    ),
    "2050": (
        {"Information Technology": 0.28, "Health Care": 0.12, "Financials": 0.12,
         "Consumer Discretionary": 0.10, "Communication Services": 0.08,
         "Industrials": 0.08, "Fixed Income": 0.12, "Other": 0.10},
        {"Mega-Cap": 0.47, "Large-Cap": 0.32, "Mid-Cap": 0.08, "Fixed Income": 0.12, "Other": 0.01},
    ),
    "2045": (
        {"Information Technology": 0.26, "Health Care": 0.12, "Financials": 0.11,
         "Consumer Discretionary": 0.10, "Communication Services": 0.07,
         "Industrials": 0.08, "Fixed Income": 0.16, "Other": 0.10},
        {"Mega-Cap": 0.44, "Large-Cap": 0.30, "Mid-Cap": 0.08, "Fixed Income": 0.16, "Other": 0.02},
    ),
    "2040": (
        {"Information Technology": 0.24, "Health Care": 0.12, "Financials": 0.11,
         "Consumer Discretionary": 0.09, "Communication Services": 0.07,
         "Industrials": 0.08, "Fixed Income": 0.20, "Other": 0.09},
        {"Mega-Cap": 0.41, "Large-Cap": 0.30, "Mid-Cap": 0.08, "Fixed Income": 0.20, "Other": 0.01},
    ),
    "2035": (
        {"Information Technology": 0.21, "Health Care": 0.11, "Financials": 0.10,
         "Consumer Discretionary": 0.08, "Communication Services": 0.06,
         "Industrials": 0.07, "Fixed Income": 0.28, "Other": 0.09},
        {"Mega-Cap": 0.36, "Large-Cap": 0.27, "Mid-Cap": 0.07, "Fixed Income": 0.28, "Other": 0.02},
    ),
    "2030": (
        {"Information Technology": 0.18, "Health Care": 0.10, "Financials": 0.09,
         "Consumer Discretionary": 0.07, "Communication Services": 0.05,
         "Industrials": 0.06, "Fixed Income": 0.35, "Other": 0.10},
        {"Mega-Cap": 0.30, "Large-Cap": 0.23, "Mid-Cap": 0.06, "Fixed Income": 0.35, "Other": 0.06},
    ),
    "bond": (
        {"Fixed Income": 1.0},
        {"Fixed Income": 1.0},
    ),
    "international": (
        {"Financials": 0.20, "Industrials": 0.15, "Information Technology": 0.12,
         "Consumer Discretionary": 0.10, "Consumer Staples": 0.08,
         "Health Care": 0.08, "Materials": 0.07, "Other": 0.20},
        {"Large-Cap": 0.45, "Mid-Cap": 0.35, "Small-Cap": 0.20},
    ),
    "small cap": (
        {"Industrials": 0.18, "Financials": 0.17, "Health Care": 0.15,
         "Consumer Discretionary": 0.13, "Information Technology": 0.12,
         "Real Estate": 0.07, "Other": 0.18},
        {"Small-Cap": 0.70, "Mid-Cap": 0.30},
    ),
}


# ── JXA / AppleScript runners ─────────────────────────────────────────────────

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


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    config: dict = {}
    cfg_path = Path.home() / ".dividend_refresher" / "config.json"
    if cfg_path.exists():
        try:
            with open(cfg_path) as f:
                config = json.load(f)
        except Exception as e:
            print(f"WARNING: Could not read {cfg_path}: {e}", file=sys.stderr)
    config["fmp_key"]           = os.environ.get("FMP_KEY")            or config.get("fmp_key", "")
    config["av_key"]            = os.environ.get("AV_KEY")             or config.get("av_key", "")
    config["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY")  or config.get("anthropic_api_key", "")
    return config


# ── CUSIP / money-market detection ────────────────────────────────────────────

def is_cusip(sym: str) -> bool:
    if not sym:
        return False
    return bool(
        re.match(r"^\d{9}[A-Z]\d$", sym)
        or re.match(r"^\d{6}[A-Z]{2}\d$", sym)
        or (re.match(r"^\d+[A-Z]+\d+$", sym) and len(sym) >= 8)
    )


def _is_money_market(sym: str) -> bool:
    su = sym.upper()
    if su in MONEY_MARKETS:
        return True
    for mm in MONEY_MARKETS:
        if su.startswith(mm + " ") or su.startswith(mm + "-"):
            return True
    return False


# ── NumBridge client ──────────────────────────────────────────────────────────

class NumBridgeClient:
    """Thin JSON-RPC wrapper around the NumBridge MCP HTTP endpoint."""

    def __init__(self, url: str = NUMBRIDGE_URL):
        self.url       = url
        self._sess     = requests.Session()
        self._msg_id   = 0
        self._calls    = 0
        self.available = False
        self._init()

    def _init(self):
        try:
            self._msg_id += 1
            resp = self._sess.post(self.url, json={
                "jsonrpc": "2.0", "method": "initialize",
                "params": {}, "id": self._msg_id,
            }, timeout=5)
            resp.raise_for_status()
            self.available = True
        except Exception:
            self.available = False

    def call(self, tool: str, **kwargs) -> str:
        if not self.available:
            raise RuntimeError("NumBridge is not available — is it running?")
        # Reinitialize periodically to avoid session expiry
        if self._calls > 0 and self._calls % 18 == 0:
            self._init()
        self._msg_id += 1
        self._calls  += 1
        resp = self._sess.post(self.url, json={
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": tool, "arguments": kwargs},
            "id": self._msg_id,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"NumBridge [{tool}]: {data['error']}")
        content = (data.get("result") or {}).get("content") or []
        return content[0].get("text", "") if content else ""


# ── FMP helpers ───────────────────────────────────────────────────────────────

_fmp_call_count = 0
_fmp_last_call  = 0.0
_stock_cache: dict = {}
_etf_cache:   dict = {}


def _fmp_get(path: str, fmp_key: str):
    global _fmp_call_count, _fmp_last_call
    if _fmp_call_count >= FMP_QUOTA_WARN:
        print(f"WARNING: FMP quota limit ({FMP_QUOTA_WARN} calls reached). "
              "Remaining unclassified positions will show as 'Other'.")
        return None
    wait = FMP_RATE_S - (time.time() - _fmp_last_call)
    if wait > 0:
        time.sleep(wait)
    url = f"{FMP_BASE}{path}"
    for attempt in range(3):
        try:
            resp = requests.get(url, params={"apikey": fmp_key}, timeout=15)
            _fmp_last_call   = time.time()
            _fmp_call_count += 1
            if resp.status_code == 429:
                wait_s = 30
                print(f"  FMP 429 rate limit — waiting {wait_s}s (attempt {attempt+1}/3)")
                time.sleep(wait_s)
                continue
            if not resp.ok:
                return None
            data = resp.json()
            if isinstance(data, dict) and (data.get("Error Message") or data.get("error")):
                return None
            return data
        except requests.RequestException as e:
            print(f"  FMP request error ({path}): {e}")
            return None
    return None


# ── Classification helpers ────────────────────────────────────────────────────

def _mktcap_tier(mkt_cap: float) -> str:
    if mkt_cap > 200e9: return "Mega-Cap"
    if mkt_cap > 10e9:  return "Large-Cap"
    if mkt_cap > 2e9:   return "Mid-Cap"
    if mkt_cap > 250e6: return "Small-Cap"
    return "Micro-Cap"


def _normalize(weights: dict) -> dict:
    total = sum(weights.values())
    if total <= 0:
        return {"Other": 1.0}
    return {k: v / total for k, v in weights.items() if v > 0}


def _classify_401k(description: str) -> tuple:
    desc = description.lower()
    for key, (sw, cw) in FUND_401K_CLASSIFICATIONS.items():
        if key in desc:
            return _normalize(sw), _normalize(cw)
    return {"Other": 1.0}, {"Other": 1.0}


def _get_stock_profile(symbol: str, fmp_key: str) -> Optional[dict]:
    if symbol in _stock_cache:
        return _stock_cache[symbol]
    data = _fmp_get(f"/profile/{symbol}", fmp_key)
    if not data or not isinstance(data, list) or not data[0]:
        _stock_cache[symbol] = None
        return None
    p = data[0]
    result = {
        "sector": p.get("sector") or "Other",
        "mktCap": float(p.get("mktCap") or 0),
        "isEtf":  bool(p.get("isEtf")),
    }
    _stock_cache[symbol] = result
    return result


def get_etf_breakdown(etf_symbol: str, fmp_key: str) -> tuple:
    """Return (sector_weights, cap_weights) for an ETF based on top-25 holdings."""
    if etf_symbol in _etf_cache:
        return _etf_cache[etf_symbol]

    print(f"  ETF {etf_symbol}: fetching top-25 holdings…")
    holders = _fmp_get(f"/etf-holder/{etf_symbol}", fmp_key)
    if not holders or not isinstance(holders, list):
        print(f"    {etf_symbol}: no holder data — Other/Other")
        result = ({"Other": 1.0}, {"Other": 1.0})
        _etf_cache[etf_symbol] = result
        return result

    top25    = sorted(holders, key=lambda h: h.get("weightPercentage", 0), reverse=True)[:25]
    total_w  = sum(h.get("weightPercentage", 0) for h in top25)
    if total_w == 0:
        result = ({"Other": 1.0}, {"Other": 1.0})
        _etf_cache[etf_symbol] = result
        return result

    sector_w: dict = {}
    cap_w:    dict = {}
    missing = 0

    for h in top25:
        sym    = (h.get("asset") or "").strip()
        weight = h.get("weightPercentage", 0) / total_w
        if not sym:
            sector_w["Other"] = sector_w.get("Other", 0) + weight
            cap_w["Other"]    = cap_w.get("Other", 0) + weight
            continue
        profile = _get_stock_profile(sym, fmp_key)
        if not profile:
            sector_w["Other"] = sector_w.get("Other", 0) + weight
            cap_w["Other"]    = cap_w.get("Other", 0) + weight
            missing += 1
            continue
        sector = profile.get("sector") or "Other"
        tier   = _mktcap_tier(profile["mktCap"]) if profile.get("mktCap", 0) > 0 else "Other"
        sector_w[sector] = sector_w.get(sector, 0) + weight
        cap_w[tier]      = cap_w.get(tier, 0) + weight

    print(f"    {etf_symbol}: {len(top25)} holdings classified "
          f"({missing} missing), {_fmp_call_count} FMP calls total")

    result = (_normalize(sector_w), _normalize(cap_w))
    _etf_cache[etf_symbol] = result
    return result


# ── Position reading (JXA) ────────────────────────────────────────────────────

def _list_all_sheets(doc_name: str) -> list:
    script = f"""
ObjC.import('Foundation');
const app = Application("Numbers");
const docs = app.documents.whose({{name: {{_equals: {json.dumps(doc_name)}}}}});
if (!docs.length) throw new Error("Document not found: " + {json.dumps(doc_name)});
JSON.stringify(docs[0].sheets().map(s => s.name()));
"""
    return json.loads(run_jxa_file(script))


def _read_sheet(doc_name: str, sheet_name: str) -> tuple:
    """Return (rows, totals_mv) for a sheet.

    rows: list of {symbol, description, market_value}
    totals_mv: the Market Value in the totals/footer row (col K), or None
    """
    script = f"""
ObjC.import('Foundation');
const app = Application("Numbers");
const doc = app.documents.whose({{name: {{_equals: {json.dumps(doc_name)}}}}});
if (!doc.length) throw new Error("Document not found");
const sheet = doc[0].sheets.whose({{name: {{_equals: {json.dumps(sheet_name)}}}}});
if (!sheet.length) throw new Error("Sheet not found: " + {json.dumps(sheet_name)});
const tbl = sheet[0].tables.whose({{name: {{_equals: {json.dumps(PORTFOLIO_TABLE)}}}}});
if (!tbl.length) throw new Error("Table not found: " + {json.dumps(PORTFOLIO_TABLE)});
const t = tbl[0];
const nRows = t.rowCount();

function safeVal(cells, idx) {{
    try {{
        const v = cells[idx].value();
        if (v === null || v === undefined) return "";
        if (v instanceof Date) return "";
        return String(v);
    }} catch(e) {{ return ""; }}
}}
function safeMv(cells, idx) {{
    try {{
        const v = cells[idx].value();
        if (typeof v === 'number') return v;
        if (v !== null && v !== undefined && v !== "" && v !== "\\u2013" && v !== "-")
            return parseFloat(String(v));
        return null;
    }} catch(e) {{ return null; }}
}}

const rows = [];
let totals_mv = null;
for (let r = 1; r < nRows; r++) {{
    const cells = t.rows[r].cells;
    const colA  = safeVal(cells, 0).trim();
    const sym   = safeVal(cells, 1).trim();
    const desc  = safeVal(cells, 2).trim();
    // Totals / footer row: col A contains the portfolio sheet label
    if (colA && colA.startsWith("Portfolio")) {{
        totals_mv = safeMv(cells, 10);
        break;
    }}
    // Skip completely empty rows
    if (!sym && !desc) continue;
    const mv = safeMv(cells, 10);
    if (mv === null || isNaN(mv)) continue;
    rows.push({{symbol: sym, description: desc, market_value: mv}});
}}
JSON.stringify({{rows: rows, totals_mv: totals_mv}});
"""
    raw = json.loads(run_jxa_file(script))
    return raw["rows"], raw["totals_mv"]


def read_all_positions(doc_name: str) -> tuple:
    """Return (positions, cash_mv).

    positions: list of dicts with symbol, description, market_value, sheet,
               is_etf, is_401k
    cash_mv: Portfolio-Cash total market value (0 if sheet absent)
    """
    all_sheets = _list_all_sheets(doc_name)
    portfolio_sheets = [s for s in all_sheets if s.startswith("Portfolio")]

    all_positions: list = []
    cash_mv = 0.0

    for sheet in portfolio_sheets:
        print(f"  Reading {sheet}…")
        try:
            rows, totals_mv = _read_sheet(doc_name, sheet)
        except Exception as e:
            print(f"    WARNING: could not read {sheet}: {e}")
            continue

        if sheet == "Portfolio-Cash":
            if totals_mv is not None:
                cash_mv = float(totals_mv)
            print(f"    Cash total: ${cash_mv:,.0f}")
            continue

        accepted = 0
        for row in rows:
            sym  = row.get("symbol", "").strip()
            desc = row.get("description", "").strip()
            mv   = row.get("market_value", 0) or 0

            if mv <= 0:
                continue
            if _is_money_market(sym) or is_cusip(sym):
                continue

            is_401k = (not sym) and ("IRA" in sheet or "401" in sheet.upper())
            is_etf  = sym.upper() in KNOWN_ETFS

            all_positions.append({
                "symbol":       sym,
                "description":  desc,
                "market_value": mv,
                "sheet":        sheet,
                "is_etf":       is_etf,
                "is_401k":      is_401k,
            })
            accepted += 1

        print(f"    {accepted} positions")

    return all_positions, cash_mv


# ── Classification ────────────────────────────────────────────────────────────

def classify_positions(positions: list, fmp_key: str) -> list:
    unique_syms = {p["symbol"] for p in positions if p.get("symbol")}
    n_401k      = sum(1 for p in positions if p.get("is_401k"))
    print(f"\nClassifying {len(positions)} positions "
          f"({len(unique_syms)} unique symbols, {n_401k} 401k funds)…")

    for pos in positions:
        if pos.get("is_401k"):
            sw, cw = _classify_401k(pos.get("description", ""))
            pos["sector_weights"] = sw
            pos["cap_weights"]    = cw
            print(f"  {pos['description'][:40]}: 401k classification")
            continue

        sym = pos.get("symbol", "")
        if not sym:
            pos["sector_weights"] = {"Other": 1.0}
            pos["cap_weights"]    = {"Other": 1.0}
            continue

        profile = _get_stock_profile(sym, fmp_key)

        if pos.get("is_etf") or (profile and profile.get("isEtf")):
            pos["is_etf"] = True
            sw, cw = get_etf_breakdown(sym, fmp_key)
        elif profile:
            sector = profile.get("sector") or "Other"
            tier   = _mktcap_tier(profile["mktCap"]) if profile.get("mktCap", 0) > 0 else "Other"
            sw, cw = {sector: 1.0}, {tier: 1.0}
            print(f"  {sym}: {sector} / {tier}")
        else:
            print(f"  {sym}: profile not found — Other/Other")
            sw, cw = {"Other": 1.0}, {"Other": 1.0}

        pos["sector_weights"] = sw
        pos["cap_weights"]    = cw

    return positions


# ── Aggregation ───────────────────────────────────────────────────────────────

def aggregate_breakdowns(positions: list, cash_mv: float) -> tuple:
    sector_totals: dict = {}
    cap_totals:    dict = {}

    for pos in positions:
        mv = pos.get("market_value", 0) or 0
        for sector, weight in pos.get("sector_weights", {}).items():
            sector_totals[sector] = sector_totals.get(sector, 0) + mv * weight
        for tier, weight in pos.get("cap_weights", {}).items():
            cap_totals[tier] = cap_totals.get(tier, 0) + mv * weight

    if cash_mv > 0:
        sector_totals["Cash"] = sector_totals.get("Cash", 0) + cash_mv
        cap_totals["Cash"]    = cap_totals.get("Cash", 0) + cash_mv

    return sector_totals, cap_totals


# ── Stdout preview ────────────────────────────────────────────────────────────

def print_breakdown(sector_totals: dict, cap_totals: dict):
    total = sum(sector_totals.values())
    if total == 0:
        print("No portfolio value found.")
        return

    def _pct(v):
        return v / total * 100 if total else 0

    print(f"\n{'='*55}")
    print(f"PORTFOLIO CATEGORY BREAKDOWN")
    print(f"Total portfolio: ${total:,.0f}")

    print(f"\nSECTOR BREAKDOWN:")
    seen = set()
    for sector in SECTOR_ORDER:
        if sector in sector_totals:
            mv = sector_totals[sector]
            print(f"  {sector:<38} ${mv:>12,.0f}  {_pct(mv):>5.1f}%")
            seen.add(sector)
    # Unlisted sectors (shouldn't happen, but be safe)
    for sector, mv in sorted(sector_totals.items(), key=lambda x: -x[1]):
        if sector not in seen:
            print(f"  {sector:<38} ${mv:>12,.0f}  {_pct(mv):>5.1f}%")

    print(f"\nMARKET CAP BREAKDOWN:")
    seen = set()
    for tier in CAP_ORDER:
        if tier in cap_totals:
            mv = cap_totals[tier]
            print(f"  {tier:<38} ${mv:>12,.0f}  {_pct(mv):>5.1f}%")
            seen.add(tier)
    for tier, mv in sorted(cap_totals.items(), key=lambda x: -x[1]):
        if tier not in seen:
            print(f"  {tier:<38} ${mv:>12,.0f}  {_pct(mv):>5.1f}%")

    print(f"\nAPI calls used: {_fmp_call_count} FMP calls")


# ── Numbers write (NumBridge) ─────────────────────────────────────────────────

def _build_table_rows(totals: dict, order: list, grand_total: float) -> tuple:
    """Return (data_rows, ordered_keys) sorted per order then by value."""
    ordered: list = []
    added   = set()
    for key in order:
        if key in totals:
            ordered.append(key)
            added.add(key)
    for key, _ in sorted(totals.items(), key=lambda x: -x[1]):
        if key not in added:
            ordered.append(key)

    rows = []
    for key in ordered:
        mv  = totals[key]
        pct = mv / grand_total if grand_total else 0
        rows.append([key, round(mv, 2), round(pct, 6)])
    return rows


def _write_breakdown_table(nb: NumBridgeClient, doc_name: str,
                            table_name: str, header: list,
                            data_rows: list, grand_total: float,
                            x: float, y: float):
    """Create a breakdown table on the Summary sheet and populate it."""
    n_data = len(data_rows)
    n_rows = 1 + n_data + 1   # header + data + total

    # Create table
    nb.call("add_table", document=doc_name, sheet=SUMMARY_SHEET,
            name=table_name, num_rows=n_rows, num_columns=3)
    # Position it
    nb.call("set_table_layout", document=doc_name, sheet=SUMMARY_SHEET,
            table=table_name, x=x, y=y, width=420.0,
            height=float(n_rows * 22 + 4))

    # Header row
    nb.call("set_range", document=doc_name, sheet=SUMMARY_SHEET, table=table_name,
            start_row=1, start_col=1, values=[header], bold=True)

    # Data rows
    if data_rows:
        nb.call("set_range", document=doc_name, sheet=SUMMARY_SHEET, table=table_name,
                start_row=2, start_col=1, values=data_rows)

    # Total row
    total_pct = round(sum(r[1] for r in data_rows) / grand_total, 6) if grand_total else 1.0
    nb.call("set_range", document=doc_name, sheet=SUMMARY_SHEET, table=table_name,
            start_row=n_rows, start_col=1,
            values=[["Total", round(grand_total, 2), total_pct]],
            bold=True)

    # Column formats
    nb.call("set_column_format", document=doc_name, sheet=SUMMARY_SHEET,
            table=table_name, column=2, number_format="currency")
    nb.call("set_column_format", document=doc_name, sheet=SUMMARY_SHEET,
            table=table_name, column=3, number_format="percentage")


def write_to_numbers(doc_name: str, nb: NumBridgeClient,
                     sector_totals: dict, cap_totals: dict):
    grand_total = sum(sector_totals.values())

    # Remove existing breakdown tables if present
    existing_raw = nb.call("list_tables", document=doc_name, sheet=SUMMARY_SHEET)
    existing = [t.strip() for t in existing_raw.splitlines() if t.strip()]
    for tname in (SECTOR_TABLE, CAP_TABLE):
        if tname in existing:
            print(f"  Removing existing table: {tname}")
            nb.call("remove_table", document=doc_name, sheet=SUMMARY_SHEET, table=tname)

    # Get Portfolio Summary layout for positioning
    summary_x = 50.0
    summary_y = 50.0
    summary_w = 400.0
    summary_h = 120.0
    try:
        layout_raw = nb.call("get_table_layout", document=doc_name,
                              sheet=SUMMARY_SHEET, table=SUMMARY_TABLE)
        layout = json.loads(layout_raw)
        summary_x = float(layout.get("x", summary_x))
        summary_y = float(layout.get("y", summary_y))
        summary_w = float(layout.get("width", summary_w))
        summary_h = float(layout.get("height", summary_h))
    except Exception as e:
        print(f"  WARNING: could not read Portfolio Summary layout: {e} — using defaults")

    # Place Sector Breakdown to the right of Portfolio Summary
    sector_x = summary_x + summary_w + 40
    sector_y = summary_y
    sector_rows = _build_table_rows(sector_totals, SECTOR_ORDER, grand_total)
    sector_height = (1 + len(sector_rows) + 1) * 22 + 4

    print(f"  Writing {SECTOR_TABLE} ({len(sector_rows)} rows)…")
    _write_breakdown_table(nb, doc_name, SECTOR_TABLE,
                           ["Sector", "Market Value", "% of Portfolio"],
                           sector_rows, grand_total,
                           sector_x, sector_y)

    # Place Cap Breakdown below Sector Breakdown
    cap_x = sector_x
    cap_y = sector_y + sector_height + 30
    cap_rows = _build_table_rows(cap_totals, CAP_ORDER, grand_total)

    print(f"  Writing {CAP_TABLE} ({len(cap_rows)} rows)…")
    _write_breakdown_table(nb, doc_name, CAP_TABLE,
                           ["Cap Tier", "Market Value", "% of Portfolio"],
                           cap_rows, grand_total,
                           cap_x, cap_y)

    print(f"  Done — {nb._calls} NumBridge calls used")


# ── Document auto-detection ───────────────────────────────────────────────────

def find_open_portfolio_doc() -> Optional[str]:
    """Return the name of the first open Numbers document that has Portfolio sheets."""
    script = """
ObjC.import('Foundation');
const app = Application("Numbers");
try {
    JSON.stringify(app.documents().map(d => d.name()));
} catch(e) { JSON.stringify([]); }
"""
    try:
        docs = json.loads(run_jxa_file(script))
    except Exception:
        return None

    for doc in docs:
        try:
            sheets = _list_all_sheets(doc)
            if any(s.startswith("Portfolio") for s in sheets):
                return doc
        except Exception:
            continue
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Classify portfolio positions by sector and market cap")
    parser.add_argument("--doc",     default=None,
                        help="Numbers document name (with .numbers), e.g. 'Portfolio May 2026 (13).numbers'")
    parser.add_argument("--preview", action="store_true",
                        help="Print breakdown without writing to Numbers")
    args = parser.parse_args()

    config  = load_config()
    fmp_key = config.get("fmp_key", "")
    if not fmp_key:
        sys.exit("ERROR: FMP_KEY is required. Set it in env or ~/.dividend_refresher/config.json")

    # Resolve document name
    doc_name = args.doc
    if doc_name:
        if not doc_name.lower().endswith(".numbers"):
            doc_name += ".numbers"
    else:
        print("No --doc specified — searching for open Numbers document with Portfolio sheets…")
        doc_name = find_open_portfolio_doc()
        if not doc_name:
            sys.exit("ERROR: No open Numbers document with Portfolio sheets found. "
                     "Pass --doc explicitly.")
        print(f"Using: {doc_name}")

    # Read positions
    print(f"\nReading positions from: {doc_name}")
    try:
        positions, cash_mv = read_all_positions(doc_name)
    except Exception as e:
        sys.exit(f"ERROR reading positions: {e}")

    if not positions and cash_mv == 0:
        sys.exit("ERROR: No positions found. Is the document open in Numbers?")

    equity_total = sum(p["market_value"] for p in positions)
    print(f"\nEquity positions: {len(positions)} "
          f"(${equity_total:,.0f}), cash: ${cash_mv:,.0f}")

    # Classify
    positions = classify_positions(positions, fmp_key)

    # Aggregate
    sector_totals, cap_totals = aggregate_breakdowns(positions, cash_mv)

    # Always print to stdout
    print_breakdown(sector_totals, cap_totals)

    if args.preview:
        print("\n(--preview: not writing to Numbers)")
        return

    # Write to Numbers via NumBridge
    print(f"\nConnecting to NumBridge at {NUMBRIDGE_URL}…")
    nb = NumBridgeClient()
    if not nb.available:
        print("WARNING: NumBridge is not running — skipping write to Numbers.")
        print("  Start NumBridge, then re-run without --preview.")
        return

    print(f"Writing breakdown tables to Summary sheet…")
    try:
        write_to_numbers(doc_name, nb, sector_totals, cap_totals)
        print(f"\nDone. Open the Summary sheet in Numbers to view the tables.")
    except Exception as e:
        print(f"ERROR writing to Numbers: {e}")
        print("(Breakdown data is still available above.)")


if __name__ == "__main__":
    main()
