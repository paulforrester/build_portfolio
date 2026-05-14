# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

`build_portfolio.py` reads a Fidelity brokerage position export CSV and builds a fully populated Apple Numbers portfolio tracking document. It drives Numbers directly via **AppleScript** (`osascript` subprocess calls) — no external server required.

## Running the Script

```bash
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --template "Portfolio Template.numbers"
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --doc-name "My Portfolio"
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --output-dir ~/Desktop
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --brokerage-only
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --ira-only
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --dry-run
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --no-dividend-fill
```

The script requires:
1. Apple Numbers installed and accessible via `osascript`
2. A template Numbers document with sheets named `_template1`–`_template6`, each containing a `My Portfolio` table — defaults to `"Portfolio Template.numbers"`

Optional: `ANTHROPIC_API_KEY` env var enables dividend gap-filling via Claude API.

## Architecture

### AppleScript Runners
- `run_applescript(script)` — short inline scripts via `osascript -e`
- `run_applescript_file(script)` — writes to a UTF-8 temp file and calls `osascript <file>` (avoids command-line length limits)
- `run_jxa_file(script)` — JXA (JavaScript for Automation) via `osascript -l JavaScript <file>`, used for reading because JXA returns JSON-parseable output

### Template Structure
The template has `_template1` through `_template6` sheets (all identical, pre-formatted with column widths, number formats, bold rows). The script consumes one per output sheet: `_template1` → Portfolio, `_template2` → Portfolio-IRA. Unused template sheets are deleted at the end.

`read_template` reads from `_template1` (falls back to `_template` for backward compatibility).

### Bulk Write Strategy (`_write_rows_as_batch`)
Two osascript calls per sheet for all data rows:
1. **Static values**: one batched script with `set value of cell {col} of row {r} to {value}` for every non-empty, non-formula cell. Numbers rejects mixed-type 2D lists, so `set value of range` and `set value of cells of row N to {list}` cannot be used.
2. **Formulas**: one batched script with `set value of cell {col} of row {r} to "=formula"` for every formula cell. Numbers parses strings beginning with `=` as formulas; `set formula of cell` is unreliable in this context.

Total: ~7 `osascript` calls per sheet (resize, clear, header row, batch statics, batch formulas, totals row, tax rows; IRA sheets skip tax rows).

### Operation Order in `build_sheet`
1. `resize_table_as` + `clear_data_rows_as` (JXA) — prepare the pre-formatted `_templateN` sheet
2. `_write_single_row_as` — header row with month-name overrides
3. `_write_rows_as_batch` — all data rows (static pass + formula pass)
4. `_write_single_row_as` — totals row
5. `_write_tax_rows_as` — Portfolio only; writes rates as pre-formatted strings ("16.44%") because Numbers `set format … to percentage` is unreliable in this context
6. `rename_sheet_as` — renames `_templateN` to the final sheet name
7. `spot_check_sheet` — reads back key cells via JXA and prints row 2, totals, and T-Bill MV sanity check

### Output Document Setup
- Output defaults to `~/Desktop/{doc_name}.numbers`
- If file already exists, appends ` (2)`, ` (3)`, etc. and warns
- `setup_output_document(template_path, doc_name, output_dir)` copies the template file, opens it in Numbers, polls until document appears, confirms with JXA to get the settled name

### CSV Parsing
- First row of Fidelity export is a title line (skipped)
- Second row is the actual CSV header
- Symbols like `SPAXX**` have trailing `**` stripped
- Account bucket classification uses normalized account names (non-alphanumeric → space)

### Account Buckets (checked in priority order)
| Bucket | Matches (normalized, case-insensitive) |
|--------|----------------------------------------|
| SCHWAB | "schwab" |
| BROKERAGE | "individual", "trust", "brokerage", "tod" |
| IRA | "traditional ira", "rollover ira", "ira bda", "inherited" |
| ROTH | "roth" |
| 401K | "401k", "401 k" |
| CMA | "cma", "cash management" |

SCHWAB must come before BROKERAGE (e.g., "Trust-Schwab" contains "trust" but is SCHWAB).

### Formula Substitution
Row-2 template formulas (e.g., `=IFERROR(STOCK(B2,0),"–")`) are substituted for each data row `r` using:
```python
re.sub(r"([A-Z]+)2\b", lambda m: f"{m.group(1)}{r}", cell)
```

