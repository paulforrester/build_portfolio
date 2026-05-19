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
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --brokerage-only   # builds Portfolio + Portfolio-Cash
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --equity-only       # Portfolio only
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --cash-only         # Portfolio-Cash only
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --ira-only
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --roth-only
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --dry-run
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --no-dividend-fill

# Rebuild only the Summary sheet on an already-open document (no CSV needed):
python3 build_portfolio.py --summary-only --doc-name "Portfolio May 2026 (6)"
```

When `--summary-only` is used: `csv_file` is not required. The document must already be open in Numbers. `find_totals_row()` reads each sheet to discover the totals row, then calls `build_summary_sheet()` directly. Pass `--doc-name` with the exact document name as Numbers shows it (including any ` (2)` suffix).

The script requires:
1. Apple Numbers installed and accessible via `osascript`
2. A template Numbers document with sheets named `_template1`–`_template6` (and optionally a `_basis` sheet), each `_templateN` sheet containing a `My Portfolio` table — defaults to `"Portfolio Template.numbers"`

Optional: `ANTHROPIC_API_KEY` env var enables dividend gap-filling via Claude API.

## Architecture

### AppleScript Runners
- `run_applescript(script)` — short inline scripts via `osascript -e`
- `run_applescript_file(script)` — writes to a UTF-8 temp file and calls `osascript <file>` (avoids command-line length limits)
- `run_jxa_file(script)` — JXA (JavaScript for Automation) via `osascript -l JavaScript <file>`, used for reading because JXA returns JSON-parseable output

### Template Structure
The template has `_template1` through `_template6` sheets (all identical, pre-formatted with column widths, number formats, bold rows). The script consumes one per output sheet in this order:

| Template sheet | Output sheet |
|----------------|-------------|
| `_template1` | Portfolio |
| `_template2` | Portfolio-Cash |
| `_template3` | Portfolio-IRA |
| `_template4` | Portfolio-ROTH |

Unused template sheets (`_template5`–`_template6`) are deleted at the end. The script uses a `template_queue` list — whichever templates exist are sorted and assigned in order, so sheets are consumed even if some are missing.

`read_template` reads from `_template1` (falls back to `_template` for backward compatibility).

### `_basis` Sheet (Optional Cost Basis Overrides)
The template may contain a sheet named `_basis` with a table named `Basis`. This sheet is **never** deleted — it has no digit suffix so `TEMPLATE_SHEET_RE` (`^_templa\w*(\d+)$`) does not match it.

The `Basis` table must have these columns (row 1 = header, data from row 2):

| Column | Contents |
|--------|----------|
| A — Symbol | Ticker symbol (case-insensitive) |
| B — Account | Exact account name as it appears in the Fidelity CSV |
| C — Avg Cost Basis | Average cost per share (number or `$`-prefixed string) |
| D — Notes | Optional; ignored by the script |

`read_basis_overrides(template_doc_name)` reads this table via JXA after the template is open and returns `{(SYMBOL_UPPER, account_name): avg_cost_basis_float}`. In `parse_csv`, the override is applied when `avg_cost_basis` is missing or ≤ 0 for non-T-Bill positions — after the T-Bill cost basis derivation, before the `last_price` placeholder fallback. In dry-run mode the template is never opened, so `basis_overrides` is `{}`.

### Bulk Write Strategy (`_write_rows_as_batch`)
Two osascript calls per sheet for all data rows:
1. **Static values**: one batched script with `set value of cell {col} of row {r} to {value}` for every non-empty, non-formula cell. Numbers rejects mixed-type 2D lists, so `set value of range` and `set value of cells of row N to {list}` cannot be used.
2. **Formulas**: one batched script with `set value of cell {col} of row {r} to "=formula"` for every formula cell. Numbers parses strings beginning with `=` as formulas; `set formula of cell` is unreliable in this context.

Total: ~7 `osascript` calls per sheet (resize, clear body rows, header row, batch statics, batch formulas, footer/totals row, tax row; IRA sheets skip tax row).

### Template Structure — 3 rows
Each `_templateN` sheet has exactly 3 rows:
- **Row 1**: Header row (column names)
- **Row 2**: Data template row — formulas here are copied to every data position row
- **Row 3**: Footer row (Numbers table footer) — pre-built aggregate formulas (`=SUM(Shares)`, `=SUM(Market Value)`, etc.) that auto-sum all body rows

`read_template` reads `footerRowCount` from the template JXA and returns it so `build_sheet` can place the totals in the actual Numbers footer row.

### Operation Order in `build_sheet`
1. `resize_table_as` (JXA) — sets total row count = 1 header + n data rows + extra body rows + footer rows
2. `clear_data_rows_as` (JXA) — clears ONLY body rows (rows 2 to `total_rows − footer_row_count`); footer row is left intact so its template `=SUM(...)` formulas survive
3. `_write_single_row_as` — header row with month-name overrides
4. `_write_rows_as_batch` — all data rows (static pass + formula pass)
5. `_write_single_row_as` — writes sheet label and computed formulas (% Gain, Div %, etc.) into the **footer row** (`tot_row = total_rows`); simple column-sum cells keep their template formulas
6. `_write_tax_rows_as` — Portfolio only; writes to `tot_row − 1` (the body row immediately before the footer); rates written as pre-formatted strings ("16.44%") because Numbers `set format … to percentage` is unreliable
7. `rename_sheet_as` — renames `_templateN` to the final sheet name
7. `spot_check_sheet` — reads back key cells via JXA and prints row 2, totals, and T-Bill MV sanity check
8. Returns `tot_row` (the 1-based totals row index) — captured by `main()` and passed to `build_summary_sheet()`

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
| Bucket | Matches (normalized account name, case-insensitive) | Output sheet |
|--------|-----------------------------------------------------|--------------|
| ROTH | contains "roth" | Portfolio-ROTH |
| IRA | contains "ira" OR contains "401" | Portfolio-IRA |
| BROKERAGE | everything else (catch-all) | Portfolio |

Normalization: all non-alphanumeric characters → space, then lowercased. ROTH is checked first so "Roth IRA" accounts are not misclassified as IRA. The IRA bucket covers traditional IRA, rollover IRA, IRA BDA, and 401k accounts (the "401" substring catches "401(K)" after normalization). All non-tax-advantaged accounts — trust, CMA, joint checking, brokerage — fall into BROKERAGE.

### Formula Substitution
Row-2 template formulas (e.g., `=IFERROR(STOCK(B2,0),"–")`) are substituted for each data row `r` using:
```python
re.sub(r"([A-Z]+)2\b", lambda m: f"{m.group(1)}{r}", cell)
```

`_resolve_named_refs` converts Numbers internal named column refs (e.g., `Shares 16`) to cell-letter refs (e.g., `G2`) after reading the template. This is necessary because Numbers stores formulas using header-name refs internally.

### Sheet Differences
| | Portfolio | Portfolio-Cash | Portfolio-IRA | Portfolio-ROTH |
|---|---|---|---|---|
| Bucket | BROKERAGE equities | BROKERAGE cash | IRA (incl. 401k) | ROTH |
| Positions included | All non-cash brokerage positions | Money markets, T-Bills, CDs, direct deposit | All IRA/401k positions | All Roth positions |
| Column 15 header | "Gain %" | "Gain %" | "% of Portfolio" | "% of Portfolio" |
| Column 15 formula | `=IFERROR((D{r}-I{r})/I{r},"–")` | same as Portfolio | `=IFERROR(K{r}/K{tot_row},"–")` | same as IRA |
| Tax rate rows | yes (2 rows below totals) | no | no | no |
| Totals label (col A) | "Portfolio" | "Portfolio-Cash" | "Portfolio-IRA" | "Portfolio-ROTH" |
| Dividend fill | yes | no | yes | yes |

`is_ira=True` is passed to `build_sheet` for Portfolio-IRA and Portfolio-ROTH — controls "% of Portfolio" header override and suppresses tax rows.

`has_tax_rows` parameter (new): `None` defaults to `not is_ira`; pass `False` explicitly for Portfolio-Cash (which is `is_ira=False` but still gets no tax rows).

### Position Aggregation per Bucket
`aggregate_positions(all_positions, bucket)` processes one bucket at a time and returns rows in this order:

1. **Equities / funds** — aggregated by symbol (shares and cost_basis_total summed; weighted average cost basis recalculated). Sorted by current value descending.
2. **Money market funds** — *not* aggregated. Each account's holding becomes its own row. `display_symbol` is set to `"SYMBOL - Account Name"` (e.g. `"SPAXX - Trust: Under Agreement"`). Price is hardcoded as 1.00; Market Value is taken directly from the CSV.
3. **T-Bills and CDs** — *not* aggregated. Sorted by maturity date ascending.

**Missing cost basis**: if `cost_basis_total` is `None` when a symbol is first added to the equity accumulator, a `⚠` warning is printed and the value is treated as 0.

**Missing avg cost basis for equities**: cost basis lookup applies in this priority order:
1. `_basis` sheet override (highest priority, see `read_basis_overrides`)
2. Value from the CSV `Average Cost Basis` column (if > 0)
3. T-Bill cost basis derivation (T-Bills only: face value × quantity / 100)
4. `last_price` as a placeholder (so gain shows ~$0 instead of full value). Marked with `⚠` in the dry-run output.

### Brokerage Split: Portfolio vs Portfolio-Cash
After `aggregate_positions("BROKERAGE")`, the result is split by `is_cash_position(pos)`:
- **Portfolio** — positions where `is_cash_position` is `False` (equities and ETFs)
- **Portfolio-Cash** — positions where `is_cash_position` is `True`

```python
def is_cash_position(pos) -> bool:
    return (
        pos["is_money_market"]
        or pos["is_tbill"]
        or pos["is_cd"]
        or "direct deposit" in pos["description"].lower()
        or "money market" in pos["description"].lower()
    )
