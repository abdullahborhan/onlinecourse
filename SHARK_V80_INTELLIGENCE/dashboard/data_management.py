"""PAGE 7 - Data management: uploads, validation, missing information, configuration."""
import json

import pandas as pd
import streamlit as st

import config
from modules.audit_logger import audit
from modules.database import RESET_CONFIRMATION
from modules.mt5_importer import MT5Importer
from modules.set_parser import compare_to_baseline

UPLOADS = [
    ("MT5 report (Detailed Account History / statement HTML)", ["htm", "html"]),
    ("MT5 CSV (deal history, positions, or equity/balance history)", ["csv", "tsv"]),
    ("EA logs (Experts / Journal / transaction logs)", ["log", "txt", "csv"]),
    ("EA .set configuration", ["set"]),
]


def _show_result(r):
    color = {"IMPORTED": st.success, "IMPORTED_WITH_WARNINGS": st.warning,
             "DUPLICATE_FILE": st.info}.get(r.status, st.error)
    color(f"**{r.filename}** → {r.status} ({r.kind}); account: {r.account_id}; "
          f"records: {r.records}")
    for e in r.errors:
        st.error(e)
    for w in r.warnings:
        st.caption(f"⚠ {w}")
    with st.expander("Validation report"):
        st.json(r.to_dict()["validation"])


