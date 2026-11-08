# SVPI Metric Definitions

All metrics are computed by deterministic Python. `ccy` = account currency.

## Trades

- A **trade** is a reconstructed **position**, never a deal. Entry (IN) deals are never counted.
- A position is **CLOSED** when closed volume = opened volume; **PARTIALLY_CLOSED** and
  **OPEN** positions are excluded from closed-trade statistics.
- A position whose entry happened before the imported history (**orphan exit**) counts as
  closed with `entry_known = False`. Its duration and entry price are UNAVAILABLE.
- Period membership: performance uses positions **closed** in `[start, end)`; risk and
  layering use positions **opened or closed** in the period.
- **Net result** = profit + commission + swap + fee. Missing cost types are excluded, and the
  net-profit metric notes how many trades lack them.

## Outcome classification

`WIN` net > ε · `LOSS` net < −ε · `BREAKEVEN` |net| ≤ ε (ε = `breakeven_epsilon`, default 0).
Breakevens are in the denominator of win/loss rate and excluded from gross profit/loss.
A breakeven trade ends both winning and losing streaks.

## Profitability

| Metric | Formula |
|---|---|
| Net profit | Σ net |
| Gross profit / loss | Σ net of WIN / Σ net of LOSS (negative) |
| Profit factor | gross profit / \|gross loss\|; **UNDEFINED (∞)** if gross loss = 0 and gross profit > 0; UNDEFINED if both 0 |
| Total return % | net profit / balance at period start × 100 (UNAVAILABLE without a balance level) |
| Average profit per trade | net profit / closed trades |
| Expectancy | P(win)·avg win + P(loss)·avg loss (breakeven contributes 0); equals average profit per trade |
| Average winning / losing trade | mean net of WIN / LOSS |
| Largest winning / losing trade | max WIN / min LOSS |
| Payoff ratio | avg win / \|avg loss\| (realised reward:risk) |
| Recovery factor | net profit / max balance drawdown (money); UNDEFINED if no drawdown |

## Statistics

Win rate = wins / closed × 100 · Loss rate = losses / closed × 100 · Win rate excl. breakeven =
wins / (wins + losses) · Average duration = mean(close − open) for entry-known trades ·
Average trades per day = closed / calendar days in period · per active day = closed / days
with ≥1 close.

## Drawdown

- **Balance drawdown** (CALCULATED): on the realised balance curve: the deal-level `Balance`
  column, or derived from inception, or cumulative P/L only (then % is UNAVAILABLE).
  Deposits/withdrawals are removed: `pl_t = balance_t − cumulative_flows_t`;
  `dd_t = max(pl_≤t) − pl_t`; `dd%_t = dd_t / (balance at peak + flows since peak)`.
  A new peak starts only when the curve strictly exceeds its previous maximum.
- **Equity drawdown** (CALCULATED): same method on imported equity observations only;
  accuracy is limited by their sampling interval. UNAVAILABLE otherwise.
- **Closed-trade equity estimate** (ESTIMATED): balance drawdown re-labelled as a **lower
  bound**. It is never presented as actual equity drawdown.
- **Recovery duration**: trough → first time the curve regains the peak ("NOT RECOVERED").
- **Drawdown frequency**: number of peak-to-recovery episodes deeper than
  `drawdown_episode_min_pct` (default 1%).
- **Absolute drawdown**: period-start value − lowest flow-adjusted value (≥ 0).

## Risk

- **Point value** per symbol = median(profit / ((exit − entry)·dir·closed volume)) across
  closed trades; usable only if coefficient of variation < 2%. No contract size is assumed.
- **SL-implied risk** = |entry − SL| × volume × point value; **risk %** = risk / balance at
  open. UNAVAILABLE without SL, balance or point value.
- **Basket risk** = Σ layer risk %. **Planned R:R** = |TP − entry| / |entry − SL|.
- **Exposure**: event timeline of opens/closes (closes processed first at equal timestamps).

## Compliance checks (approved baseline: MODERATE)

