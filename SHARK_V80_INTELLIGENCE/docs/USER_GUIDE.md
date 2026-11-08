# SVPI User Guide

## 1. What SVPI is

An independent auditor of SHARK V80 ENGINE's trading records. It reads files you export from
MT5 and tells you, with evidence, how the EA performed and whether it respected the approved
risk rules. It cannot see your account by itself. **When there is no new file, it says so and
asks for one.**

## 2. Weekly routine (5 minutes)

1. In MT5: **Toolbox → History**, choose the period (at least the last week), right-click →
   **Report → HTML**. Save into `SHARK_V80_INTELLIGENCE\data\incoming`.
2. Recommended: also copy the equity CSV written by `SVPI_EquityLogger` (from the MT5 Common
   Files folder), the EA's `MQL5\Logs\YYYYMMDD.log` files, and the `.set` file if settings changed.
3. On Sunday at 20:00 Malaysia time the scheduled task imports the files and writes the report
   to `reports\weekly\<account>\`. You can also run `scripts\run_weekly.ps1` at any time.
4. Open the dashboard (`scripts\start_dashboard.ps1`) → **Weekly reports** to read or download
   the PDF.

If nothing was uploaded, you get a **DATA MISSING** report listing exactly which files are
needed. It contains no metrics.

## 3. Reading a report

**Status**

| Status | Meaning | What to do |
|---|---|---|
| STABLE | No breaches, all key checks verified, ≥ 20 trades | Continue monitoring |
| NEEDS ATTENTION | A non-critical breach, an UNVERIFIABLE check, small sample, deterioration or a HIGH finding | Read the reasons and next actions; usually upload missing data or investigate |
| TEMPORARILY STOP LIVE TRADING | A verified critical breach (hard stop, weekly loss lock, max layers, martingale, max equity DD) or drawdown ≥ 10% | Pause live trading and investigate on demo before resuming |
| DATA MISSING | No records for the period | Upload the MT5 report |

**Verdicts:** **BREACH** is proven by your records. **UNVERIFIABLE** means the data needed to
check the rule is missing. It is *not* a pass. Daily locks stay UNVERIFIABLE until an equity
history sampled at ≤ 15 minutes is uploaded. **COMPLIANT** means the rule was checked with
sufficient data.

**Labels:** `CALCULATED` from your records · `VERIFIED` read directly from a record ·
`ESTIMATED` an approximation, labelled (e.g. the closed-trade drawdown, a lower bound) ·
`UNAVAILABLE` the input does not exist · `UNDEFINED` mathematically undefined (e.g. profit
factor with no losses = ∞).

**Findings** are written as FACT (from the records) and HYPOTHESES (possible explanations
that need checking). SVPI never claims broker manipulation or EA malfunction without evidence.

## 4. Dashboard pages

1. **Overview**: balance, equity, KPIs, status reasons, check verdicts, three major findings.
2. **Performance**: cumulative P/L, balance/equity curves, daily/weekly bars, win/loss mix.
3. **Strategy analysis**: BREAKOUT vs MOMENTUM vs PULLBACK, classification coverage.
4. **Risk monitor**: compliance table, basket risk, layers, underwater curve, daily locks.
5. **Trade history**: one row per position, with filters and a CSV download.
6. **Weekly reports**: generate, compare weeks, download PDF/CSV/Markdown.
7. **Data management**: upload, validation reports, missing information, EA configuration
   confirmation, settings, backup, confirmed deletion, audit log.
8. **AI analyst**: narrative interpretation (offline by default).

Sidebar: choose the account and a week, month, all data or a custom range (server time).

## 5. Strategy identification

SVPI recognises strategies only from: EA log lines containing the ticket and a strategy tag;
the order comment (`BRK`/`BREAKOUT`, `MOM`/`SCALP`/`MOMENTUM`, `PB`/`PULLBACK`); or a
magic-number map you enter in **Settings**. It never guesses from profit or direction. If the
broker truncates comments, upload logs or enter the magic map.

Basket/layer identification is best when the comment contains a basket id and layer (e.g.
`SV80 BRK L2 bsk=1234`). Otherwise baskets are **inferred** from overlapping positions and
labelled as such.

## 6. EA configuration

The approved baseline (MODERATE: 3 layers, 0.50/0.25/0.25%, basket 1.00%, daily lock 2%,
hard stop 3%, weekly lock 5%, max equity DD 10%, daily target 1.5%, martingale off) is the
**reference**. Upload the EA's `.set` file to see a drift table (MATCH / DIFFERENT /
LOOSER_THAN_APPROVED). If parameter names are not recognised, enter the actual values under
**Data management → EA configuration**. Changing the approved baseline in **Settings**
requires a confirmation tick and is audited.

## 7. Privacy and AI

All data stays on your PC. External AI is **off**. If you enable it in Settings, each
analysis still needs a per-run tick. Only an anonymised summary of metrics is sent (no
trades, no account number). Answers that contain numbers not in your report, suggest bigger
lots, or claim account access are rejected automatically. Scheduled reports never send data
anywhere.

## 8. Maintenance

- Backups: automatic before every weekly run and before any deletion. Manual backup on
  Data management → Maintenance. Files go to `database\backups`.
- Delete one imported file: type `DELETE SOURCE <id>`. Delete everything: type
  `DELETE ALL SVPI DATA` (a backup is taken first).
- Logs: `logs\svpi_production.log`, `logs\scheduler_*.log`, `logs\weekly_run_*.json`.
- Broker server timezone (default UTC) can be changed in Settings. It defines day and week
  boundaries.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| File REJECTED "missing required columns" | Export includes the listed columns; see DATA_DICTIONARY.md |
| "SYNTHETIC test data cannot be imported" | You uploaded a demo/test file into production |
| DUPLICATE_FILE | The identical file was imported before. Nothing to do |
| Balance continuity breaks | The export is partial or deals are missing; export a longer period |
| Locks always UNVERIFIABLE | Upload equity history (≤ 15-minute sampling) |
| Deals stored under UNSPECIFIED | CSV had no account; choose the account when uploading or run with `--account` |
| Scheduled task did not run | PC was off: the task runs at next logon (`StartWhenAvailable`); check `logs\scheduler_*.log` |