def render(ctx):
    st.title("Data management")
    db, cfg = ctx["db"], ctx["cfg"]
    st.info("SVPI is local-first and read-only: it never asks for or stores broker passwords "
            "or MT5 credentials, has no broker connection, and never uploads your statements "
            "anywhere. Export reports from MT5 and upload them here, or drop them in "
            f"`{config.INCOMING_DIR}` for the weekly job.")

    tabs = st.tabs(["Upload", "Missing information", "Imported files", "EA configuration",
                    "Settings", "Maintenance & audit"])
    with tabs[0]:
        accounts = db.accounts()
        acct = st.text_input("Account number for files that do not contain one (CSV, logs)",
                             value=ctx.get("account") or (accounts[0] if accounts else ""))
        for label, types in UPLOADS:
            files = st.file_uploader(label, type=types, accept_multiple_files=True,
                                     key=f"up_{label}")
            if files and st.button(f"Import {len(files)} file(s)", key=f"btn_{label}"):
                imp = MT5Importer(db, cfg)
                for f in files:
                    _show_result(imp.import_bytes(f.getvalue(), f.name, account_id=acct or None))
                st.cache_data.clear()

    with tabs[1]:
        if not ctx.get("account"):
            st.error("No trading data imported. Required: MT5 Detailed Account History "
                     "(HTML) or deal-history CSV. SVPI cannot obtain it automatically.")
        else:
            from dashboard.common import analysis_for
            res = analysis_for(ctx)
            if res.get("data_missing"):
                st.error("No trading records for the selected period — upload the MT5 report "
                         "covering it.")
            else:
                comp = res["completeness"]
                st.metric("Data completeness", f"{comp['score_pct']:.0f}%")
                st.dataframe(pd.DataFrame(comp["items"]), hide_index=True,
                             use_container_width=True)
                for m in comp["missing"]:
                    st.warning(f"Missing: **{m['source']}** — affects {m['affects_if_missing']}")
                st.caption("Missing data limits verification; it is not evidence of poor "
                           "performance.")
            st.json(db.data_coverage(ctx["account"]))

    with tabs[2]:
        sf = db.source_files()
        if sf.empty:
            st.info("No files imported yet.")
        else:
            st.dataframe(sf.drop(columns=["validation_json"]), hide_index=True,
                         use_container_width=True)
            sid = st.selectbox("Show validation report for file id", sf["id"].tolist())
            st.json(json.loads(sf.set_index("id").loc[sid, "validation_json"] or "{}"))
            st.markdown("**Delete an imported file and its records** (a backup is taken first)")
            conf = st.text_input(f"Type exactly: DELETE SOURCE {sid}", key="del_conf")
            if st.button("Delete source file", type="secondary"):
                try:
                    db.delete_source_file(int(sid), conf)
                    st.success("Deleted.")
                    st.cache_data.clear()
                except PermissionError as e:
                    st.error(str(e))

    with tabs[3]:
        ea = db.active_ea_config()
        if not ea:
            st.warning("No EA .set file uploaded. Compliance is checked against the APPROVED "
                       "baseline only; the EA's actual configuration is unknown.")
        else:
            st.markdown(f"Active EA configuration: **{ea['origin']}** (loaded {ea['loaded_at']})")
            drift = compare_to_baseline(ea["mapped"], cfg.risk)
            st.dataframe(pd.DataFrame(drift), hide_index=True, use_container_width=True)
            with st.expander("Raw .set parameters"):
                st.json(ea["raw"])
        st.markdown("#### Provide / confirm the actual EA configuration")
        st.caption("If parameter names were not recognised, enter the EA's actual values here. "
                   "These are recorded as USER_PROVIDED and compared with the approved baseline.")
        cur = (ea or {}).get("mapped", {})
        g = lambda k, d: cur.get(k, {}).get("value", d)  # noqa: E731
        with st.form("ea_cfg"):
            c = st.columns(4)
            vals = {
                "max_layers": c[0].number_input("Max layers", 1, 20, int(g("max_layers", 3))),
                "layer1_risk_pct": c[1].number_input("Layer 1 risk %", 0.0, 10.0,
                                                     float(g("layer1_risk_pct", 0.5)), 0.05),
                "layer2_risk_pct": c[2].number_input("Layer 2 risk %", 0.0, 10.0,
                                                     float(g("layer2_risk_pct", 0.25)), 0.05),
                "layer3_risk_pct": c[3].number_input("Layer 3 risk %", 0.0, 10.0,
                                                     float(g("layer3_risk_pct", 0.25)), 0.05),
                "max_basket_risk_pct": c[0].number_input("Max basket risk %", 0.0, 20.0,
                                                         float(g("max_basket_risk_pct", 1.0)),
                                                         0.05),
                "daily_loss_lock_pct": c[1].number_input("Daily loss lock %", 0.0, 50.0,
                                                         float(g("daily_loss_lock_pct", 2.0)),
                                                         0.1),
                "daily_hard_stop_pct": c[2].number_input("Daily hard stop %", 0.0, 50.0,
                                                         float(g("daily_hard_stop_pct", 3.0)),
                                                         0.1),
                "weekly_loss_lock_pct": c[3].number_input("Weekly loss lock %", 0.0, 50.0,
                                                          float(g("weekly_loss_lock_pct", 5.0)),
                                                          0.1),
                "max_equity_drawdown_pct": c[0].number_input(
                    "Max equity DD %", 0.0, 100.0, float(g("max_equity_drawdown_pct", 10.0)), 0.5),
                "daily_profit_target_pct": c[1].number_input(
                    "Daily profit target %", 0.0, 50.0, float(g("daily_profit_target_pct", 1.5)),
                    0.1),
                "martingale_enabled": c[2].checkbox("Martingale enabled",
                                                    bool(g("martingale_enabled", False))),
            }
            if st.form_submit_button("Save as actual EA configuration"):
                mapped = {k: {"value": v, "source_key": "USER_PROVIDED", "ambiguous_keys": []}
                          for k, v in vals.items()}
                db.save_ea_config((ea or {}).get("raw", {}), mapped, origin="USER_PROVIDED")
                audit("EA_CONFIG_USER_PROVIDED", vals, actor="user", db=db)
                st.success("Saved.")
                st.cache_data.clear()

    with tabs[4]:
        st.markdown("#### Approved baseline & analysis settings")
        st.caption("Changing the APPROVED baseline changes what counts as a breach. Changes are "
                   "audited and stored in svpi_settings.json.")
        with st.form("settings"):
            r = cfg.risk
            c = st.columns(3)
            r.daily_loss_lock_pct = c[0].number_input("Approved daily loss lock %", 0.1, 50.0,
                                                      r.daily_loss_lock_pct, 0.1)
            r.daily_hard_stop_pct = c[1].number_input("Approved daily hard stop %", 0.1, 50.0,
                                                      r.daily_hard_stop_pct, 0.1)
            r.weekly_loss_lock_pct = c[2].number_input("Approved weekly loss lock %", 0.1, 50.0,
                                                       r.weekly_loss_lock_pct, 0.1)
            r.max_equity_drawdown_pct = c[0].number_input("Approved max equity DD %", 0.5, 100.0,
                                                          r.max_equity_drawdown_pct, 0.5)
            r.max_basket_risk_pct = c[1].number_input("Approved max basket risk %", 0.05, 20.0,
                                                      r.max_basket_risk_pct, 0.05)
            r.max_layers = int(c[2].number_input("Approved max layers", 1, 20, r.max_layers))
            r.profit_target_is_lock = c[0].checkbox("Profit target stops new entries",
                                                    r.profit_target_is_lock)
            cfg.server_timezone = c[1].text_input("Broker server timezone (IANA)",
                                                  cfg.server_timezone)
            cfg.analysis.breakeven_epsilon = c[2].number_input(
                "Breakeven epsilon (account ccy)", 0.0, 100.0, cfg.analysis.breakeven_epsilon,
                0.01)
            ms = st.text_area("Magic number → strategy map (JSON, e.g. {\"80001\": \"BREAKOUT\"})",
                              json.dumps(cfg.analysis.magic_strategy_map))
            st.markdown("**External AI (privacy)**")
            cfg.ai.external_ai_enabled = st.checkbox(
                "I explicitly allow sending AGGREGATED metrics (never raw statements) to the "
                "external AI provider", cfg.ai.external_ai_enabled)
            confirm = st.checkbox("I confirm these changes to the approved baseline")
            if st.form_submit_button("Save settings"):
                if not confirm:
                    st.error("Tick the confirmation box to save.")
                else:
                    try:
                        cfg.analysis.magic_strategy_map = json.loads(ms or "{}")
                    except json.JSONDecodeError:
                        st.error("Magic map is not valid JSON; not saved.")
                        st.stop()
                    r.source = "USER_MODIFIED_BASELINE"
                    config.save_config(cfg)
                    audit("SETTINGS_CHANGED", cfg.to_dict(), actor="user", db=db)
                    st.success("Settings saved.")
                    st.cache_data.clear()

    with tabs[5]:
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Back up database now"):
                st.success(f"Backup written: {db.backup()}")
        with c2:
            st.markdown("**Reset all data** (backup is taken first)")
            txt = st.text_input(f"Type exactly: {RESET_CONFIRMATION}")
            if st.button("Delete ALL imported data and reports"):
                try:
                    p = db.reset_all_data(txt)
                    st.success(f"All data deleted. Backup: {p}")
                    st.cache_data.clear()
                except PermissionError as e:
                    st.error(str(e))
        st.markdown("#### Audit log")
        st.dataframe(db.audit_events(300), hide_index=True, use_container_width=True)
