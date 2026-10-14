import altair as alt
import pandas as pd
import streamlit as st

from dashboard.common import database_ready, query, show_sql

st.title("Pipeline history")
if not database_ready():
    st.stop()

runs = query("SELECT run_id, file_name FROM run_log WHERE step = 'detect' GROUP BY run_id ORDER BY MIN(rowid) DESC"
             " LIMIT 30")
if runs.empty:
    st.stop()
log = query(f"SELECT * FROM run_log WHERE run_id IN ({','.join('?' * len(runs))})", tuple(runs.run_id))
log["duration_s"] = (pd.to_datetime(log.finished_at) - pd.to_datetime(log.started_at)).dt.total_seconds()
order = ["detect", "land", "redact", "classify", "extract", "validate", "load", "decide", "act", "analyze"]

st.subheader("Step durations, last 30 runs")
st.altair_chart(alt.Chart(log.dropna(subset=["duration_s"])).mark_bar().encode(
    x=alt.X("file_name:N", sort=list(reversed(runs.file_name)), title=None, axis=alt.Axis(labelAngle=-60)),
    y=alt.Y("sum(duration_s):Q", title="Seconds"),
    color=alt.Color("step:N", sort=order, title="Step"),
    tooltip=["file_name", "step", "duration_s", "status", "message"]).properties(height=280), width="stretch")
st.caption("Classify and extract dominate when models are called; with the kill switch on, a run takes well under a second.")

st.subheader("Rows in and out per run")
io = log[log.step.isin(["land", "validate"])].pivot_table(index="file_name", columns="step", values="rows_out",
                                                          aggfunc="last")
st.dataframe(io.rename(columns={"land": "rows landed", "validate": "rows valid or review"}), width="stretch")

st.subheader("Log rows for one run")
pick = st.selectbox("Run", [f"{r.file_name} · {r.run_id}" for r in runs.itertuples()])
run_id = pick.split(" · ")[1]
st.dataframe(log[log.run_id == run_id].sort_values("started_at")[
    ["step", "status", "started_at", "finished_at", "rows_in", "rows_out", "rows_rejected", "message"]],
    hide_index=True, width="stretch")
with st.expander("All SQL for this run"):
    show_sql(query("SELECT * FROM sql_log WHERE run_id = ? ORDER BY rowid", (run_id,)), limit=40)
