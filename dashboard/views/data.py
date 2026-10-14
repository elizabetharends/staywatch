import altair as alt
import streamlit as st

from dashboard.common import database_ready, query

st.title("Data")
st.caption("Clean, typed bookings (silver table `bookings`). Gold = imports that were published or released.")
if not database_ready():
    st.stop()

df = query("SELECT b.*, i.status AS import_status FROM bookings b JOIN imports i ON i.run_id = b.run_id")
if df.empty:
    st.info("No bookings loaded yet.")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
listings = c1.multiselect("Listing", sorted(df.listing.dropna().unique()))
channels = c2.multiselect("Channel", sorted(df.channel.dropna().unique()))
categories = c3.multiselect("Category", sorted(df.category.dropna().unique()))
gold_only = c4.toggle("Gold only", value=True)
view = df
for col, chosen in (("listing", listings), ("channel", channels), ("category", categories)):
    if chosen:
        view = view[view[col].isin(chosen)]
if gold_only:
    view = view[view.import_status.isin(["published", "released"])]

imports = query("SELECT * FROM imports")
q1, q2, q3, q4, q5 = st.columns(5)
q1.metric("Bookings shown", len(view))
q2.metric("Imports", len(imports))
q3.metric("Sent to review", f"{(df.status == 'review').mean():.0%}")
total = imports.bookings_count.sum()
q4.metric("Quarantined", f"{imports.records_quarantined.sum() / total:.1%}" if total else "-")
blank = view[["region", "average_daily_rate_usd", "booking_window_days", "booking_value_usd"]].isna().mean()
q5.metric("Blank fields (avg)", f"{blank.mean():.1%}", help="Share of key fields left null because the source was blank")

st.dataframe(view.drop(columns=["record_hash"]), hide_index=True, width="stretch", height=320)

st.subheader("Bookings per import")
per = query("SELECT rowid AS import_seq, file_name, bookings_count, status FROM imports WHERE status != 'loading'"
            " ORDER BY rowid")
st.altair_chart(
    alt.Chart(per).mark_line(point=True, color="#52514e").encode(
        x=alt.X("import_seq:Q", title="Import number"), y=alt.Y("bookings_count:Q", title="Bookings"),
        tooltip=["file_name", "bookings_count", "status"]).properties(height=260), width="stretch")
