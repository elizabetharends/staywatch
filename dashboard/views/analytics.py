import json

import altair as alt
import pandas as pd
import streamlit as st

from agent import stats
from dashboard.common import SETTINGS, database_ready, generator_truth, query, show_stat

st.title("Analytics")
st.caption("Computed live from the gold views (published or released imports only).")
if not database_ready():
    st.stop()
threshold = SETTINGS["decide"]["z_threshold"]
INK, BLUE, ORANGE, RED = "#52514e", "#2a78d6", "#eb6834", "#d03b3b"


def how(sql: str, stat: dict):
    with st.expander("How this was calculated"):
        st.code(sql, language="sql")
        show_stat(stat)


# ---------- 1. Anomaly trigger ----------
st.header("1 · Anomaly trigger (z-score)")
st.markdown(f"**What it measures:** how many standard deviations an import's bookings count is from the "
            f"average of the last {SETTINGS['decide']['history_imports']} published imports. "
            f"**Decision it changes:** above ±{threshold:g}, the import is held for a person.")
TRIGGER_SQL = ("SELECT i.rowid AS import_seq, i.file_name, i.bookings_count, i.status, s.inputs_json, s.result\n"
               "FROM imports i JOIN stats_log s ON s.run_id = i.run_id AND s.step = 'decide'\nORDER BY i.rowid")
trig = query(TRIGGER_SQL)
if not trig.empty:
    inputs = trig.inputs_json.apply(json.loads)
    trig["mean"] = inputs.apply(lambda d: d.get(r"\bar{x}"))
    trig["s"] = inputs.apply(lambda d: d.get("s"))
    trig["lower"], trig["upper"] = trig["mean"] - threshold * trig["s"], trig["mean"] + threshold * trig["s"]
    trig["held"] = trig.status.isin(["held", "released", "rejected"])
    base = alt.Chart(trig).encode(x=alt.X("import_seq:Q", title="Import number"))
    band = base.mark_area(opacity=0.15, color=BLUE, interpolate="step").encode(y="lower:Q", y2="upper:Q")
    line = base.mark_line(color=INK).encode(y=alt.Y("bookings_count:Q", title="Bookings"))
    pts = base.mark_point(filled=True, size=40, color=INK).encode(
        y="bookings_count:Q", tooltip=["file_name", "bookings_count", "result", "status"])
    held = base.transform_filter("datum.held").mark_point(shape="cross", size=160, filled=True, color=RED).encode(
        y="bookings_count:Q", tooltip=["file_name", "bookings_count", "result", "status"])
    st.altair_chart((band + line + pts + held).properties(height=280), width="stretch")
    st.caption(f"Blue band: x̄ ± {threshold:g}·s for each decision. Red crosses: held imports.")
    last = query("SELECT * FROM stats_log WHERE step = 'decide' AND result IS NOT NULL ORDER BY rowid DESC LIMIT 1")
    if not last.empty:
        how(TRIGGER_SQL, last.to_dict("records")[0])

    truth = generator_truth()
    if not truth.empty:
        merged = trig.merge(truth[truth.is_resend_of.isna()][["file_name", "anomaly"]], on="file_name", how="left")
        tp = int(((merged.held) & (merged.anomaly == 1)).sum())
        fp = int(((merged.held) & (merged.anomaly != 1)).sum())
        fn = int(((~merged.held) & (merged.anomaly == 1)).sum())
        pr = stats.precision_recall(tp, fp, fn)
        st.markdown("**Trigger precision and recall** against `generator_log.csv` (which imports were seeded):")
        a, b, c = st.columns(3)
        a.metric("Precision", f"{tp / (tp + fp):.0%}" if tp + fp else "-")
        b.metric("Recall", f"{tp / (tp + fn):.0%}" if tp + fn else "-")
        c.metric("TP / FP / FN", f"{tp} / {fp} / {fn}")
        st.latex(pr["formula_latex"])
        st.caption(pr["interpretation"])