`_resolve_named_refs` converts Numbers internal named column refs (e.g., `Shares 16`) to cell-letter refs (e.g., `G2`) after reading the template. This is necessary because Numbers stores formulas using header-name refs internally.

### Sheet Differences: Portfolio vs Portfolio-IRA
- **Portfolio**: BROKERAGE bucket only; column 15 header is "Gain %" with formula `=IFERROR((D{r}-I{r})/I{r},"–")`; includes tax rate rows below totals
- **Portfolio-IRA**: IRA + ROTH buckets aggregated by symbol; column 15 header overridden to "% of Portfolio" with formula `=IFERROR(K{r}/K{tot_row},"–")`; same symbol across multiple IRA accounts has shares and cost basis summed

### Position Ordering
1. Equities/funds by current value descending
2. Money market funds (SPAXX, FZDXX, FZROX, FZILX, FCASH or description contains "money market")
3. T-Bills and CDs by maturity date ascending (parsed from description with `re.search(r"(\d{1,2}/\d{1,2}/\d{4})")`)

### T-Bill Handling
T-Bills are detected by: symbol matching `^\d{9}[A-Z]\d$` or description containing "treasury bill", "treas bills", etc. For T-Bills:
- Price (col D): hardcoded from CSV last price (no STOCK() formula)
- Market Value (col K): `=IFERROR(D{r}*(G{r}/100),"–")` — T-Bills are quoted per $100 face value

### Dividend Gap-Fill
After all sheets are written, if `ANTHROPIC_API_KEY` is set and `--no-dividend-fill` is not passed, `fill_dividends` calls the Claude API (claude-sonnet-4-20250514 with web_search tool) to look up dividend data for equity positions and writes results back to Numbers.

---

## dividend-refresher.html

A browser-based tool for updating dividend information in Apple Numbers portfolio
tracking documents. It communicates with Numbers via **NumBridge** (a local MCP
server at `http://127.0.0.1:8765/mcp`) rather than directly via AppleScript, because
browsers cannot call `osascript`. NumBridge must be running before opening the file.

Open the file directly in a browser (`file://...`) — no local server needed.

### What It Does

1. Reads all sheets whose names start with `"Portfolio"` from a user-selected Numbers
   document (e.g., `Portfolio May 2026.numbers`)
2. For each equity position on those sheets, fetches current dividend data from a
   cascade of sources (see below)
3. Writes `Ann Div / sh`, `Divs/year`, `Ex-Div Date`, `Div Pay Date`, and the four
   rolling monthly dividend amount columns back to Numbers
4. Sorts each Portfolio sheet by pay date ascending
5. Skips positions whose existing sheet dates are already current (avoids unnecessary
   API calls on re-runs)

### Data Source Cascade

Tries each source in order; uses the first that returns usable current data:

| Priority | Source | Key | Free Tier |
|----------|--------|-----|-----------|
| 1st | Alpha Vantage | `dr_av_key` (localStorage) | 25 req/day, 5/min |
| 2nd | Financial Modeling Prep (FMP) | `dr_fmp_key` (localStorage) | 250 req/day |
| 3rd | Yahoo Finance | none required | unofficial, no limit |
| 4th | Claude API w/ web search | `dr_key` (localStorage) | per token usage |

A result is considered "usable" if it contains a current/future ex-div date, a
pay date in the current month or later, or a positive `ann_div_per_share`. Stale
dates (prior month) from a source cause fallthrough to the next source. If all
sources return stale dates but the sheet already has good current dates, the
existing sheet dates are kept and only the monthly amounts are updated.

### Cross-Sheet Deduplication

All Portfolio sheets are read first, collecting every row including duplicates
(e.g., QQQM in both Portfolio and Portfolio-IRA). The fetch loop then groups rows
by symbol and fetches each unique symbol exactly once. The result is applied to
all rows across all sheets that hold that symbol — so shared tickers like VTI,
QQQM, and VOO are only looked up once regardless of how many sheets they appear on.

T-Bills (CUSIP-format symbols) and money market funds (SPAXX, FZDXX, etc.) are
excluded from fetching entirely.

### NumBridge Dependency