```

Cash positions get `pos["is_cash_instrument"] = True` before being passed to `build_sheet`. In `build_data_row`, cash instruments that are not money markets or T-Bills (e.g., direct deposit rows) use the same price=1.00 / cost-basis=MV logic as money markets (so Gain evaluates to $0).

`order_cash_positions(positions)` sorts Portfolio-Cash rows: money markets by value descending, then other cash, then T-Bills/CDs by maturity date ascending.

### 401k Position Handling
401k accounts (IRA bucket) have no ticker symbol — only a fund description. Detection: `is_401k = (not symbol) and bucket == "IRA"`.

In `build_data_row`, 401k positions short-circuit the normal formula path:
- **Col B (Symbol)**: empty string
- **Col C (Name)**: fund description from CSV
- **Col D (Price)**, **Col E (Price Change)**: hardcoded from CSV last price / last price change
- **Col F (% Change)**: computed as `price_change / price`
- **Col K (Market Value)**: hardcoded `current_value` from CSV (no live STOCK() formula)
- **Col J (Cost Basis)**: `avg_cost_basis × quantity` if available, else `cost_basis_total`
- All other formula columns (Gain, % Gain, Ann Div, etc.) keep their substituted formulas and evaluate correctly against the hardcoded static cells.

### Position Ordering
1. Equities/funds by current value descending
2. Money market funds (SPAXX, FZDXX, FZROX, FZILX, FCASH or description contains "money market")
3. T-Bills and CDs by maturity date ascending (parsed from description with `re.search(r"(\d{1,2}/\d{1,2}/\d{4})")`)

### T-Bill Handling
T-Bills are detected by: symbol matching `^\d{9}[A-Z]\d$` or description containing "treasury bill", "treas bills", etc. For T-Bills:
- Price (col D): hardcoded from CSV last price (no STOCK() formula)
- Market Value (col K): `=IFERROR(D{r}*(G{r}/100),"–")` — T-Bills are quoted per $100 face value

### Summary Sheet

After all four portfolio sheets are built, `build_summary_sheet()` creates a `Summary`
sheet positioned first in the document. It contains a `Portfolio Summary` table with
cross-sheet formula references to the totals row of each portfolio sheet and an
instruction cell prompting the user to add a pie chart manually.

**The totals row number** for each sheet is returned by `build_sheet()` and passed to
`build_summary_sheet()` as `tot_rows: dict`. When running `--summary-only`,
`find_totals_row()` discovers these dynamically by reading down column A until it finds
a cell containing `"Portfolio"`.

**Cross-sheet formula syntax:** `='SheetName'::My Portfolio::K31` — single-quoted sheet
names handle hyphens correctly (e.g. `'Portfolio-Cash'`).

**ROUND() in formulas:** All formula values are wrapped in `ROUND()` to avoid
floating-point precision noise — `ROUND(..., 2)` for currency cells, `ROUND(..., 4)`
for percentage cells (4 stored decimal places display as 2 when formatted as percent).

**Formatting — critical constraints discovered through testing:**

Numbers AppleScript has severe limitations on cell formatting. These are the only
patterns that work without `-2740` errors:

```applescript
-- ✓ WORKS: format type keywords (no decimal control)
set format of cell 2 of row r to currency
set format of cell 5 of row r to percent   -- NOTE: "percent" not "percentage"

