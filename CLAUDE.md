# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

`build_portfolio.py` reads a Fidelity brokerage position export CSV and builds a fully populated Apple Numbers portfolio tracking document. It drives Numbers directly via **AppleScript** (`osascript` subprocess calls) — no external server required.

## Running the Script

```bash
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --template "Portfolio Template.numbers"
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --doc-name "My Portfolio"
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --brokerage-only
python3 build_portfolio.py Portfolio_Positions_May-08-2026.csv --ira-only
```

The script requires:
1. Apple Numbers installed and accessible via `osascript`
2. A template Numbers document open in Numbers with a sheet named `_template` and a table named `My Portfolio` — defaults to `"Portfolio Template.numbers"`

## Architecture

### AppleScript Runners
- `run_applescript(script)` — short inline scripts via `osascript -e`
- `run_applescript_file(script)` — writes to a UTF-8 temp file and calls `osascript <file>` (avoids command-line length limits)
- `run_jxa_file(script)` — JXA (JavaScript for Automation) via `osascript -l JavaScript <file>`, used for reading because JXA returns JSON-parseable output

### Template Reading (`read_template`)
- Single JXA call reads row counts, all header values (row 1), and formula strings (rows 2–3) from the `_template` sheet in one shot
- `cell.formula()` returns the formula string; value falls back to `cell.value()` for non-formula cells
- Formulas are normalised to include a leading `=`
- Totals row from template is **not used** — totals are always rebuilt from scratch because Numbers auto-converts cell refs to named references (`Market Value Portfolio`) that break when copied to other documents

### Bulk Write Strategy (`_write_rows_as`)
Two osascript calls per sheet for all rows:
1. **Static values**: `set value of cells of row N to {list}` for every row (formula cells written as `""`)
2. **Formulas**: one batched script with `set formula of cell C of row R to "=..."` for every formula cell

### Operation Order in `write_sheet`
1. `add_sheet_and_table_as` — creates sheet, captures default table refs, creates `My Portfolio` table, deletes old tables
2. Build header / data / totals rows in Python
3. `_write_rows_as` — static values pass, then formula pass (2 calls)
4. `_apply_formatting_as` — bold rows 1 and tot_row, column number formats, resize table (1 call)
5. `_write_tax_rows_as` — Portfolio only, written **after** column formats so cell-level format wins (1 call)

Total: ~5 `osascript` calls per sheet.

**Document naming**: Numbers saves documents to `~/Library/Mobile Documents/com~apple~Numbers/Documents/`. If a file with the same name already exists on disk, Numbers appends a numeric suffix (e.g., "Portfolio May 2026 2"). The script handles this by:
1. Closing any open document matching the target name
2. Deleting matching `.numbers` files from the iCloud Documents folder
3. Polling `list_documents` after `create_document` to detect the actual name (Numbers is async)

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

### Operation Order in `write_sheet`
1. `add_sheet`, `add_table`
2. Write header row
3. Overwrite month-name columns (derived from CSV filename: `May-08-2026.csv` → May/Jun/Jul/Aug 2026)
4. Write data rows
5. Write totals row + bold format
6. **Apply column formats** (must come before tax rows, or column-level format overrides cell-level)
7. Write tax rate rows (Portfolio only)
8. Resize table

### Default Sheets Cleanup
Numbers creates a default "Sheet 1" on new documents. The script collects default sheet names before adding new sheets, then deletes them at the very end (Numbers won't delete the last sheet in a document).