The refresher calls these NumBridge tools:
- `list_documents` — populate the document picker dropdown
- `list_sheets` — discover Portfolio sheets (must return newline-delimited names)
- `get_cell` — read header row (col map) and row data
- `set_cell` — write dividend data back to cells
- `sort_table` — sort each Portfolio sheet by pay date column after writing

**Important:** `list_sheets`, `list_documents`, and `list_tables` must return
newline-delimited strings (one name per line), not concatenated blobs. If NumBridge
is updated and these revert to concatenated output, `getPortfolioSheets()` will
silently find only the first sheet.

### Column Map

The refresher reads the header row dynamically via `readColMap()` on each sheet,
building a 0-based index map. All NumBridge write calls use `colIdx + 1` (1-based).
Expected columns (by header name substring match):

| Field | Header contains | Typical col (1-based) |
|-------|----------------|----------------------|
| Symbol | "symbol" | 2 |
| Shares | "shares" | 7 |
| Ann Div/sh | "ann div / sh" | 16 |
| Ex-Div Date | "ex-div date" | 23 |
| Div Pay Date | "div pay date" | 24 |
| Divs/year | "divs/year" | 26 |
| Month start | (after Divs/year) | 27 |

Monthly columns (4 of them starting at MonthStart) use dynamic
`NOW()`-based header formulas in the template — they return empty strings from
`get_cell`, so `readColMap` reads all 32 columns unconditionally and derives
`MONTH_START` as `divsYearCol + 1`.

### Settings (localStorage)

All keys persist in the browser's `localStorage`:

| Key | Purpose |
|-----|---------|
| `dr_av_key` | Alpha Vantage API key |
| `dr_fmp_key` | FMP API key |
| `dr_key` | Anthropic API key (Claude fallback) |
| `dr_url` | NumBridge URL (default: `http://127.0.0.1:8765/mcp`) |

Open ⚙ Settings in the UI to set these. They survive page refreshes and browser
restarts until browser storage is cleared.

### Session Management

NumBridge sessions expire after roughly 20 consecutive `get_cell` calls. The
`readColMap` function reinitialises the session at column 17 (midway through the
32-column header read). The `readPortfolio` row scan reinitialises before starting
and every 20 calls via the `nbGet` helper. The main fetch loop reinitialises once
before writing begins.

### Rate Limiting

- **Alpha Vantage free tier**: 13-second minimum between calls (`avThrottle`)
- **Claude API**: token-bucket rate limiter (`waitForTokenBudget`) tracks input
  tokens used in the last 60 seconds against a 30k/minute limit. Token estimate
  seeds at 20k and calibrates to the median of recent calls.
- **FMP and Yahoo**: no throttling — FMP has no stated per-minute limit on the
  free tier; Yahoo is unofficial with no documented limit.

### Versioning

The version string is displayed in the page header (`v{major}.{minor}.{patch} ·
{date}`). **Claude Desktop owns the version string** — it must be incremented
whenever Claude Desktop produces a new version of the file:
- **Patch** bump: bug fixes
- **Minor** bump: new features
- **Major** bump: architectural changes

When making any change to `dividend-refresher.html`, always update the version
string and date before saving. Current version: **v1.6.1 · 2026-05-14**.

### What Claude Code Should and Should Not Change

**Safe to change:**
- NumBridge URL default in `HARDCODED_NUMBRIDGE`
- Model name in `MODEL` constant
- Token budget constants (`TOKEN_LIMIT`, `HEADROOM`, `OUTLIER_CAP`)
- AV throttle interval `AV_MIN_MS` if rate limits change
- Bug fixes in any fetch source (`fetchFromAlphaVantage`, `fetchFromFMP`,
  `fetchFromYahoo`, `fetchFromClaude`)
- CSS styling

**Do not change without consulting Claude Desktop first:**
- The cascade order in `fetchDivDates` — source priority was set deliberately
- `readColMap` column detection logic — fragile due to dynamic month headers
- Session reinit strategy in `readPortfolio` — tuned to avoid NumBridge timeouts
- The `resultUsable()` definition — encodes business logic about what counts as
  "current" dividend data
- The `getPortfolioSheets` newline-split logic — depends on NumBridge returning
  newline-delimited sheet names; document this assumption if NumBridge changes
