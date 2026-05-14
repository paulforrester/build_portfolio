# build_portfolio

Reads a Fidelity brokerage position export (CSV) and builds a fully populated Apple Numbers portfolio tracking document — directly via AppleScript, no intermediary server required.

## What it produces

Two sheets in the output `.numbers` file:

| Sheet | Contents |
|---|---|
| **Portfolio** | Brokerage (taxable) positions, sorted by value. Includes tax rate rows below the totals. |
| **Portfolio-IRA** | IRA + Roth positions, aggregated by symbol across accounts. Shows each position as a % of total IRA value. |

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

# Build only one sheet
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --brokerage-only
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --ira-only

# Preview what would be built without touching Numbers
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --dry-run

# Skip the Claude API dividend lookup even if ANTHROPIC_API_KEY is set
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --no-dividend-fill
```

Output defaults to `~/Desktop/{document name}.numbers`. If a file with that name already exists it is not overwritten — the script appends ` (2)`, ` (3)`, etc. and warns you.

## Template preparation

The script requires `Portfolio Template.numbers` (in the same directory, or specify with `--template`) with sheets named `_template1` through `_template6`. Each sheet must contain a table named **My Portfolio** where:

- **Row 1** — column headers (Symbol, Name, Shares, Price, …)
- **Row 2** — formula patterns using column-letter refs (e.g. `=IFERROR(STOCK(B2,0),"–")`). **Row 2 must have no data values** — if Numbers has converted the column-letter refs into named refs (e.g. `Shares QQQM`), clear the cells and re-enter the formulas with column letters.
- **Row 3** — totals pattern (read but not used; can be blank)

Column widths and number formats set on the template sheets are preserved in the output.

## Privacy note

The Fidelity CSV export contains real account names, positions, and balances. The `.gitignore` in this repo excludes `*.csv` and generated `Portfolio *.numbers` files. **Never commit those files.**
