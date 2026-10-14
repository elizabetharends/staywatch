"""StayWatch dashboard. Run: uv run streamlit run dashboard/app.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

st.set_page_config(page_title="StayWatch", page_icon="🏝️", layout="wide")

pages = [
    st.Page("views/live.py", title="Live run", icon="🔴", default=True),
    st.Page("views/data.py", title="Data", icon="🗂️"),
    st.Page("views/models.py", title="Models (MoE)", icon="🧠"),
    st.Page("views/history.py", title="Pipeline history", icon="⏱️"),
    st.Page("views/analytics.py", title="Analytics", icon="📈"),
    st.Page("views/governance.py", title="Governance", icon="🛡️"),
]
st.navigation(pages).run()
