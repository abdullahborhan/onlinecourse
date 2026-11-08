"""PAGE 1 - Overview."""
import pandas as pd
import streamlit as st

from dashboard.common import (analysis_for, data_missing_box, fm, kpi, need_data,
                              status_badge, sub_of, verdict_tag)


def render(ctx):
    st.title("Overview")
    if need_data(ctx):
        return
    res = analysis_for(ctx)
    st.caption(f"Account {ctx['account']} · {ctx['start']:%Y-%m-%d} → "
               f"{ctx['end'] - pd.Timedelta(days=1):%Y-%m-%d} (server time)")
    if res.get("data_missing"):
        data_missing_box(res)
        return
    st.markdown(status_badge(res["status"]["status"]), unsafe_allow_html=True)
    P = res["performance"]["profitability"]
    S = res["performance"]["statistics"]
    dm = res["drawdown"]["metrics"]
    deals = res["trades_all"]
    db = ctx["db"]
    d = db.load_deals(ctx["account"], end=ctx["end"])
    bal = d["balance"].dropna()
    eq = db.load_equity(ctx["account"], end=ctx["end"])

    c = st.columns(4)
    kpi(c[0], "Account balance", f"{bal.iloc[-1]:,.2f}" if len(bal) else "UNAVAILABLE",
        f"as of {d['time'].iloc[-1]:%Y-%m-%d %H:%M}" if len(bal) else
        "no balance column in source")
    kpi(c[1], "Account equity", f"{eq['equity'].iloc[-1]:,.2f}" if not eq.empty else
        "UNAVAILABLE", f"as of {eq['time'].iloc[-1]:%Y-%m-%d %H:%M}" if not eq.empty else
        "no equity history uploaded")
    kpi(c[2], "Net profit (period)", fm(P["net_profit"]), sub_of(P["net_profit"]))
    kpi(c[3], "Profit factor", fm(P["profit_factor"]), sub_of(P["profit_factor"]))
    c = st.columns(4)
    kpi(c[0], "Win rate", fm(S["win_rate"]), "breakeven in denominator")
    kpi(c[1], "Closed trades", fm(S["total_closed_trades"]), "positions, not deals")
    kpi(c[2], "Max balance DD", fm(dm["max_balance_drawdown_pct"]),
        f"{fm(dm['max_balance_drawdown'])} closed-trade")
    kpi(c[3], "Max equity DD", fm(dm["max_equity_drawdown_pct"]),
        sub_of(dm["max_equity_drawdown_pct"]))

    st.subheader("Current risk status")
    left, right = st.columns([3, 2])
    with left:
        stt = res["status"]
        for r in stt["stop_reasons"]:
            st.error(r)
        for r in stt["attention_reasons"]:
            st.warning(r)
        for r in stt.get("info", []):
            st.success(r)
    with right:
        rows = "".join(f"<tr><td>{c['check']}</td><td>{verdict_tag(c['verdict'])}</td></tr>"
                       for c in res["risk"]["checks"])
        st.markdown(f"<table style='width:100%'>{rows}</table>", unsafe_allow_html=True)

    st.subheader("Three major findings")
    for i, f in enumerate(res["major_findings"], 1):
        st.markdown(f"**{i}.** {f}")
    with st.expander("Data completeness"):
        st.dataframe(pd.DataFrame(res["completeness"]["items"]), hide_index=True,
                     use_container_width=True)
    _ = deals