-- ✗ FAILS with -2740: custom format strings
set format of cell 2 of row r to number format "$#,##0.00"

-- ✗ FAILS with -2740: font bold in any form
set font bold of cell c of row 1 to true
tell cell c / set font bold to true / end tell
```

**Do not attempt** to set decimal places, custom format strings, or bold formatting
via AppleScript in `build_summary_sheet()` — all known approaches generate `-2740`
errors. The `ROUND()` formulas compensate for the lack of decimal place control.
Bold formatting on the header and totals rows is not applied programmatically.

**Pie chart:** Cannot be created programmatically — Numbers does not expose chart
creation reliably via AppleScript or JXA. An instruction cell in row 9 prompts the
user to create it manually (select A1:B5 → Insert → Chart → Pie). This is a one-time
step per document since the chart references live data afterward.

**Formatting is wrapped in try/except** — if the formatting block fails for any reason,
a WARNING is printed and the script continues. The data itself is always written
correctly regardless of formatting success.

### Dividend Gap-Fill
After all sheets are written, if `ANTHROPIC_API_KEY` is set and `--no-dividend-fill` is not passed, `fill_dividends` calls the Claude API (claude-sonnet-4-20250514 with web_search tool) to look up dividend data for equity positions and writes results back to Numbers.

---

## dividend-refresher.html

A browser-based tool for updating dividend information in Apple Numbers portfolio
tracking documents. It communicates with Numbers via **NumBridge** (a local MCP
server at `http://127.0.0.1:8765/mcp`) rather than directly via AppleScript, because
browsers cannot call `osascript`. NumBridge must be running before opening the file.

