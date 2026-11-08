"""
Rule-based and statistical anomaly detection.

Each anomaly is a ``Finding`` with a FACT (numbers from imported records) and
HYPOTHESES (possible explanations that need verification). The detector never
asserts market/broker manipulation or EA malfunction. When the history is too
short for a statistical baseline, that is reported instead of an anomaly.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from modules.common import Finding


def norm_sf(z: float) -> float:
    """Upper-tail probability of the standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def prob_streak_at_least(n: int, k: int, p: float) -> float:
    """P(at least one run of >= k consecutive losses in n Bernoulli(p) trials)."""
    if k <= 0:
        return 1.0
    if n < k or p <= 0:
        return 0.0
    # state = current run length (< k); absorbing when reaching k
    state = np.zeros(k)
    state[0] = 1.0
    absorbed = 0.0
    for _ in range(n):
        new = np.zeros(k)
        new[0] = state.sum() * (1 - p)
        new[1:] = state[:-1] * p
        absorbed += state[-1] * p
        state = new
    return float(min(1.0, absorbed))


def two_proportion_z(x1, n1, x2, n2) -> float | None:
    if n1 == 0 or n2 == 0:
        return None
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return None if se == 0 else (x1 / n1 - x2 / n2) / se


def welch_t(a: pd.Series, b: pd.Series) -> float | None:
    if len(a) < 2 or len(b) < 2:
        return None
    se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    return None if se == 0 else (a.mean() - b.mean()) / se


def chi_square_p(stat: float, dof: int) -> float:
    """Wilson–Hilferty approximation of the chi-square upper tail."""
    if dof <= 0:
        return 1.0
    z = ((stat / dof) ** (1 / 3) - (1 - 2 / (9 * dof))) / math.sqrt(2 / (9 * dof))
    return norm_sf(z)


