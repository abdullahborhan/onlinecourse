"""Shared dashboard helpers: context, cached analysis, styling, formatting."""
from __future__ import annotations

import hashlib
import json

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

import config
from modules.analysis_service import month_bounds, run_analysis, week_bounds
from modules.common import (STATUS_ATTENTION, STATUS_DATA_MISSING, STATUS_STABLE, STATUS_STOP,
                            format_metric)
from modules.database import Database

pio.templates.default = "plotly_dark"
COLORS = {"pos": "#22c55e", "neg": "#ef4444", "accent": "#22d3ee", "muted": "#94a3b8",
          "warn": "#f59e0b", "BREAKOUT": "#22d3ee", "MOMENTUM": "#a78bfa",
          "PULLBACK": "#f59e0b", "UNCLASSIFIED": "#64748b"}
STATUS_COLORS = {STATUS_STABLE: "#16a34a", STATUS_ATTENTION: "#d97706", STATUS_STOP: "#dc2626",
                 STATUS_DATA_MISSING: "#475569"}
VERDICT_COLORS = {"BREACH": "#dc2626", "COMPLIANT": "#16a34a", "UNVERIFIABLE": "#d97706",
                  "NOT_APPLICABLE": "#475569"}

CSS = """
<style>
.block-container {padding-top: 3.2rem; max-width: 1500px;}
.kpi {background:#111a2e;border:1px solid #1e293b;border-radius:10px;padding:14px 16px;}
.kpi .label {color:#94a3b8;font-size:0.78rem;text-transform:uppercase;letter-spacing:.04em;}
.kpi .value {color:#e5e7eb;font-size:1.45rem;font-weight:600;margin-top:2px;}
.kpi .sub {color:#64748b;font-size:0.72rem;margin-top:2px;}
.badge {display:inline-block;padding:6px 14px;border-radius:8px;font-weight:700;color:white;}
.synthetic {background:#7f1d1d;color:#fecaca;padding:8px 12px;border-radius:8px;
            font-weight:700;margin-bottom:10px;}
.tag {display:inline-block;padding:1px 8px;border-radius:6px;font-size:0.75rem;color:white;}
</style>
"""


def get_db() -> Database:
    if "db" not in st.session_state:
        st.session_state.db = Database()
    return st.session_state.db


def data_version(db: Database) -> str:
    with db.connect() as c:
        row = c.execute("SELECT COUNT(*), MAX(id) FROM source_files").fetchone()
        row2 = c.execute("SELECT COUNT(*), MAX(id) FROM reports").fetchone()
        row3 = c.execute("SELECT COUNT(*), MAX(id) FROM ea_configs").fetchone()
    return f"{tuple(row)}-{tuple(row2)}-{tuple(row3)}"


def cfg_hash(cfg) -> str:
    return hashlib.md5(json.dumps(cfg.to_dict(), sort_keys=True, default=str).encode()
                       ).hexdigest()


@st.cache_data(show_spinner="Running deterministic analysis…", max_entries=24)
def cached_analysis(db_path: str, account: str, start: str, end: str, period_type: str,
                    _version: str, _cfg_hash: str):
    db = Database(db_path)
    return run_analysis(db, account, start, end, period_type, config.load_config())


def analysis_for(ctx) -> dict:
    db, cfg = ctx["db"], ctx["cfg"]
    return cached_analysis(str(db.path), ctx["account"], str(ctx["start"]), str(ctx["end"]),
                           ctx["period_type"], data_version(db), cfg_hash(cfg))


