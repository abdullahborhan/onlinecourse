"""PAGE 6 - Weekly reports (history, comparisons, downloads)."""
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.common import COLORS, line, status_badge
from modules.report_generator import generate_report


def _val(m, k):
    v = (m or {}).get(k)
    v = v.get("value") if isinstance(v, dict) else v
    return None if v in ("INF", "-INF") else v


def render(ctx):
    st.title("Weekly reports")
    db = ctx["db"]
    if ctx.get("account") and ctx.get("start") is not None and \
            ctx["period_type"] in ("WEEKLY", "MONTHLY"):
        c1, c2, c3 = st.columns([2, 1, 1])
        c1.markdown(f"Generate **{ctx['period_type']}** report for "
                    f"{ctx['start']:%Y-%m-%d} → {ctx['end'] - pd.Timedelta(days=1):%Y-%m-%d}")
        force = c2.checkbox("Regenerate if unchanged", value=False)
        if c3.button("Generate report", type="primary"):
            with st.spinner("Generating…"):
                r = generate_report(db, ctx["account"], ctx["start"], ctx["end"],
                                    ctx["period_type"], ctx["cfg"], force=force)
            if r["status"] == "DATA MISSING":
                st.error("DATA MISSING report generated — upload the MT5 report for this "
                         "period.")
            elif r["duplicate"]:
                st.info("Inputs unchanged since the last report; existing report kept.")
            else:
                st.success(f"Report generated: {r['status']}")
            st.cache_data.clear()

    reps = db.list_reports(ctx.get("account"))
    if reps.empty:
        st.info("No reports stored yet.")
        return
    weekly = reps[reps["period_type"] == "WEEKLY"].sort_values("period_start")
    if len(weekly[weekly["is_data_missing"] == 0]) >= 2:
        hist = []
        for _, r in weekly[weekly["is_data_missing"] == 0].iterrows():
            m = db.get_report(int(r["id"]))["metrics"]
            hist.append({"week": r["period_start"][:10], "net": _val(m, "net_profit"),
                         "pf": _val(m, "profit_factor"), "wr": _val(m, "win_rate"),
                         "dd": _val(m, "max_balance_drawdown_pct"), "status": r["status"]})
        h = pd.DataFrame(hist)
        c1, c2 = st.columns(2)
        with c1:
            fig = go.Figure(go.Bar(x=h["week"], y=h["net"],
                                   marker_color=[COLORS["pos"] if (v or 0) >= 0 else
                                                 COLORS["neg"] for v in h["net"]]))
            fig.update_layout(title="Weekly net profit")
            fig.update_xaxes(type="category")
            st.plotly_chart(line(fig, 280), use_container_width=True)
        with c2:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=h["week"], y=h["pf"], name="Profit factor"))
            fig.add_trace(go.Scatter(x=h["week"], y=h["dd"], name="Max balance DD %",
                                     yaxis="y2"))
            fig.update_layout(title="Weekly PF & drawdown",
                              yaxis2=dict(overlaying="y", side="right"))
            fig.update_xaxes(type="category")
            st.plotly_chart(line(fig, 280), use_container_width=True)

    st.subheader("Report history")
    st.dataframe(reps[["id", "account_id", "period_type", "period_start", "period_end", "status",
                       "generated_at"]], hide_index=True, use_container_width=True)
    rid = st.selectbox("Open report", reps["id"].tolist(),
                       format_func=lambda i: " · ".join(str(x) for x in reps.set_index("id").loc[
                           i, ["period_type", "period_start", "status"]]))
    rep = db.get_report(int(rid))
    st.markdown(status_badge(rep["status"]), unsafe_allow_html=True)
    c = st.columns(3)
    for col, key, label, mime in ((c[0], "pdf_path", "PDF", "application/pdf"),
                                  (c[1], "csv_path", "Metrics CSV", "text/csv"),
                                  (c[2], "md_path", "Markdown", "text/markdown")):
        p = rep.get(key)
        if p and Path(p).exists():
            col.download_button(f"Download {label}", Path(p).read_bytes(), Path(p).name, mime)
        else:
            col.caption(f"{label}: not available")
    if rep.get("md_path") and Path(rep["md_path"]).exists():
        with st.expander("Report content", expanded=True):
            st.markdown(Path(rep["md_path"]).read_text(encoding="utf-8"))
