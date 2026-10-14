import json

import pandas as pd
import streamlit as st

from agent.decisions import pending_requests, request_decision
from dashboard.common import (SETTINGS, STEP_INFO, database_ready, query, show_sql, show_stat)
from dashboard.flow import build_dot

st.title("StayWatch: live run")
st.caption("A governed data agent for short-term rental bookings. Simulated data only.")

if not database_ready():
    st.stop()


@st.fragment(run_every=5)
def held_banner():
    held = query("SELECT i.run_id, i.file_name, i.bookings_count, q.result_json FROM imports i"
                 " JOIN review_queue q ON q.run_id = i.run_id AND q.row_number IS NULL"
                 " WHERE i.status = 'held' ORDER BY i.rowid DESC")
    if held.empty:
        return
    pending = {p["run_id"]: p for p in pending_requests(SETTINGS)}
    st.error(f"⚠️ **{len(held)} import(s) held by the anomaly trigger.** A person must release or reject each one.")
    with st.container(border=True):
        options = {f"{h.file_name} · {h.bookings_count} bookings · {h.run_id}": h for h in held.itertuples()}
        pick = st.selectbox("Held import", list(options), key="held_pick")
        h = options[pick]
        detail = json.loads(h.result_json)
        st.markdown(f"**{h.file_name}**: {h.bookings_count} bookings, **z = {detail['result']:+.2f}** "
                    f"(threshold ±{detail['threshold']:g})")
        st.write(detail["explanation"])
        st.caption(f"Explanation by {detail['explained_by']} · run {h.run_id}")
        if h.run_id in pending:
            p = pending[h.run_id]
            st.info(f"{p['decision'].capitalize()} requested by {p['person']} at {p['requested_at']}; "
                    "the agent applies it on its next scan (within 15 s).")
            return
        name_col, a, b = st.columns([3, 1, 1], vertical_alignment="bottom")
        name = name_col.text_input("Your name (recorded with the decision)", key="reviewer")
        for col, label, decision in ((a, "Release", "released"), (b, "Reject", "rejected")):
            if col.button(label, key=f"{decision}_{h.run_id}", disabled=not name.strip(), width="stretch",
                          type="primary" if decision == "released" else "secondary"):
                request_decision(SETTINGS, h.run_id, decision, name)
                st.rerun(scope="fragment")


held_banner()

runs = query("SELECT run_id, file_name, MIN(started_at) AS started FROM run_log WHERE step = 'detect'"
             " GROUP BY run_id ORDER BY MIN(rowid) DESC LIMIT 30")
if runs.empty:
    st.info("Waiting for the first import.")
    st.stop()
choice = st.selectbox("Run", ["Latest"] + [f"{r.file_name} · {r.run_id}" for r in runs.itertuples()],
                      help="Latest follows new imports automatically.")