def detect_anomalies(current: pd.DataFrame, history: pd.DataFrame, *, period_days: float,
                     max_layers: int, z_thr: float, min_hist: int, alpha: float,
                     eps: float = 0.0, current_dd_pct: float | None = None,
                     historical_dd_pcts: list | None = None, exposure: dict | None = None
                     ) -> dict:
    cur = current[current["status"] == "CLOSED"] if not current.empty else current
    hist = history[history["status"] == "CLOSED"] if not history.empty else history
    F: list[Finding] = []
    info: list[str] = []
    out = {"findings": F, "info": info, "baseline_trades": int(len(hist))}

    # Exposure (rule-based, no history needed)
    if exposure and exposure.get("available") and exposure["max_positions"] > max_layers:
        F.append(Finding("anomaly", "HIGH",
                         f"Up to {exposure['max_positions']} positions were open simultaneously "
                         f"(at {exposure['time_of_max']}); approved basket maximum is "
                         f"{max_layers}.",
                         ["Several baskets may have been open at once (e.g. different "
                          "strategies).", "A basket may have exceeded its layer limit."],
                         {"max_positions": exposure["max_positions"]}, "EXCESSIVE_EXPOSURE"))

    # Consecutive losses vs what the loss rate implies
    if len(cur) >= 5:
        outcomes = np.where(cur["net_profit"] > eps, "W", np.where(cur["net_profit"] < -eps,
                                                                    "L", "B"))
        run = best = 0
        for o in outcomes:
            run = run + 1 if o == "L" else 0
            best = max(best, run)
        base = hist if len(hist) >= min_hist else cur
        p_loss = float((base["net_profit"] < -eps).mean()) if len(base) else 0.0
        prob = prob_streak_at_least(len(cur), best, p_loss) if best else 1.0
        out["max_losing_streak"] = best
        out["streak_probability"] = prob
        if best >= 3 and prob < 0.01:
            F.append(Finding("anomaly", "HIGH",
                             f"Losing streak of {best} trades in {len(cur)} trades. Given a loss "
                             f"rate of {p_loss * 100:.1f}% ({'history' if base is hist else 'this period'}"
                             f"), a streak this long has probability {prob:.4f}.",
                             ["Market regime change unfavourable to the strategies.",
                              "Entry filter behaviour changed (verify .set and logs)."],
                             {"streak": best, "probability": prob}, "ABNORMAL_LOSING_STREAK"))

    if len(hist) < min_hist:
        info.append(f"Statistical baseline unavailable: {len(hist)} historical closed trades "
                    f"(minimum {min_hist}). Frequency, lot, duration, win-rate and strategy-mix "
                    "anomaly tests were not run.")
        return out
    if cur.empty:
        return out

    # Trade frequency
    hist_days = max((hist["close_time"].max() - hist["close_time"].min()).total_seconds() / 86400,
                    1.0)
    daily = hist.groupby(hist["close_time"].dt.normalize()).size()
    daily = daily.reindex(pd.date_range(hist["close_time"].min().normalize(),
                                        hist["close_time"].max().normalize()), fill_value=0)
    mu, sd = daily.mean(), daily.std(ddof=1)
    cur_rate = len(cur) / max(period_days, 1)
    if sd and sd > 0:
        z = (cur_rate - mu) / (sd / math.sqrt(max(period_days, 1)))
        out["frequency_z"] = z
        if z > z_thr:
            F.append(Finding("anomaly", "MEDIUM",
                             f"Trade frequency {cur_rate:.2f}/day vs historical {mu:.2f}/day "
                             f"(z = {z:.1f}).",
                             ["Higher volatility producing more signals.",
                              "EA settings changed (e.g. signal filters relaxed)."],
                             {"current_per_day": cur_rate, "historical_per_day": mu},
                             "TRADE_FREQUENCY_SPIKE"))

    # Lot-size escalation
    med, p99 = hist["volume"].median(), hist["volume"].quantile(0.99)
    big = cur[cur["volume"] > max(2 * med, p99)]
    if len(big):
        F.append(Finding("anomaly", "HIGH",
                         f"{len(big)} trade(s) with volume above both 2x the historical median "
                         f"({med:g}) and the historical 99th percentile ({p99:g}); max "
                         f"{big['volume'].max():g}.",
                         ["Balance growth increasing risk-based lot size.",
                          "Lot sizing parameter changed."],
                         {"positions": big["position_id"].tolist()[:20]}, "LOT_ESCALATION"))

    # Holding duration
    hd = hist["duration_s"].dropna()
    cd = cur["duration_s"].dropna()
    if len(hd) >= min_hist and len(cd):
        q1, q3 = hd.quantile(0.25), hd.quantile(0.75)
        upper = q3 + 3 * (q3 - q1)
        long_ = cd[cd > upper]
        if len(long_):
            F.append(Finding("anomaly", "LOW",
                             f"{len(long_)} trade(s) held longer than the historical outlier "
                             f"bound ({upper / 60:.1f} min).",
                             ["Trades held through low-volatility periods without hitting "
                              "SL/TP."], {"count": int(len(long_))}, "UNUSUAL_HOLDING_TIME"))

    # Win-rate reduction (two-proportion z-test, one-sided)
    w_c, n_c = int((cur["net_profit"] > eps).sum()), len(cur)
    w_h, n_h = int((hist["net_profit"] > eps).sum()), len(hist)
    z = two_proportion_z(w_c, n_c, w_h, n_h)
    if z is not None:
        p = 1 - norm_sf(z)  # P(Z <= z)
        out["win_rate_test"] = {"z": z, "p_value": p, "current": w_c / n_c, "history": w_h / n_h}
        if z < 0 and p < alpha:
            F.append(Finding("anomaly", "HIGH",
                             f"Win rate fell to {w_c / n_c * 100:.1f}% from a historical "
                             f"{w_h / n_h * 100:.1f}% (one-sided p = {p:.4f}).",
                             ["Market regime change.", "Strategy parameters changed."],
                             {"z": z, "p": p}, "WIN_RATE_DROP"))

    # Performance deterioration (expectancy, Welch t)
    t = welch_t(cur["net_profit"], hist["net_profit"])
    if t is not None:
        p = 1 - norm_sf(t)
        out["expectancy_test"] = {"t": t, "p_value_approx": p,
                                  "current": float(cur["net_profit"].mean()),
                                  "history": float(hist["net_profit"].mean())}
        if t < 0 and p < alpha:
            F.append(Finding("anomaly", "HIGH",
                             f"Average net per trade {cur['net_profit'].mean():.2f} vs historical "
                             f"{hist['net_profit'].mean():.2f} (Welch t = {t:.2f}, "
                             f"p ≈ {p:.4f}).",
                             ["Performance deterioration; verify configuration and market "
                              "conditions before drawing conclusions."],
                             {"t": t}, "PERFORMANCE_DETERIORATION"))

    # Strategy distribution change (chi-square)
    if "strategy" in cur:
        hp = hist["strategy"].value_counts(normalize=True)
        cc = cur["strategy"].value_counts()
        cats = sorted(set(hp.index) | set(cc.index))
        exp = np.array([hp.get(c, 0) * len(cur) for c in cats])
        obs = np.array([cc.get(c, 0) for c in cats])
        mask = exp > 0
        new_cats = [c for c, e, o in zip(cats, exp, obs) if e == 0 and o > 0]
        if len(cur) >= 10 and (mask.sum() >= 2 or new_cats):
            stat, p = None, None
            if mask.sum() >= 2:
                stat = float(((obs[mask] - exp[mask]) ** 2 / exp[mask]).sum())
                p = chi_square_p(stat, int(mask.sum()) - 1)
            out["strategy_mix_test"] = {"chi2": stat, "p_value_approx": p, "new": new_cats}
            if (p is not None and p < alpha) or new_cats:
                test_txt = f" (χ² = {stat:.1f}, p ≈ {p:.4f})" if p is not None else ""
                F.append(Finding("anomaly", "MEDIUM",
                                 "Strategy distribution differs from history" + test_txt
                                 + (f"; categories absent from history: {new_cats}"
                                    if new_cats else "") + ".",
                                 ["Market conditions favouring different setups.",
                                  "Strategy enable/disable settings changed."],
                                 {"current": cc.to_dict(), "historical_share": hp.round(3).to_dict()},
                                 "STRATEGY_MIX_CHANGE"))

    # Unexpected drawdown vs historical period drawdowns
    hdd = [x for x in (historical_dd_pcts or []) if x is not None]
    if current_dd_pct is not None and len(hdd) >= 4:
        m, s = float(np.mean(hdd)), float(np.std(hdd, ddof=1))
        z = (current_dd_pct - m) / s if s > 0 else None
        if (z is not None and z > z_thr) or current_dd_pct > 1.5 * max(hdd):
            F.append(Finding("anomaly", "HIGH",
                             f"Period drawdown {current_dd_pct:.2f}% vs historical period mean "
                             f"{m:.2f}% (max {max(hdd):.2f}%).",
                             ["Larger exposure or adverse market movement; review layering."],
                             {"z": z}, "UNEXPECTED_DRAWDOWN"))
    return out