def sidebar_context() -> dict | None:
    db = get_db()
    cfg = config.load_config()
    st.sidebar.markdown(f"### 🦈 {config.APP_SHORT}")
    st.sidebar.caption(f"{config.APP_NAME} v{config.APP_VERSION}")
    accounts = db.accounts()
    ctx = {"db": db, "cfg": cfg, "account": None, "start": None, "end": None,
           "period_type": "WEEKLY"}
    if not accounts:
        st.sidebar.info("No trading data yet. Upload an MT5 report on **Data Management**.")
        return ctx
    acct = st.sidebar.selectbox("Account", accounts, key="account")
    cov = db.data_coverage(acct)
    first = cov["deals"]["first"] or cov["report_positions"]["first"]
    last = cov["deals"]["last"] or cov["report_positions"]["last"]
    ptype = st.sidebar.radio("Period", ["WEEKLY", "MONTHLY", "ALL DATA", "CUSTOM"],
                             horizontal=True, key="ptype")
    last_ts = pd.Timestamp(last) if last else pd.Timestamp.now()
    first_ts = pd.Timestamp(first) if first else last_ts
    if ptype == "WEEKLY":
        weeks = []
        s, _ = week_bounds(last_ts)
        while s >= week_bounds(first_ts)[0] and len(weeks) < 104:
            weeks.append(s)
            s -= pd.Timedelta(days=7)
        sel = st.sidebar.selectbox("Week (Mon–Sun, server time)", weeks,
                                   format_func=lambda d: f"{d:%Y-%m-%d} → "
                                   f"{d + pd.Timedelta(days=6):%Y-%m-%d}")
        start, end = sel, sel + pd.Timedelta(days=7)
    elif ptype == "MONTHLY":
        months, m = [], month_bounds(last_ts)[0]
        while m >= month_bounds(first_ts)[0] and len(months) < 36:
            months.append(m)
            m = month_bounds(m - pd.Timedelta(days=1))[0]
        sel = st.sidebar.selectbox("Month", months, format_func=lambda d: f"{d:%B %Y}")
        start, end = month_bounds(sel)
    elif ptype == "ALL DATA":
        start, end = first_ts.normalize(), last_ts.normalize() + pd.Timedelta(days=1)
    else:
        d = st.sidebar.date_input("Date range (server time)",
                                  (first_ts.date(), last_ts.date()))
        if isinstance(d, tuple) and len(d) == 2:
            start, end = pd.Timestamp(d[0]), pd.Timestamp(d[1]) + pd.Timedelta(days=1)
        else:
            return None
    st.sidebar.caption(f"Data held: {first} → {last}")
    st.sidebar.caption(f"Server timezone assumption: {cfg.server_timezone}")
    ctx.update({"account": acct, "start": start, "end": end,
                "period_type": ptype if ptype in ("WEEKLY", "MONTHLY") else "CUSTOM"})
    return ctx


def synthetic_banner():
    if config.IS_SYNTHETIC_ENV:
        st.markdown(f"<div class='synthetic'>⚠ SYNTHETIC {config.ENVIRONMENT.upper()} "
                    "ENVIRONMENT — demonstration data, NOT real trading results.</div>",
                    unsafe_allow_html=True)


def kpi(col, label, value, sub=""):
    col.markdown(f"<div class='kpi'><div class='label'>{label}</div>"
                 f"<div class='value'>{value}</div><div class='sub'>{sub}</div></div>",
                 unsafe_allow_html=True)


def status_badge(status: str) -> str:
    return (f"<span class='badge' style='background:{STATUS_COLORS.get(status, '#475569')}'>"
            f"{status}</span>")


def verdict_tag(v: str) -> str:
    return (f"<span class='tag' style='background:{VERDICT_COLORS.get(v, '#475569')}'>{v}"
            "</span>")


def fm(m) -> str:
    return format_metric(m)


def sub_of(m) -> str:
    if m is None:
        return ""
    md = m.to_dict() if hasattr(m, "to_dict") else m
    if md.get("status") == "UNAVAILABLE":
        return md.get("note", "")[:90]
    return md.get("status", "")


def metrics_df(d: dict) -> pd.DataFrame:
    rows = []
    for k, m in d.items():
        md = m.to_dict() if hasattr(m, "to_dict") else m
        if not isinstance(md, dict) or "status" not in md:
            continue
        rows.append({"Metric": k.replace("_", " ").capitalize(), "Value": format_metric(md),
                     "Status": md["status"],
                     "Basis / note": " | ".join(x for x in (md.get("basis"), md.get("note"))
                                                if x)})
    return pd.DataFrame(rows)


def need_data(ctx) -> bool:
    if not ctx or not ctx.get("account"):
        st.warning("No trading data available. Please upload the latest MT5 report on the "
                   "**Data Management** page. SVPI has no connection to MT5 and cannot fetch "
                   "reports itself.")
        return True
    return False


def data_missing_box(res):
    st.error("**DATA MISSING** — no trading records for this period. No metrics are shown. "
             "Upload the MT5 report covering this period on the Data Management page.")
    st.json(res.get("coverage", {}))


def line(fig: go.Figure, height=340):
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=40, b=10),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      legend=dict(orientation="h", y=1.08))
    return fig
