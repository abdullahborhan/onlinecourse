# SVPI Architecture

## Principles

1. **Deterministic core.** Every number is produced by plain Python (pandas/NumPy). The AI
   layer only interprets an aggregated fact sheet and can never override a metric.
2. **Epistemic status travels with data.** Metrics are `Metric(value, status, unit, basis, note)`
   with status `VERIFIED | CALCULATED | ESTIMATED | UNDEFINED | UNAVAILABLE`. Compliance checks
   return `BREACH | COMPLIANT | UNVERIFIABLE | NOT_APPLICABLE`. Findings separate **FACT** from
   **HYPOTHESES**.
3. **NULL is not zero.** Values absent from a source stay NULL end to end.
4. **Local-first and read-only.** No broker connectivity, no credentials, no trading.
5. **Provenance.** Every row keeps its source file id and raw text; originals are archived
   byte-for-byte; every import, report, setting change and destructive action is audited.

## Data flow

```
data/incoming ─► mt5_importer ─► parsing_utils / html_parser / csv_parser /
   (or upload)        │            ea_log_parser / set_parser ─► normalizer
                      ▼
               data_validator (validation report, balance continuity, results cross-check)
                      ▼
                SQLite (database.py) ◄── archive original, audit_log
                      ▼
          analysis_service.run_analysis(account, period)
             ├─ trade_reconstruction  deals → positions (partial closes, orphan exits)
             ├─ point values + SL-implied risk at entry
             ├─ strategy_analyzer     classification (EA log > comment > magic map)
             ├─ layering_analyzer     baskets (explicit / inferred), layers, exposure
             ├─ performance_engine    profitability + statistics
             ├─ drawdown_engine       balance DD, equity DD, closed-trade estimate
             ├─ risk_analyzer         compliance checks + risk metrics
             ├─ execution_quality     costs, spread, slippage, rejections, delay
             ├─ anomaly_detector      statistical/rule-based FACT + HYPOTHESIS findings
             ├─ comparison_engine     vs previous stored report (+ limitations)
             └─ status_engine         explicit configurable status rules
                      ▼
        report_generator (MD / PDF / CSV / JSON, DATA MISSING variant) → reports/
        dashboard/* (Streamlit)        ai_analyst (optional interpretation)
        weekly_workflow + run_weekly.py ◄── Windows Task Scheduler (Sun 20:00 MYT)
```

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Approved risk baseline (`RiskConfig`), analysis parameters, status criteria, AI/notification settings, paths, environment isolation (`SVPI_ENV`) |
| `modules/common.py` | `Metric`, `Finding`, status vocabularies, formatting |
| `modules/parsing_utils.py` | Decoding (UTF-16/UTF-8), numbers, timestamp format detection with day/month ambiguity flag |
| `modules/html_parser.py` | MT5 HTML report sections, account header, stated Results |
| `modules/csv_parser.py` | Delimiter / decimal detection, CSV kind detection |
| `modules/normalizer.py` | Column alias mapping onto canonical deal/position/order/equity schemas |
| `modules/ea_log_parser.py` | EA journal/expert logs → events (strategy, ticket, layer, spread, rejections) |
| `modules/set_parser.py` | `.set` parsing, parameter mapping, drift vs approved baseline |
| `modules/data_validator.py` | Validation reports, balance continuity, stated-results cross-check |
| `modules/mt5_importer.py` | Orchestrates import; SHA-256 de-duplication; archive; synthetic guard |
| `modules/database.py` | SQLite schema and access; backup; confirmed reset/delete; report versions |
| `modules/trade_reconstruction.py` | Position lifecycle; point value; SL-implied risk |
| `modules/performance_engine.py` | Profitability and trading statistics |
| `modules/drawdown_engine.py` | Flow-adjusted drawdown measures and episodes |
| `modules/risk_analyzer.py` | Compliance checks, daily table, risk metrics |
| `modules/layering_analyzer.py` | Basket assignment, layer statistics, layering behaviour findings |
| `modules/strategy_analyzer.py` | Strategy classification and comparison |
| `modules/execution_quality.py` | Costs and log-based execution metrics |
| `modules/anomaly_detector.py` | z-scores, two-proportion test, Welch t, χ², streak probability |
| `modules/comparison_engine.py` | Period comparison with compatibility limitations |
| `modules/status_engine.py` | Status determination |
| `modules/analysis_service.py` | Orchestration, completeness, major findings, next actions |
| `modules/report_generator.py` | 14-section report, DATA MISSING report, PDF rendering |
| `modules/weekly_workflow.py` | Scheduled workflow, back-fill, monthly report, notifications |
| `modules/ai_analyst.py` | Fact sheet, offline analyst, optional Claude call, guardrails |
| `modules/notifications.py` | Optional log/e-mail upload requests |
| `modules/audit_logger.py` | Rotating log file + audit table |

## Database schema (SQLite, `database/performance.db`)

| Table | Key | Purpose |
|---|---|---|
| `meta` | key | schema version, environment |
| `source_files` | id, **sha256 unique** | every imported file, kind, status, validation JSON, archive path, synthetic flag |
| `account_info` | account_id | name, currency, server, company from report header |
| `deals` | (account_id, **deal_id**) | normalised MT5 deals + raw JSON + source file |
| `report_positions` | (account_id, **position_id**) | MT5 HTML/CSV position rows |
| `orders` | (account_id, order_id) | MT5 order rows |
| `equity_points` | (account_id, time) | imported equity/balance observations |
| `ea_events` | id, unique(account, time, message) | parsed EA log lines |
| `ea_configs` | id | `.set` / user-provided EA configuration (one active) |
| `reports` | id | stored reports with metrics JSON, status, fingerprint, file paths, `superseded` flag |
| `audit_log` | id | audit trail |

Duplicate handling: identical files are refused by SHA-256; overlapping exports are merged
by natural keys (`INSERT OR IGNORE`); regenerating a report for the same period marks older
versions `superseded`; unchanged inputs (same fingerprint) skip regeneration.

## Five-agent development organisation

Development was organised by role ownership. Shared production modules (`common.py`,
`database.py`, `analysis_service.py`, `config.py`) change only through the Chief Architect.

| Agent | Owns | Acceptance gate |
|---|---|---|
| 1 Chief Architect | `config.py`, `common.py`, `analysis_service.py`, `status_engine.py`, `app.py`, docs | Integration review; full suite green before each commit |
| 2 Quantitative Analyst | `performance_engine.py`, `anomaly_detector.py`, `comparison_engine.py`, `strategy_analyzer.py`, `METRICS.md` | Hand-calculated expectations in `test_performance.py`, `test_strategy.py`, `test_comparison.py` |
| 3 Data Engineer | importers, parsers, `normalizer.py`, `data_validator.py`, `database.py`, `trade_reconstruction.py` | `test_importer.py`, `test_reconstruction.py`, `test_database.py` |
| 4 Risk Engineer | `drawdown_engine.py`, `risk_analyzer.py`, `layering_analyzer.py`, `execution_quality.py` | `test_drawdown.py`, `test_risk.py`, `test_layering.py` |
| 5 QA Engineer | `tests/`, synthetic fixtures, `report_generator.py` checks, dashboard smoke test | 87 tests passing; synthetic data never reaches production |

## Extension points

- **AI provider:** `ai_analyst._call_claude` is the only network code; swap or disable it
  without touching any metric.
- **Notifications:** `notifications.send_notification` (log or SMTP).
- **New input formats:** add aliases to `normalizer.py` or a handler to `MT5Importer`.
- **Live data integration:** not implemented. Adding one must keep the importer as the only
  write path and must be verified before SVPI may claim account access.
