"""PAGE 3 - Strategy analysis."""
import plotly.graph_objects as go
import streamlit as st

from dashboard.common import COLORS, analysis_for, data_missing_box, kpi, line, need_data
from modules.common import STRATEGIES, format_duration


def render(ctx):
    st.title("Strategy analysis — BREAKOUT vs MOMENTUM vs PULLBACK")
    if need_data(ctx):
        return
    res = analysis_for(ctx)
    if res.get("data_missing"):
        data_missing_box(res)
        return
    cls, s = res["classification"], res["strategy"]
    cov = cls.get("coverage_pct")
    if not cls.get("complete"):
        st.warning(f"Strategy classification INCOMPLETE: {cov or 0:.0f}% of positions "
                   "classified. Classification uses EA logs, EA comments or a magic-number map "
                   "only — never profit or direction.")
    st.caption(f"Classification sources: {cls.get('by_source')}")
    if cls.get("conflicts"):
        with st.expander(f"{len(cls['conflicts'])} classification conflict(s)"):
            st.write(cls["conflicts"])

    tbl = s["table"].set_index("strategy")
    cols = st.columns(3)
    for col, name in zip(cols, STRATEGIES):
        r = tbl.loc[name]
        with col:
            st.markdown(f"#### {name.title()}")
            if r["trades"] == 0:
                st.info("No trades in period.")
                continue
            pf = r["profit_factor"]
            kpi(st, "Net profit", f"{r['net_profit']:,.2f}", f"{int(r['trades'])} trades")
            st.write("")
            kpi(st, "Win rate · PF", f"{r['win_rate_pct']:.1f}% · "
                f"{'∞' if pf == float('inf') else f'{pf:.2f}'}",
                f"expectancy {r['expectancy']:.2f} · avg hold "
                f"{format_duration(r['avg_holding_s'])}")
    c1, c2 = st.columns(2)
    known = s["table"][s["table"]["trades"] > 0]
    with c1:
        fig = go.Figure(go.Bar(x=known["strategy"], y=known["net_profit"],
                               marker_color=[COLORS.get(x, "#64748b") for x in known["strategy"]]))
        fig.update_layout(title="Net profit by strategy")
        st.plotly_chart(line(fig, 300), use_container_width=True)
    with c2:
        fig = go.Figure()
        fig.add_trace(go.Bar(x=known["strategy"], y=known["win_rate_pct"], name="Win rate %",
                             marker_color=COLORS["accent"]))
        fig.add_trace(go.Bar(x=known["strategy"], y=known["dd_window_contribution"],
                             name="Contribution in max-DD window", marker_color=COLORS["neg"]))
        fig.update_layout(title="Win rate & drawdown contribution", barmode="group")
        st.plotly_chart(line(fig, 300), use_container_width=True)
    closed = res["closed_trades_period"]
    if not closed.empty:
        fig = go.Figure()
        for name, g in closed.sort_values("close_time").groupby("strategy"):
            fig.add_trace(go.Scatter(x=g["close_time"], y=g["net_profit"].cumsum(), name=name,
                                     line=dict(color=COLORS.get(name, "#64748b"))))
        fig.update_layout(title="Cumulative net P/L by strategy")
        st.plotly_chart(line(fig), use_container_width=True)
    st.subheader("Comparison table")
    st.dataframe(s["table"], hide_index=True, use_container_width=True)
    for k, label in (("most_profit", "Most profit"), ("most_risk", "Most risk"),
                     ("most_drawdown_contribution", "Largest drawdown contribution")):
        if s.get(k):
            st.markdown(f"**{label}:** {s[k]}")
    base = res["comparison"].get("historical_strategy_baseline")
    if base:
        st.caption(f"Historical strategy baseline (stored reports): {base}")