| Check | BREACH when | COMPLIANT requires | Otherwise |
|---|---|---|---|
| daily_loss_lock (2%) | a position opens after cumulative realised (or observed equity) loss for the day reached the lock | equity samples ≤ 900 s for each trading day | UNVERIFIABLE |
| daily_hard_stop (3%) | day loss reached 3% (realised or equity) | as above | UNVERIFIABLE |
| daily_profit_lock (1.5%) | a position opens after the day's gain reached the target (if `profit_target_is_lock`) | as above | UNVERIFIABLE |
| weekly_loss_lock (5%) | cumulative period loss reached 5% (entries after it are listed) | equity observations in period | UNVERIFIABLE |
| max_layers (3) | any basket has > 3 positions | baskets identifiable | — |
| max_basket_risk (1%) | basket risk > 1% × (1 + 10% tolerance) or a layer above its 0.50/0.25/0.25% allocation | risk computable for all baskets | UNVERIFIABLE |
| max_equity_drawdown (10%) | equity DD ≥ 10%, or closed-trade DD alone ≥ 10% | equity observations | UNVERIFIABLE |
| martingale (disabled) | ≥ 2 positions with volume ≥ 1.5× the previous same-layer position that lost | trade volumes | — |
| stop_loss_presence | any position without SL at entry | SL recorded for all | UNVERIFIABLE |
| lot_escalation | volume > 2× historical median for its layer | ≥ 5 trades of history | UNVERIFIABLE |

Day boundaries are broker **server** days (`server_timezone`, default UTC). Day-start balance
is the last balance before 00:00, or the balance after leading deposits at 00:00.

## Layering

- Baskets: **EXPLICIT** (EA comment `basket`/`bsk` id; layer `L1`/`layer=2`) or
  **INFERRED_OVERLAP** (same symbol, direction and magic, overlapping in time).
- **Floating P/L at add** = Σ (new entry − existing entry) × dir × existing volume × point
  value. Negative → layer added **AGAINST** the move; otherwise **CONTINUATION**.
- **Disproportionate layering**: layers 2+ share of gross loss > 1.5 × their volume share.

## Strategy

Classification: EA log (ticket + strategy tag) > comment pattern > user magic map;
otherwise UNCLASSIFIED. **Most risk** = largest gross loss; **drawdown contribution** = net
P/L of a strategy's trades closed inside the account's max balance drawdown window.

## Execution quality

Cost drag = |costs| / gross profit before costs (material at ≥ 10% or if costs turn a
pre-cost profit into a loss). Entry slippage = (fill − logged requested price) × dir
(positive = adverse). Exit deviation = (fill − SL/TP level) × dir for `[sl]`/`[tp]` exits.
Delay = order log line − preceding signal log line (same clock, ≤ 60 s window).

## Anomaly tests (need ≥ 30 historical closed trades unless stated)

| Test | Method | Flag |
|---|---|---|
| Trade frequency | z of period daily rate vs historical daily mean/SD | z > 3 |
| Lot escalation | volume > max(2×median, p99) of history | any |
| Holding time | > Q3 + 3·IQR of history | any |
| Win-rate drop | one-sided two-proportion z-test | p < 0.05 |
| Deterioration | Welch t on net per trade (normal approx.) | p < 0.05 |
| Strategy mix | χ² (Wilson–Hilferty p) or categories absent from history | p < 0.05 |
| Losing streak (no history needed) | P(run ≥ L in n trials at loss rate p), exact DP | P < 0.01 and L ≥ 3 |
| Exposure (rule) | max simultaneous positions > max layers | any |
| Unexpected drawdown | vs ≥ 4 stored period drawdowns: z > 3 or > 1.5 × max | any |

## Status

See `modules/status_engine.py`. **STOP**: verified breach of a critical check (max equity
DD, daily hard stop, max layers, martingale, weekly loss lock), drawdown ≥ 10%, or a CRITICAL
finding. **NEEDS ATTENTION**: other verified breaches, any UNVERIFIABLE check, < 20 closed
trades, PF decline ≥ 30% vs previous period, HIGH/CRITICAL finding, incomplete classification.
**STABLE**: none of these. Missing data never yields STOP by itself.
