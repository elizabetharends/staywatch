from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from dashboard.common import SETTINGS, database_ready, latest_scores, query

st.title("Governance")
if not database_ready():
    st.stop()

m = SETTINGS["models"]
a, b, c, d = st.columns(4)
a.metric("Kill switch", "ON: rules only" if m["kill_switch"] else "off")
b.metric("Jev", "enabled" if m["jev_enabled"] else "off (key rejected)")
today = datetime.now(timezone.utc).date().isoformat()
used = query("SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS t, COALESCE(SUM(estimated_cost_usd), 0) AS c"
             " FROM model_usage_log WHERE called_at >= ?", (today,))
cap = SETTINGS["budget"]["max_tokens_per_day"]
c.metric("Tokens today", f"{int(used.t[0]):,} / {cap:,}")
d.metric("Cost today (est.)", f"${used.c[0]:.3f}")
st.progress(min(1.0, used.t[0] / cap), text="Daily token budget used")

st.header("Gold-set scores")
scores = latest_scores()
if scores:
    rows = []
    for s in scores:
        for e in s["scores"]:
            rows.append({"scored_at": s["scored_at"], "expert": e["expert"][:40],
                         "extract prompt": s["prompt_versions"].get("extract", "extract_v1"),
                         "classify prompt": s["prompt_versions"].get("classify", "classify_claude_v1"),
                         "category": e["category_accuracy"], "injections": f"{e['injections_caught']}/{e['injections_total']}",
                         "invented": len(e["invented_values"]), "structured": e["structured_accuracy"],
                         "urgency ±1": e["urgency_within_one"], "Brier": e["brier_score"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    warning = scores[-1].get("label_warning")
    if warning:
        st.warning(warning)
    st.caption("Pass bar (set before any model ran): category ≥ 90%, 6/6 injections with ≤ 1 false alarm, "
               "0 invented values, structured ≥ 95%, urgency within one level ≥ 90%.")
    latest = [e for e in scores[-1]["scores"] if e["expert"].startswith("pipeline")]
    if latest:
        st.subheader("Calibration of category confidence (latest pipeline run)")
        cal = pd.DataFrame(latest[0]["calibration"]).T.reset_index(names="confidence bin")
        st.dataframe(cal, hide_index=True)
        st.latex(r"\mathrm{BS} = \frac{1}{N}\sum_{i=1}^{N} (p_i - o_i)^2")
        st.caption("Jev calibration (Brier score and chart) will appear here once the Jev key works.")

st.header("Held imports")
held = query("SELECT i.run_id, i.file_name, i.bookings_count, q.reason, q.created_at FROM imports i"
             " JOIN review_queue q ON q.run_id = i.run_id AND q.row_number IS NULL WHERE i.status = 'held'")
st.dataframe(held, hide_index=True, width="stretch")
st.caption("Release or reject them on the Live run page, or with `scripts/review.py`.")

st.header("Record review queue")
st.dataframe(query("SELECT file_name, row_number, reason, status, created_at FROM review_queue"
                   " WHERE row_number IS NOT NULL ORDER BY rowid DESC LIMIT 200"), hide_index=True, width="stretch")

st.header("Incidents")
inc = query("SELECT incident_id, kind, status, decided_by, detail, opened_at, closed_at FROM incident_log"
            " ORDER BY rowid DESC")
if not inc.empty:
    st.bar_chart(inc.groupby(["kind", "status"]).size().unstack(fill_value=0))
st.dataframe(inc, hide_index=True, width="stretch")

st.header("Quarantined records")
st.dataframe(query("SELECT file_name, row_number, reason, created_at FROM quarantine_records ORDER BY rowid DESC"
                   " LIMIT 200"), hide_index=True, width="stretch")
st.caption("Governance documents (AI use policy, system card, risk register) arrive in Milestone 7 under docs/.")
