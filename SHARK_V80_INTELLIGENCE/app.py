"""
SHARK V80 PERFORMANCE INTELLIGENCE - Streamlit dashboard.

    streamlit run app.py

Independent, local-first, read-only audit of SHARK V80 ENGINE trading records.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st  # noqa: E402

import config  # noqa: E402

st.set_page_config(page_title="SHARK V80 Performance Intelligence", page_icon="🦈",
                   layout="wide")

from dashboard import (ai_analyst_page, data_management, overview, performance, risk,  # noqa
                       strategies, trade_history, weekly_reports)
from dashboard.common import CSS, sidebar_context, synthetic_banner  # noqa: E402

PAGES = {
    "1 · Overview": overview.render,
    "2 · Performance": performance.render,
    "3 · Strategy analysis": strategies.render,
    "4 · Risk monitor": risk.render,
    "5 · Trade history": trade_history.render,
    "6 · Weekly reports": weekly_reports.render,
    "7 · Data management": data_management.render,
    "8 · AI analyst": ai_analyst_page.render,
}


def main():
    config.ensure_directories()
    st.markdown(CSS, unsafe_allow_html=True)
    synthetic_banner()
    page = st.sidebar.radio("Navigate", list(PAGES), key="page")
    ctx = sidebar_context()
    st.sidebar.markdown("---")
    st.sidebar.caption("Read-only auditor · no broker connection · never trades · "
                       "never modifies the EA")
    if ctx is None:
        st.warning("Select a complete date range.")
        return
    PAGES[page](ctx)


main()
