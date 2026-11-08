"""PAGE 8 - AI quantitative analyst (interpretation only)."""
import streamlit as st

from dashboard.common import analysis_for, data_missing_box, need_data
from modules.ai_analyst import build_fact_sheet, run_ai_analysis


def render(ctx):
    st.title("AI quantitative analyst")
    st.caption("Interprets the deterministic metrics. It never calculates or overrides numbers, "
               "never accesses MT5, never changes the EA and never recommends larger lots.")
    if need_data(ctx):
        return
    res = analysis_for(ctx)
    if res.get("data_missing"):
        data_missing_box(res)
        return
    cfg = ctx["cfg"]
    sheet = build_fact_sheet(res)
    with st.expander("Fact sheet that would be analysed (aggregated metrics only — no trades, "
                     "no account number)"):
        st.json(sheet)
    consent = False
    if cfg.ai.external_ai_enabled:
        consent = st.checkbox(f"Send this aggregated fact sheet to {cfg.ai.model} "
                              "for this one analysis")
    else:
        st.info("External AI is disabled (Data Management → Settings). The offline "
                "rule-based analyst is used; no data leaves this computer.")
    if st.button("Run analysis", type="primary"):
        out = run_ai_analysis(res, cfg, consent=consent)
        st.markdown(f"**Source:** {out['source']}")
        st.caption(out["note"])
        for p in out["paras"]:
            tag = p.split(":")[0]
            fn = {"FACT": st.markdown, "HYPOTHESIS": st.info, "MISSING DATA": st.warning,
                  "TEST": st.success}.get(tag, st.markdown)
            fn(p)
        if out.get("validation") and not out["validation"]["ok"]:
            with st.expander("Rejected AI output"):
                st.write(out["validation"])
                st.text(out.get("rejected_ai_text", ""))
