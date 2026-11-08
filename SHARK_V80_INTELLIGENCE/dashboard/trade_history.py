"""PAGE 5 - Trade history (reconstructed positions)."""
import streamlit as st

from dashboard.common import analysis_for, data_missing_box, need_data

COLS = {"position_id": "Ticket", "open_time": "Open time", "close_time": "Close time",
        "direction": "Direction", "volume": "Volume", "entry_price": "Entry",
        "exit_price": "Exit", "sl": "SL", "tp": "TP", "profit": "Profit",
        "commission": "Commission", "swap": "Swap", "fee": "Fee", "net_profit": "Net",
        "strategy": "Strategy", "strategy_source": "Strategy source", "basket_id": "Basket",
        "layer": "Layer", "status": "Status", "n_exits": "Exit deals",
        "exit_reason": "Exit reason", "risk_pct": "Risk % (SL)", "symbol": "Symbol",
        "magic": "Magic", "comment": "Comment", "source": "Source"}


def render(ctx):
    st.title("Trade history")
    if need_data(ctx):
        return
    res = analysis_for(ctx)
    if res.get("data_missing"):
        data_missing_box(res)
        return
    t = res["trades_period"]
    if t.empty:
        st.info("No positions in this period.")
        return
    st.caption("One row per reconstructed POSITION (entry and exit deals combined). Empty "
               "cells mean the source did not provide the value — they are not zero.")
    c1, c2, c3 = st.columns(3)
    strat = c1.multiselect("Strategy", sorted(t["strategy"].unique()))
    dirs = c2.multiselect("Direction", sorted(t["direction"].unique()))
    stat = c3.multiselect("Status", sorted(t["status"].unique()))
    v = t.copy()
    if strat:
        v = v[v["strategy"].isin(strat)]
    if dirs:
        v = v[v["direction"].isin(dirs)]
    if stat:
        v = v[v["status"].isin(stat)]
    view = v[[c for c in COLS if c in v.columns]].rename(columns=COLS)
    st.dataframe(view, hide_index=True, use_container_width=True, height=520)
    st.download_button("Download CSV", view.to_csv(index=False).encode(),
                       f"svpi_trades_{ctx['account']}_{ctx['start']:%Y%m%d}.csv", "text/csv")
