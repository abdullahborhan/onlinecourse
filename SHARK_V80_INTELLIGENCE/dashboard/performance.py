"""PAGE 2 - Performance."""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.common import COLORS, analysis_for, data_missing_box, line, metrics_df, need_data
from modules.performance_engine import period_breakdown


def render(ctx):
    st.title("Performance")
    if need_data(ctx):
        return
    res = analysis_for(ctx)
    if res.get("data_missing"):
        data_missing_box(res)
        return
    closed = res["closed_trades_period"]
    db = ctx["db"]

    c1, c2 = st.columns(2)
    with c1:
        if closed.empty:
            st.info("No closed trades in period.")
        else:
            cum = closed.sort_values("close_time")
            fig = go.Figure(go.Scatter(x=cum["close_time"], y=cum["net_profit"].cumsum(),
                                       mode="lines", line=dict(color=COLORS["accent"]),
                                       name="Cumulative net P/L"))
            fig.update_layout(title="Cumulative net P/L (closed trades)")
            st.plotly_chart(line(fig), use_container_width=True)
    with c2:
        curve = res["drawdown"]["period"]["curve"]
        eq = db.load_equity(ctx["account"], ctx["start"], ctx["end"])
        fig = go.Figure()
        if not curve.empty:
            fig.add_trace(go.Scatter(x=curve["time"], y=curve["balance"], name="Balance",
                                     line=dict(color=COLORS["muted"], shape="hv")))
        if not eq.empty:
            fig.add_trace(go.Scatter(x=eq["time"], y=eq["equity"], name="Equity (observed)",
                                     line=dict(color=COLORS["accent"])))
        fig.update_layout(title="Balance and equity curves")
        st.plotly_chart(line(fig), use_container_width=True)
        if eq.empty:
            st.caption("Equity curve UNAVAILABLE — no equity history uploaded. The balance "
                       "curve is realised (closed-trade) balance only.")

    c1, c2 = st.columns(2)
    for col, freq, title in ((c1, "D", "Daily net P/L"), (c2, "W-SUN", "Weekly net P/L")):
        with col:
            pb = period_breakdown(closed, freq) if not closed.empty else pd.DataFrame()
            if pb.empty:
                st.info(f"{title}: no data.")
                continue
            fig = go.Figure(go.Bar(x=pb["period"], y=pb["net_profit"],
                                   marker_color=[COLORS["pos"] if v >= 0 else COLORS["neg"]
                                                 for v in pb["net_profit"]]))
            fig.update_layout(title=title)
            st.plotly_chart(line(fig, 300), use_container_width=True)

    S = res["performance"]["statistics"]
    c1, c2 = st.columns([1, 2])
    with c1:
        vals = [S["winning_trades"].value, S["losing_trades"].value, S["breakeven_trades"].value]
        fig = go.Figure(go.Pie(labels=["Winning", "Losing", "Breakeven"], values=vals, hole=.55,
                               marker=dict(colors=[COLORS["pos"], COLORS["neg"],
                                                   COLORS["muted"]])))
        fig.update_layout(title="Winning vs losing trades")
        st.plotly_chart(line(fig, 300), use_container_width=True)
    with c2:
        if not closed.empty:
            fig = go.Figure(go.Histogram(x=closed["net_profit"], nbinsx=30,
                                         marker_color=COLORS["accent"]))
            fig.update_layout(title="Distribution of trade net results")
            st.plotly_chart(line(fig, 300), use_container_width=True)

    st.subheader("Profitability & statistics")
    perf = res["performance"]
    st.dataframe(metrics_df({**perf["profitability"], **perf["statistics"],
                             **perf.get("costs", {})}), hide_index=True,
                 use_container_width=True)
    for n in perf["notes"]:
        st.caption(n)
