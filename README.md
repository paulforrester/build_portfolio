# build_portfolio

Reads a Fidelity brokerage position export (CSV) and builds a fully populated Apple Numbers portfolio tracking document — directly via AppleScript, no intermediary server required.

## Tools in this repository

| Tool | Description |
|------|-------------|
| `build_portfolio.py` | Reads a Fidelity CSV and builds a new Numbers portfolio document |
| `refresh_dividends.py` | Updates dividend data (ex-div dates, pay dates, monthly amounts) in an open Numbers document |
| `categorize_portfolio.py` | Classifies all positions by GICS sector and market cap, writes breakdown tables to the Summary sheet |
| `dividend-refresher.html` | Browser-based equivalent of `refresh_dividends.py` — requires NumBridge running |

## What it produces

Five sheets in the output `.numbers` file (Summary is always first):

| Sheet | Contents |
|---|---|
| **Summary** | Cross-sheet overview: Market Value, Cost Basis, Gain/Loss, % Gain, and % of Total for each account bucket plus a grand total. Live formulas reference the four portfolio sheets. |
| **Portfolio** | Taxable (brokerage) equity and fund positions — trust, CMA, joint, etc. — sorted by value. Includes tax rate rows below the totals. |
| **Portfolio-Cash** | Brokerage cash instruments: money market funds, T-Bills, CDs, and direct deposit entries. No tax rows. |
| **Portfolio-IRA** | Traditional IRA, rollover IRA, IRA BDA, and 401k positions, aggregated by symbol across accounts. Shows each position as a % of total IRA value. |
| **Portfolio-ROTH** | Roth IRA positions, aggregated by symbol. Shows each position as a % of total Roth value. |

**Pie chart**: the Summary sheet includes an instruction cell prompting you to select `A1:B5 → Insert → Chart → Pie`. This takes about 5 seconds and only needs to be done once — the chart references live data after that.

**Money market funds** (SPAXX, FZDXX, etc.) are not aggregated — each account gets its own row in Portfolio-Cash labelled `SYMBOL - Account Name` (e.g. `SPAXX - Trust: Under Agreement`).

**401k positions** have no ticker symbol; price and market value are taken directly from the CSV rather than fetched via `STOCK()`.

**Cost basis overrides**: if `_basis.json` exists in the same directory as the script, those values override missing or zero cost basis from the CSV before the `last_price` placeholder fallback is used. Copy `_basis-example.json` to `_basis.json` and fill in your data. The file is gitignored to prevent accidentally committing personal financial data.

Monthly dividend columns self-update every month via Numbers `MONTHNAME(NOW())` formulas — no script changes needed.

## Requirements

- macOS with **Apple Numbers** installed
- Python 3
- `pip install requests` — required by `refresh_dividends.py` and `categorize_portfolio.py`
- `pip install yfinance` — optional but recommended; used by `categorize_portfolio.py` for live ETF sector data
- `pip install anthropic` — optional; enables Claude API dividend gap-filling in `build_portfolio.py` and `refresh_dividends.py`

**API keys** (optional for `build_portfolio.py`; see individual tool sections for what each tool needs):

