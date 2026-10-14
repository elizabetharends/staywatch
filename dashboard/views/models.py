import altair as alt
import streamlit as st

from dashboard.common import SETTINGS, database_ready, query

st.title("Models (mixture of experts)")
st.markdown("Inside some large models, *mixture of experts* is an architecture: a learned gate sends each token "
            "to specialist sub-networks. StayWatch uses the idea **at the application level**: a readable router "
            "(`agent/router.py`) sends each task to the expert model best suited to it and logs why.")
if not database_ready():
    st.stop()

usage = query("SELECT * FROM model_usage_log ORDER BY rowid DESC LIMIT 3000")
if usage.empty:
    st.info("No expert calls logged yet.")
    st.stop()
usage["called_at"] = usage.called_at.astype("datetime64[ns, UTC]")
TASKS = ["classify", "extract", "escalate", "explain"]
task_colors = alt.Scale(domain=TASKS, range=["#2a78d6", "#eb6834", "#1baf7a", "#eda100"])

st.subheader("Who did what, when")
st.altair_chart(alt.Chart(usage).mark_circle(size=70, opacity=0.8).encode(
    x=alt.X("called_at:T", title="Time (UTC)"), y=alt.Y("expert:N", title=None),
    color=alt.Color("task:N", scale=task_colors, title="Task"),
    tooltip=["called_at:T", "task", "expert", "route_reason", "confidence", "outcome", "prompt_version"],
).properties(height=220), width="stretch")

st.subheader("Task to expert")
st.altair_chart(alt.Chart(usage).mark_bar().encode(
    x=alt.X("count():Q", title="Calls"), y=alt.Y("task:N", sort=TASKS, title=None),
    color=alt.Color("expert:N", title="Expert", scale=alt.Scale(range=["#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7"])),
    yOffset="expert:N", tooltip=["task", "expert", "count()"]).properties(height=240), width="stretch")
st.caption("Escalations are Sonnet re-extractions after Haiku was unsure or invalid; rules rows are fallbacks "
           "(kill switch, budget, empty messages, or a model being unavailable).")

st.subheader("Per expert")
paid = usage[usage.outcome != "cached"]
table = usage.groupby("expert").agg(calls=("call_id", "size"), cached=("outcome", lambda s: (s == "cached").sum()),
                                    avg_confidence=("confidence", "mean"), input_tokens=("input_tokens", "sum"),
                                    output_tokens=("output_tokens", "sum"), cost_usd=("estimated_cost_usd", "sum"))
table["avg_latency_ms"] = paid.groupby("expert").latency_ms.mean()
st.dataframe(table.round(4), width="stretch")
st.subheader("Why the router chose each expert")
st.dataframe(usage.groupby(["task", "expert", "route_reason"]).size().rename("calls").reset_index()
             .sort_values("calls", ascending=False), hide_index=True, width="stretch")
st.caption(f"Jev enabled: {SETTINGS['models']['jev_enabled']} · extractor {SETTINGS['models']['extractor']} · "
           f"escalation {SETTINGS['models']['escalation']} · kill switch {SETTINGS['models']['kill_switch']}")