@st.fragment(run_every=5)
def live(choice: str):
    latest = query("SELECT run_id, file_name FROM run_log WHERE step = 'detect' ORDER BY rowid DESC LIMIT 1")
    run_id = latest.run_id[0] if choice == "Latest" else choice.split(" · ")[1]
    steps_df = query("SELECT rowid, * FROM run_log WHERE run_id = ? ORDER BY rowid", (run_id,))
    file_name = steps_df.file_name.dropna().iloc[0] if not steps_df.empty else ""
    steps = {r["step"]: r for r in steps_df.to_dict("records")}   # last row per step wins
    usage = query("SELECT * FROM model_usage_log WHERE run_id = ? ORDER BY rowid", (run_id,))
    decision = query("SELECT * FROM decision_log WHERE run_id = ?", (run_id,))
    imp = query("SELECT * FROM imports WHERE run_id = ?", (run_id,))
    sql = query("SELECT * FROM sql_log WHERE run_id = ? ORDER BY rowid", (run_id,))

    last = pd.to_datetime(steps_df.finished_at.dropna().max() if not steps_df.empty else None, utc=True)
    age = (pd.Timestamp.now(tz="UTC") - last).total_seconds() if pd.notna(last) else None
    st.markdown(f"#### `{file_name or '-'}`")
    c2, c3, c4 = st.columns(3)
    c2.metric("Bookings", int(imp.bookings_count[0]) if not imp.empty else "-")
    c3.metric("Status", imp.status[0].upper() if not imp.empty else steps_df.status.iloc[-1].upper())
    c4.metric("Last step finished", f"{age:.0f} s ago" if age is not None else "running")

    st.graphviz_chart(build_dot(steps, usage.to_dict("records"),
                                decision.to_dict("records")[0] if not decision.empty else None,
                                imp.to_dict("records")[0] if not imp.empty else None, file_name),
                      width="stretch")
    st.caption("Grey waiting · blue running · green done · amber held or warning · red failed. "
               "Dashed arrow: branch not taken. Numbers on arrows: rows passed on.")

    st.subheader("Step by step")
    for step in ["detect", "land", "redact", "route", "classify", "extract", "validate", "load", "decide", "act"]:
        row = steps.get(step)
        state = row["status"] if row else ("ok" if step == "route" and not usage.empty else "waiting")
        icon = {"ok": "🟢", "warning": "🟠", "held": "🟠", "failed": "🔴", "running": "🔵"}.get(state, "⚪")
        with st.expander(f"{icon} {step.capitalize()}: {row['message'] if row else ''}"[:140]):
            st.markdown(f"_{STEP_INFO[step]}_  \nFile: `{file_name}`")
            if row:
                st.caption(f"{row['started_at']} → {row['finished_at']} · rows in {row['rows_in']}, "
                           f"out {row['rows_out']}, rejected {row['rows_rejected']}")
            if step == "detect" and row:
                st.code(f"sha256 {row['file_sha256']}")
            if step == "route":
                if usage.empty:
                    st.caption("No model calls in this run.")
                else:
                    st.dataframe(usage[["task", "expert", "route_reason", "outcome"]], hide_index=True,
                                 width="stretch")
            if step == "classify":
                cls = usage[usage.task == "classify"]
                if not cls.empty:
                    st.dataframe(cls[["expert", "confidence", "latency_ms", "route_reason"]], hide_index=True,
                                 width="stretch")
                st.markdown(f"Records with category confidence below "
                            f"**{SETTINGS['models']['review_confidence']}** go to the review queue. "
                            "For Jev, confidence is computed from its probabilities:")
                st.latex(r"\mathrm{confidence} = \frac{p_{\max} - 1/n}{1 - 1/n}")
                if not SETTINGS["models"]["jev_enabled"]:
                    st.caption("Jev is off (its key was rejected), so Claude answers the same questions and "
                               "reports its own confidence.")
            if step == "extract":
                ext = usage[usage.task.isin(["extract", "escalate"])]
                if not ext.empty:
                    st.dataframe(ext[["task", "expert", "prompt_version", "input_tokens", "output_tokens",
                                      "estimated_cost_usd", "outcome"]], hide_index=True, width="stretch")
            if step == "validate":
                st.dataframe(query("SELECT row_number, reason FROM review_queue WHERE run_id = ? AND row_number"
                                   " IS NOT NULL UNION ALL SELECT row_number, 'quarantined: ' || reason FROM"
                                   " quarantine_records WHERE run_id = ?", (run_id, run_id)),
                             hide_index=True, width="stretch")
            if step == "decide":
                stat = query("SELECT * FROM stats_log WHERE run_id = ? AND step = 'decide'", (run_id,))
                if not stat.empty:
                    show_stat(stat.to_dict("records")[0])
            if step == "act":
                st.dataframe(query("SELECT incident_id, kind, status, decided_by, detail FROM incident_log"
                                   " WHERE run_id = ?", (run_id,)), hide_index=True, width="stretch")
            show_sql(sql[sql.step == step] if step != "validate" else sql[sql.step == "validate"])


live(choice)
