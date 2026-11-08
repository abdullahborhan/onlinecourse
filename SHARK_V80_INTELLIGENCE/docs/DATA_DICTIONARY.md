# SVPI Data Dictionary

## Input formats

### MT5 HTML report (Detailed Account History / statement)
Encoding UTF-16 or UTF-8. Sections recognised: *Positions*, *Orders*, *Deals*,
*Open Positions*, *Working Orders*, *Results*. Header rows `Name:`, `Account:`
(`12345678 (USD, Deriv-Server, real, Hedge)`), `Company:`, `Date:`.
**The MT5 Deals table has no Position ID or Magic column**: positions then come from the
Positions table (partial-close detection UNAVAILABLE). The Deals totals row is excluded. The
stated *Total Net Profit* is cross-checked against the deals.

### CSV deal history (kind `csv_deals`)
Required: `Deal|Ticket`, `Time`, `Type`, `Volume`, `Price`, `Profit`.
Optional: `Entry|Direction` (in/out/inout/out by or 0–3), `Position`, `Magic`, `Order`,
`Commission`, `Swap`, `Fee`, `Balance`, `SL`, `TP`, `Comment`, `Symbol`.
Delimiter `,` `;` tab or `|`; with `;` a decimal comma is assumed.

### CSV positions (kind `csv_positions`)
Required: `Position|Ticket`, open `Time`, `Type`, `Volume`, open `Price`, close `Time`,
close `Price`, `Profit` (a duplicated `Time`/`Price` header is read as close time/price).

### Equity history (kind `csv_equity`)
Required: `Time`, `Equity`; optional `Balance`. No `Symbol`/`Price` columns.

### EA logs (kind `ea_log`)
Terminal log lines (`CS 0 10:15:01.234 SHARK_V80 (Volatility 80 Index,M1) message`, with the
date taken from a `YYYYMMDD` file name) or full timestamps. Optional CSV logs with
`time,strategy,ticket,event,message,...`. Extracted when the text states them: strategy
(`BREAKOUT|BRK|BO`, `MOMENTUM|SCALP|MOM`, `PULLBACK|PB`), ticket/position/order, `signal id=`,
`layer=`, `spread=`, `requested price=`, event type.

### EA `.set`
`Name=Value[||start||step||stop||Y]`. Parameters are mapped by configurable patterns. The
mapping is shown for confirmation, and unmapped items are reported.

### Accepted timestamp formats
`YYYY.MM.DD HH:MM[:SS[.fff]]`, ISO (`YYYY-MM-DD[ T]HH:MM[:SS]`), `YYYY/MM/DD`,
`DD.MM.YYYY`, `DD/MM/YYYY` vs `MM/DD/YYYY` (detected; ambiguous files are flagged and read
day-first), Unix epoch (s or ms). All times are stored as naive broker **server** time.

## Canonical tables

### deals
| Column | Type | Notes |
|---|---|---|
| account_id | text | from report header or upload selection |
| deal_id | text | MT5 deal ticket (PK with account) |
| time | text | `YYYY-MM-DD HH:MM:SS` server time |
| symbol | text | NULL for balance operations |
| deal_type | text | buy, sell, balance, credit, charge, correction, bonus, commission |
| entry | text | IN, OUT, INOUT, OUT_BY, UNKNOWN, NULL (not provided) |
| volume, price | real | lots, price |
| order_id, position_id, magic | text | NULL when not provided |
| commission, swap, fee, profit | real | NULL = not provided (never 0) |
| balance | real | balance after the deal (MT5 column) |
| sl, tp | real | 0 = not set; NULL = unknown |
| comment | text | EA comment |
| source_file_id | int | → source_files.id |
| raw_json | text | original row text |

### report_positions
`position_id, open_time, symbol, direction, volume, open_price, sl, tp, close_time,
close_price, commission, swap, profit, comment, source_file_id, raw_json`.

### equity_points
`account_id, time, balance, equity, source_file_id`.

### ea_events
`time, event_type (SIGNAL|ORDER|CLOSE|REJECTION|LOCK|INFO), strategy, signal_id, ticket, layer,
spread, requested_price, message, source_file_id`.

### reports
`period_type (WEEKLY|MONTHLY|CUSTOM), period_start, period_end, status, is_data_missing,
data_fingerprint, metrics_json, report_json, md_path, pdf_path, csv_path, superseded`.

## Reconstructed position fields (analysis frame)

`position_id, direction, open_time, close_time, volume, volume_closed, entry_price` (VWAP of IN
deals), `exit_price` (VWAP of OUT deals), `sl, tp, sl_status (SET|NOT_SET|UNKNOWN), profit,
commission, swap, fee, net_profit, costs_complete, n_exits, partial_close, status
(CLOSED|PARTIALLY_CLOSED|OPEN), exit_reason (SL|TP|STOP_OUT|UNKNOWN), source
(DEALS|REPORT_POSITIONS), entry_known, duration_s, balance_at_open, balance_basis, risk_money,
risk_pct, strategy, strategy_source, basket_id, basket_method, layer, layer_source`.

## Output files

`reports/weekly/<account>/SVPI_WEEKLY_<account>_<start>_<end>.{md,pdf,json}`,
`..._metrics.csv`, `..._trades.csv`, and `..._DATA_MISSING.{md,pdf}` when data is absent.
Synthetic outputs are prefixed `SYNTHETIC_`.
