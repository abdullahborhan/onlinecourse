"""PAGE 4 - Risk monitor."""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.common import (COLORS, analysis_for, data_missing_box, line, metrics_df,
                              need_data, verdict_tag)


def render(ctx):
    st.title("Risk monitor")
    if need_data(ctx):
        return
    res = analysis_for(ctx)
    if res.get("data_missing"):
        data_missing_box(res)
        return
    cfg = ctx["cfg"]
    st.caption(f"Reference: {cfg.risk.source} — approved baseline, not proof of actual EA "
               "configuration. BREACH = proven; UNVERIFIABLE = data missing (not compliance).")
    rows = "".join(
        f"<tr><td><b>{c['check']}</b></td><td>{verdict_tag(c['verdict'])}</td>"
        f"<td>{c['summary']}</td><td style='color:#94a3b8'>{c['basis']}</td></tr>"
        for c in res["risk"]["checks"])
    st.markdown(f"<table style='width:100%;font-size:0.85rem'>{rows}</table>",
                unsafe_allow_html=True)

    st.subheader("Basket exposure & layering")
    lay = res["layering"]
    if not lay.get("available"):
        st.info("No positions in period.")
    else:
        b = lay["baskets"]
        c1, c2 = st.columns(2)
        with c1:
            if b["basket_risk_pct"].notna().any():
                fig = go.Figure(go.Bar(x=b["basket_id"], y=b["basket_risk_pct"],
                                       marker_color=[COLORS["neg"] if v > cfg.risk.max_basket_risk_pct
                                                     else COLORS["accent"]
                                                     for v in b["basket_risk_pct"].fillna(0)]))
                fig.add_hline(y=cfg.risk.max_basket_risk_pct, line_dash="dash",
                              line_color=COLORS["warn"], annotation_text="max basket risk")
                fig.update_layout(title="SL-implied basket risk (% of balance)")
                st.plotly_chart(line(fig, 300), use_container_width=True)
            else:
                st.info("Basket risk UNAVAILABLE (no SL / balance / point value).")
        with c2:
            fig = go.Figure(go.Histogram(x=b["layers"], marker_color=COLORS["accent"]))
            fig.add_vline(x=cfg.risk.max_layers + 0.5, line_dash="dash",
                          line_color=COLORS["neg"])
            fig.update_layout(title="Layers per basket")
            st.plotly_chart(line(fig, 300), use_container_width=True)
        st.caption(f"Basket identification: {lay.get('basket_methods')} — INFERRED baskets "
                   "are grouped by time overlap, not by EA identifiers.")
        if not lay["layers"].empty:
            st.dataframe(lay["layers"], hide_index=True, use_container_width=True)
        for f in lay.get("findings", []):
            d = f.to_dict() if hasattr(f, "to_dict") else f
            st.warning(f"**{d['severity']}** — {d['fact']}")

    st.subheader("Drawdown")
    curve = res["drawdown"]["full"]["curve"]
    if not curve.empty:
        pl = curve["pl"]
        dd = pl - pl.cummax()
        fig = go.Figure(go.Scatter(x=curve["time"], y=dd, fill="tozeroy",
                                   line=dict(color=COLORS["neg"])))
        fig.update_layout(title="Underwater curve — realised, flow-adjusted (account history)")
        st.plotly_chart(line(fig, 280), use_container_width=True)
    st.dataframe(metrics_df(res["drawdown"]["metrics"]), hide_index=True,
                 use_container_width=True)

    st.subheader("Daily loss / profit-lock compliance")
    daily = res["risk"]["daily"]
    if isinstance(daily, pd.DataFrame) and not daily.empty:
        st.dataframe(daily, hide_index=True, use_container_width=True)
        if "realised_worst_pct" in daily:
            fig = go.Figure()
            fig.add_trace(go.Bar(x=daily["day"], y=-daily["realised_worst_pct"],
                                 name="Worst realised intraday %", marker_color=COLORS["neg"]))
            if "realised_best_pct" in daily:
                fig.add_trace(go.Bar(x=daily["day"], y=daily["realised_best_pct"],
                                     name="Best realised intraday %",
                                     marker_color=COLORS["pos"]))
            for y, lbl in ((-cfg.risk.daily_loss_lock_pct, "loss lock"),
                           (-cfg.risk.daily_hard_stop_pct, "hard stop"),
                           (cfg.risk.daily_profit_target_pct, "profit target")):
                fig.add_hline(y=y, line_dash="dot", annotation_text=lbl)
            st.plotly_chart(line(fig, 300), use_container_width=True)
    st.subheader("Risk metrics")
    st.dataframe(metrics_df(res["risk"]["metrics"]), hide_index=True, use_container_width=True)
