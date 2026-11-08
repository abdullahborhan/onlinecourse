# SHARK V80 PERFORMANCE INTELLIGENCE (SVPI)

Independent, **read-only** quantitative audit system for the **SHARK V80 ENGINE** MT5 Expert
Advisor (Deriv Volatility 80). SVPI imports MT5 reports, reconstructs positions from deals,
computes performance, drawdown, risk-compliance, strategy, layering, execution-quality and
anomaly analytics, compares periods, and produces a weekly report with an explicit status:
**STABLE / NEEDS ATTENTION / TEMPORARILY STOP LIVE TRADING** (or **DATA MISSING**).

What SVPI will never do:

- connect to MT5 or a broker, ask for or store credentials;
- modify the EA, execute trades, or recommend increasing lot size;
- invent numbers. Missing inputs are reported as **UNAVAILABLE** / **UNVERIFIABLE**, never as 0
  or as "compliant".

## Requirements

- Windows 10/11, Python **3.12+** (python.org installer with the `py` launcher)
- PowerShell 5.1 (built in) or PowerShell 7

## Installation (PowerShell)

```powershell
cd C:\path\to\SHARK_V80_INTELLIGENCE
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
# optional external AI analyst (off by default):
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -WithAI -SkipTests
```

`setup.ps1` creates `.venv`, installs `requirements.txt`, creates the folders and runs the
test suite. If you need to do the same by hand:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

## Daily use

```powershell
# Dashboard (http://localhost:8501, local only)
powershell -ExecutionPolicy Bypass -File .\scripts\start_dashboard.ps1

# Weekly workflow now (imports data\incoming, writes reports\weekly\<account>\...)
powershell -ExecutionPolicy Bypass -File .\scripts\run_weekly.ps1
# ...for one account (also attributes account-less CSV/log files to it)
powershell -ExecutionPolicy Bypass -File .\scripts\run_weekly.ps1 -ExtraArgs "--account","12345678"
# Report for a specific week
.\.venv\Scripts\python.exe run_weekly.py --account 12345678 --week 2026-10-05

# Schedule: every Sunday 20:00 Malaysia time (Asia/Kuala_Lumpur)
powershell -ExecutionPolicy Bypass -File .\scripts\register_weekly_task.ps1
Start-ScheduledTask -TaskName "SHARK V80 Performance Intelligence - Weekly Report"   # test now
powershell -ExecutionPolicy Bypass -File .\scripts\unregister_weekly_task.ps1        # remove
```

`run_weekly.py` exit codes: `0` reports generated, `2` DATA MISSING (upload required), `1` error.

## Getting data out of MT5

| Data | How | Enables |
|---|---|---|
| **Detailed account history (HTML)** — required | Toolbox → History → right-click → *Report* → HTML | Positions, deals, balance, costs |
| Deal history CSV with `Position` / `Magic` columns | an export script or EA transaction log | Partial-close detection, magic numbers |
| Equity history CSV (`Time,Balance,Equity`) | optional read-only logger `tools/mql5/SVPI_EquityLogger.mq5` on a separate chart | Equity drawdown, daily/weekly lock **verification** |
| EA Experts/Journal logs | `MQL5\Logs\YYYYMMDD.log` (keep the file name) | Strategy tags, spread, slippage, rejections |
| EA `.set` file | chart → Expert properties → *Save* | Configuration drift vs approved baseline |

Put files in `data\incoming` (weekly job) or upload them on **Data Management**.

## Synthetic demo (clearly labelled, separate database)

```powershell
$env:SVPI_ENV = "demo"
.\.venv\Scripts\python.exe scripts\generate_demo_data.py
powershell -ExecutionPolicy Bypass -File .\scripts\start_dashboard.ps1 -Demo
Remove-Item Env:SVPI_ENV
```

Demo and test data go to `database\demo_SYNTHETIC.db` / test databases, are marked
`SVPI_SYNTHETIC_TEST_DATA`, and are **rejected by the production importer**.

## Documentation

- [docs/USER_GUIDE.md](docs/USER_GUIDE.md): operating manual and how to read a report
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): modules, data flow, database schema, team roles
- [docs/METRICS.md](docs/METRICS.md): every formula, definition and verdict rule
- [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md): tables, columns and input formats

## Project layout

```
app.py                 Streamlit dashboard (8 pages)
run_weekly.py          weekly workflow CLI (scheduler entry point)
config.py              approved risk baseline, status criteria, analysis settings
modules/               importers, reconstruction, engines, reports, AI analyst
dashboard/             dashboard pages
scripts/               PowerShell setup / scheduler scripts, demo generator
tools/mql5/            optional read-only equity logger
tests/                 pytest suite with hand-calculated synthetic fixtures
data/{incoming,processed,archive}   reports/{weekly,monthly}   database/   logs/
```