**Prefer `refresh_dividends.py` for command-line use.** The HTML tool is retained as
a browser-based alternative and for reference — its cascade logic is the canonical
source of truth that `refresh_dividends.py` replicates.

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
excluded from fetching entirely. `Portfolio-Cash` is excluded entirely — it contains
only T-Bills, money markets, and cash instruments, none of which have dividends.

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

---

## refresh_dividends.py

Python command-line equivalent of `dividend-refresher.html`. Runs natively on macOS
via direct AppleScript (`osascript`) — no browser or NumBridge required. Uses the
same four-source cascade (AV → FMP → Yahoo → Claude) and cross-sheet deduplication
logic as the HTML tool. **Preferred over `dividend-refresher.html` for automated or
scripted use.**

### Running

`--doc` is required. Use the document name as shown in the Numbers title bar — no `.numbers` suffix.

```bash
python3 refresh_dividends.py --doc "Portfolio May 2026"
python3 refresh_dividends.py --doc "Portfolio May 2026" --force        # re-fetch even if dates current
python3 refresh_dividends.py --doc "Portfolio May 2026" --preview      # fetch but don't write
python3 refresh_dividends.py --doc "Portfolio May 2026" --no-claude    # skip Claude fallback
python3 refresh_dividends.py --doc "Portfolio May 2026" --amounts-only # recalc month amounts only
```

### API Keys

Keys are read in this priority order:
1. Environment variables: `AV_KEY`, `FMP_KEY`, `ANTHROPIC_API_KEY`
2. Config file: `~/.dividend_refresher/config.json`

```json
{
  "av_key": "...",
  "fmp_key": "...",
  "anthropic_api_key": "..."
}
```

### Numbers Access

Uses `run_applescript_file()` and `run_jxa_file()` — same functions as
`build_portfolio.py`. No NumBridge, no session management needed. AppleScript
reads and writes cells directly.

### Sheet Handling

