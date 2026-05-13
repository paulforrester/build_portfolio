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
1. **Static values**: `set value of range "A2:Zn"` with a 2D AppleScript list (formula cells written as `""`)
2. **Formulas**: one batched script with `set formula of cell "X2" to "IFERROR(...)"` for every formula cell (no leading `=`)

Total: ~4 `osascript` calls per sheet (resize/clear, header row, batch data, totals + tax).

### Operation Order in `build_sheet`
1. `resize_table_as` + `clear_data_rows_as` (JXA) — prepare the pre-formatted `_templateN` sheet
2. `_write_single_row_as` — header row with month-name overrides
3. `_write_rows_as_batch` — all data rows (static pass + formula pass)
4. `_write_single_row_as` — totals row
5. `_write_tax_rows_as` — Portfolio only (cell-level format set here)
6. `rename_sheet_as` — renames `_templateN` to the final sheet name

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
After all sheets are written, if `ANTHROPIC_API_KEY` is set and `--no-dividend-fill` is not passed, `fill_dividends` calls the Claude API (claude-opus-4-7 with web_search tool) to look up dividend data for equity positions and writes results back to Numbers.