# ---------- 2. Regression ----------
st.header("2 · Nightly rate vs. booking window (regression)")
st.markdown("**What it measures:** whether booking further ahead goes with a lower nightly rate, and by how much "
            "per day. **Decision it changes:** a clear negative slope supports raising last-minute prices; "
            "no slope means the early-booking discount is not doing anything.")
REG_SQL = ("SELECT booking_window_days AS x, average_daily_rate_usd AS y FROM gold_bookings\n"
           "WHERE booking_window_days IS NOT NULL AND average_daily_rate_usd IS NOT NULL")
pairs = query(REG_SQL)
reg = stats.regression(pairs.x.tolist(), pairs.y.tolist()) if not pairs.empty else None
if reg and reg["result"] is not None:
    b0, b1 = reg["inputs"][r"\beta_0"], reg["inputs"][r"\beta_1"]
    fit = pd.DataFrame({"x": [pairs.x.min(), pairs.x.max()]})
    fit["y"] = b0 + b1 * fit.x
    scatter = alt.Chart(pairs).mark_circle(size=30, opacity=0.5, color=BLUE).encode(
        x=alt.X("x:Q", title="Booking window (days)"), y=alt.Y("y:Q", title="Average daily rate (USD)"),
        tooltip=["x", "y"])
    st.altair_chart((scatter + alt.Chart(fit).mark_line(color="#0b0b0b", strokeWidth=2).encode(x="x", y="y"))
                    .properties(height=300), width="stretch")
    st.info(reg["interpretation"])
    how(REG_SQL, reg)
else:
    st.caption("Not enough gold bookings yet.")

# ---------- 3. Forecast ----------
st.header("3 · Forecast of bookings per import")
st.markdown("**What it measures:** the expected bookings in the next imports, with a 95% prediction interval, "
            "backtested against a naive 'same as last import' forecast. **Decision it changes:** if it does not "
            "beat the naive forecast, it is not worth using for planning.")
FC_SQL = "SELECT bookings_count FROM gold_import_metrics ORDER BY import_seq"
series = query(FC_SQL).bookings_count.tolist() if not query(FC_SQL).empty else []
with st.spinner("Fitting the forecast..."):
    fc = stats.forecast(series, horizon=10, season=30)
if fc["path"]:
    n, p = len(series), fc["path"]
    hist = pd.DataFrame({"t": range(n), "value": series, "series": "actual"})
    fut = pd.DataFrame({"t": range(n, n + 10), "value": p["mean"], "lower": p["lower"], "upper": p["upper"]})
    test = pd.DataFrame({"t": list(range(n - p["test_n"], n)) * 2,
                         "value": p["backtest_pred"] + p["backtest_naive"],
                         "series": ["backtest: ETS"] * p["test_n"] + ["backtest: same as last"] * p["test_n"]})
    chart = (alt.Chart(hist).mark_line(color=INK).encode(x=alt.X("t:Q", title="Import number"),
                                                        y=alt.Y("value:Q", title="Bookings"))
             + alt.Chart(fut).mark_area(opacity=0.18, color=BLUE).encode(x="t:Q", y="lower:Q", y2="upper:Q")
             + alt.Chart(fut).mark_line(color=BLUE, strokeWidth=2).encode(x="t:Q", y="value:Q",
                                                                         tooltip=["t", "value", "lower", "upper"])
             + alt.Chart(test).mark_line(strokeDash=[4, 3], strokeWidth=2).encode(
                 x="t:Q", y="value:Q", color=alt.Color("series:N", scale=alt.Scale(range=[BLUE, ORANGE]),
                                                       title=None)))
    st.altair_chart(chart.properties(height=300), width="stretch")
    m1, m2 = st.columns(2)
    m1.metric("Backtest MAE: ETS", fc["inputs"]["MAE"])
    m2.metric("Backtest MAE: same as last import", fc["inputs"]["MAE_naive"])
    st.info(fc["interpretation"])
    st.latex(r"\mathrm{MAE} = \frac{1}{n}\sum_t \lvert y_t - \hat{y}_t \rvert")
    how(FC_SQL, fc)
else:
    st.caption(fc["interpretation"])