Processes all sheets starting with `"Portfolio"` **except `"Portfolio-Cash"`**.
The `_basis` sheet is excluded automatically (doesn't start with "Portfolio").
`Summary` is excluded automatically (doesn't start with "Portfolio").

### Data Source Cascade

Identical cascade to `dividend-refresher.html` — AV → FMP → Yahoo → Claude.
`resultUsable()` definition must match the HTML exactly. See the HTML tool's
Data Source Cascade section for the canonical description.

### Cross-Sheet Deduplication

Same as `dividend-refresher.html` — all sheets read first, grouped by symbol,
each unique symbol fetched once, result applied to all rows across all sheets.

### Rate Limiting

- **Alpha Vantage**: 13-second `time.sleep()` between calls
- **Claude API**: token-bucket rate limiter — same logic as `waitForTokenBudget()`
  in the HTML tool
- **FMP / Yahoo**: no throttling

### Column Map

Same column positions as `dividend-refresher.html`. Read dynamically via JXA
from the header row of each sheet. 0-based indices; write calls use `colIdx + 1`.
Monthly columns (4 after Divs/year) derived as `divsYearCol + 1` — dynamic
`NOW()` headers return empty strings from JXA, so all 35 columns are read
unconditionally.

### What Must NOT Change Without Consulting Claude Desktop

- Cascade order (AV → FMP → Yahoo → Claude)
- `resultUsable()` logic — must match `dividend-refresher.html` exactly
- Stale-date fallback behaviour (keep existing sheet dates if fetch returns stale)
- Cross-sheet dedup logic
- Monthly amount calculation including noon-UTC timezone fix

---

## categorize_portfolio.py

Classifies all portfolio positions by GICS sector and market cap tier, writing two
summary tables (`Sector Breakdown`, `Cap Breakdown`) to the Summary sheet.

Uses FMP API for individual stock profiles and ETF holdings (top-25 holdings per
ETF, weighted). 401k funds without tickers use hardcoded classification by fund
description keyword. Requires `FMP_KEY`. Uses same config file as
`refresh_dividends.py` (`~/.dividend_refresher/config.json`).

**Requires NumBridge running** for writing tables. If NumBridge is unavailable the
script degrades gracefully: breakdown is printed to stdout, write is skipped.

```bash
python3 categorize_portfolio.py
python3 categorize_portfolio.py --doc "Portfolio May 2026 (13).numbers"
python3 categorize_portfolio.py --preview   # print only, don't write to Numbers
```

### ETF classification

Calls `/etf-holder/{symbol}` for the top-25 holdings by weight, then
`/profile/{holding}` for each to get `sector` and `mktCap`. Results are cached
in memory per run. Budget: ~26 FMP calls per unique ETF symbol (1 holder + 25
profiles). Individual stock profiles are also cached — QQQM appearing in both
Portfolio and Portfolio-IRA triggers only one set of API calls.

Known ETF list (pre-flagged without needing a profile call): `KNOWN_ETFS` constant.
Any symbol FMP returns with `isEtf: true` is also classified as ETF regardless.

### Market cap tiers

| Tier | Market Cap |
|------|-----------|
| Mega-Cap | > $200B |
| Large-Cap | $10B–$200B |
| Mid-Cap | $2B–$10B |
| Small-Cap | $250M–$2B |
| Micro-Cap | < $250M |

### 401k fund classification

`FUND_401K_CLASSIFICATIONS` dict maps lowercase substrings (e.g., `"s&p 500"`,
`"2030"`, `"bond"`, `"international"`) to `(sector_weights, cap_weights)` tuples.
Weights are normalized to 1.0 at runtime. No match → `{"Other": 1.0}`.

### Table placement (NumBridge)

Tables are created on the `Summary` sheet using `add_table`, then positioned with
`set_table_layout` relative to the existing `Portfolio Summary` table (to its
right). `Sector Breakdown` goes at `(summary.x + summary.width + 40, summary.y)`;
`Cap Breakdown` is placed below it. Column 2 formatted as `currency`, column 3 as
`percentage` via `set_column_format`. Existing tables with the same names are
removed and recreated on each run.

### "Other" category

Holds unclassified portions: ETF holdings outside the top-25, profiles not found
in FMP, and any position the script could not classify. Acceptable at < ~15% of
total portfolio value.