| Key | Used by | Free tier | Where to get one |
|-----|---------|-----------|-----------------|
| `ANTHROPIC_API_KEY` | all three Python tools (Claude fallback) | pay-per-token | [console.anthropic.com](https://console.anthropic.com) |
| `FMP_KEY` | `categorize_portfolio.py` (required), `refresh_dividends.py` | 250 req/day | [financialmodelingprep.com/register](https://financialmodelingprep.com/register) |
| `AV_KEY` | `refresh_dividends.py` (1st source) | 25 req/day | [alphavantage.co](https://www.alphavantage.co/support/#api-key) |

Keys can be set as environment variables or stored in `~/.dividend_refresher/config.json` (shared by all three Python tools):

```json
{
  "av_key": "YOUR_AV_KEY",
  "fmp_key": "YOUR_FMP_KEY",
  "anthropic_api_key": "YOUR_ANTHROPIC_KEY"
}
```

This file lives in your home directory by design — outside the repository — so API keys cannot be accidentally committed.

## Setup

1. Clone or download this repository.
2. Open `Portfolio Template.numbers` in Numbers and keep it accessible (the script reads it on each run).
3. Export your positions from Fidelity:  
   **Accounts & Trade → Portfolio → Positions → Download** (CSV format).

## Usage

```bash
# Basic — auto-derives document name from the CSV filename
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv

# Specify output location or document name
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --output-dir ~/Desktop
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --doc-name "My Portfolio May 2026"

# Use a different template file
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --template "My Template.numbers"

# Build a subset of sheets
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --brokerage-only  # Portfolio + Portfolio-Cash
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --equity-only     # Portfolio only
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --cash-only       # Portfolio-Cash only
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --ira-only
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --roth-only

# Preview what would be built without touching Numbers
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --dry-run

# Skip the Claude API dividend lookup even if ANTHROPIC_API_KEY is set
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --no-dividend-fill
```

Output defaults to `~/Desktop/{document name}.numbers`. If a file with that name already exists it is not overwritten — the script appends ` (2)`, ` (3)`, etc. and warns you.

## Template preparation

The script requires `Portfolio Template.numbers` (in the same directory, or specify with `--template`) with sheets named `_template1` through `_template6`. It consumes four of them in order (`_template1`→Portfolio, `_template2`→Portfolio-Cash, `_template3`→Portfolio-IRA, `_template4`→Portfolio-ROTH) and deletes the rest.

Each `_templateN` sheet must contain a table named **My Portfolio** where:

- **Row 1** — column headers (Symbol, Name, Shares, Price, …)
- **Row 2** — formula patterns using column-letter refs (e.g. `=IFERROR(STOCK(B2,0),"–")`). **Row 2 must have no data values** — if Numbers has converted the column-letter refs into named refs (e.g. `Shares QQQM`), clear the cells and re-enter the formulas with column letters.
- **Row 3** — totals pattern (read but not used; can be blank)

Column widths and number formats set on the template sheets are preserved in the output.

## refresh_dividends.py

Updates dividend data for all equity positions across all Portfolio sheets in an open Numbers document. For each symbol, it fetches the current ex-dividend date, pay date, annual dividend per share, dividends per year, and the four rolling monthly dividend amount columns, then writes the results back and sorts each sheet by pay date.

Tries four data sources in cascade order, using the first that returns current data: **Alpha Vantage → FMP → Yahoo Finance → Claude API** with web search. Cross-sheet deduplication means each unique symbol (e.g. VTI held in both Portfolio and Portfolio-IRA) is looked up exactly once and written to all sheets. `Portfolio-Cash` is excluded entirely. T-Bills and money market funds are always skipped.

**`--doc` is required.** Use the document name as shown in Numbers' title bar — no `.numbers` suffix. Keys are read from environment variables or `~/.dividend_refresher/config.json` (see Requirements above).

```bash
python3 refresh_dividends.py --doc "Portfolio May 2026"
python3 refresh_dividends.py --doc "Portfolio May 2026" --force        # re-fetch even if dates are current
python3 refresh_dividends.py --doc "Portfolio May 2026" --preview      # fetch but don't write to Numbers
python3 refresh_dividends.py --doc "Portfolio May 2026" --no-claude    # skip Claude API fallback
python3 refresh_dividends.py --doc "Portfolio May 2026" --amounts-only # recalculate monthly amounts only
```

The script skips positions whose existing sheet dates are already in the current month or later — pass `--force` to override this check on every symbol.

## categorize_portfolio.py

Classifies all portfolio positions by GICS sector and market cap tier, then writes `Sector Breakdown` and `Cap Breakdown` tables to the Summary sheet. The document must already be open in Numbers. Results are also printed to stdout so you can review them without writing.

For ETFs, four sources are tried in order: **FMP ETF holders** (paid tier) → **yfinance** sector weightings (free) → **Claude API** with web search → **hardcoded Q1-2026 approximations**. Individual stocks use FMP profile then fall back to Claude API. 401k fund positions without ticker symbols are classified by description keyword (e.g. "S&P 500", "2030", "bond", "international").

**`FMP_KEY` is required** (free tier: 250 req/day). `ANTHROPIC_API_KEY` is optional but strongly recommended — it covers cases where FMP and yfinance both fail. Keys are read from `~/.dividend_refresher/config.json` (same file as `refresh_dividends.py`).

`--doc` is optional — if omitted, the script auto-detects the first open Numbers document that has Portfolio sheets.

```bash
python3 categorize_portfolio.py
python3 categorize_portfolio.py --doc "Portfolio May 2026 (13).numbers"
python3 categorize_portfolio.py --preview   # print breakdown to stdout only
```

## dividend-refresher.html

A browser-based equivalent of `refresh_dividends.py`, retained as an alternative and for reference. Open it directly in any browser (`file://…`) — no local server needed.

**Requires NumBridge running** at `http://127.0.0.1:8765/mcp` — the browser cannot call `osascript` directly so it drives Numbers through the NumBridge MCP server instead. For most use cases `refresh_dividends.py` is preferred since it runs natively with no browser and no NumBridge dependency.

Uses the same four-source cascade (AV → FMP → Yahoo → Claude) and cross-sheet deduplication logic as `refresh_dividends.py`. API keys are stored in the browser's `localStorage` via the ⚙ Settings panel.

## Privacy note

The Fidelity CSV export contains real account names, positions, and balances. The `.gitignore` in this repo excludes `*.csv`, `_basis.json`, and generated `Portfolio *.numbers` files. **Never commit those files.**

API keys stored in `~/.dividend_refresher/config.json` live in your home directory by design — outside the repository — so they cannot be accidentally committed.
