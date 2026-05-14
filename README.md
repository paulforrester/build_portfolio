# build_portfolio

Reads a Fidelity brokerage position export (CSV) and builds a fully populated Apple Numbers portfolio tracking document — directly via AppleScript, no intermediary server required.

## What it produces

Four sheets in the output `.numbers` file:

| Sheet | Contents |
|---|---|
| **Portfolio** | Taxable (brokerage) equity and fund positions — trust, CMA, joint, etc. — sorted by value. Includes tax rate rows below the totals. |
| **Portfolio-Cash** | Brokerage cash instruments: money market funds, T-Bills, CDs, and direct deposit entries. No tax rows. |
| **Portfolio-IRA** | Traditional IRA, rollover IRA, IRA BDA, and 401k positions, aggregated by symbol across accounts. Shows each position as a % of total IRA value. |
| **Portfolio-ROTH** | Roth IRA positions, aggregated by symbol. Shows each position as a % of total Roth value. |

**Money market funds** (SPAXX, FZDXX, etc.) are not aggregated — each account gets its own row in Portfolio-Cash labelled `SYMBOL - Account Name` (e.g. `SPAXX - Trust: Under Agreement`).

**401k positions** have no ticker symbol; price and market value are taken directly from the CSV rather than fetched via `STOCK()`.

**Cost basis overrides**: if the template contains a `_basis` sheet with a `Basis` table (columns: Symbol, Account, Avg Cost Basis, Notes), those values override missing or zero cost basis from the CSV before the `last_price` placeholder fallback is used.

Monthly dividend columns self-update every month via Numbers `MONTHNAME(NOW())` formulas — no script changes needed.

## Requirements

- macOS with **Apple Numbers** installed
- Python 3 (stdlib only — no pip packages needed for the core script)
- Optional: `ANTHROPIC_API_KEY` environment variable to enable automatic dividend gap-filling via Claude API (requires `pip install anthropic`)

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

**Optional:** add a sheet named `_basis` with a table named `Basis` (columns: Symbol, Account, Avg Cost Basis, Notes). The script reads it to override missing cost basis values from the CSV. This sheet is never deleted by the script.

Each `_templateN` sheet must contain a table named **My Portfolio** where:

- **Row 1** — column headers (Symbol, Name, Shares, Price, …)
- **Row 2** — formula patterns using column-letter refs (e.g. `=IFERROR(STOCK(B2,0),"–")`). **Row 2 must have no data values** — if Numbers has converted the column-letter refs into named refs (e.g. `Shares QQQM`), clear the cells and re-enter the formulas with column letters.
- **Row 3** — totals pattern (read but not used; can be blank)

Column widths and number formats set on the template sheets are preserved in the output.

## Privacy note

The Fidelity CSV export contains real account names, positions, and balances. The `.gitignore` in this repo excludes `*.csv` and generated `Portfolio *.numbers` files. **Never commit those files.**
