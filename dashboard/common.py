"""Shared helpers for the dashboard pages: read-only queries, formatting, formulas.

Local mode reads data/analytics.sqlite. Cloud mode (Streamlit Community Cloud, when the
GH_DATA_TOKEN secret exists) downloads the snapshot the pipeline pushed to the `data`
branch, through the GitHub contents API, and opens it immutable (read-only, no locks).
"""
import hashlib
import hmac
import json
import sqlite3
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import streamlit as st

from agent.settings import ROOT, load_settings, resolve

SETTINGS = load_settings()
DB_PATH = resolve(SETTINGS["paths"]["sqlite"])


def _secret(name: str):
    try:
        return st.secrets[name]
    except Exception:          # no secrets file locally
        return None


CLOUD = bool(_secret("GH_DATA_TOKEN"))
REPO = _secret("GH_REPO")      # "owner/staywatch"


@st.cache_data(ttl=30, show_spinner=False)
def _download(path: str) -> tuple[bytes | None, str]:
    """GitHub contents API, raw media type. Avoids raw.githubusercontent.com, which caches for minutes.
    Returns (content, problem). Tries the token first; the repo is public, so it falls back to an
    unauthenticated request (60 per hour per IP) and reports what went wrong with the token."""
    problems = []
    token = _secret("GH_DATA_TOKEN")
    for auth in ([f"Bearer {token}"] if token else []) + [None]:
        headers = {"Accept": "application/vnd.github.raw+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "staywatch-dashboard"}
        if auth:
            headers["Authorization"] = auth
        request = urllib.request.Request(f"https://api.github.com/repos/{REPO}/contents/{path}?ref=data",
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                note = "; ".join(problems)
                return response.read(), (f"{note}; downloaded without the token instead" if note else "")
        except urllib.error.HTTPError as err:
            problems.append(f"{'with' if auth else 'without'} GH_DATA_TOKEN: GitHub HTTP {err.code}")
        except urllib.error.URLError as err:
            problems.append(f"could not reach GitHub ({err.reason})")
    return None, "; ".join(problems)


def data_problem() -> str:
    return _download("analytics.sqlite")[1] if CLOUD else ""


def db_path() -> Path | None:
    if not CLOUD:
        return DB_PATH if DB_PATH.exists() else None
    data, _ = _download("analytics.sqlite")
    if not data:
        return None
    path = Path(tempfile.gettempdir()) / f"staywatch_{hashlib.sha256(data).hexdigest()[:12]}.sqlite"
    if not path.exists():
        path.write_bytes(data)
    return path

STEP_INFO = {
    "detect": "Finds the new file, records its name, size and SHA-256 fingerprint, and skips content already seen.",
    "land": "Stores every record exactly as it arrived in TinyDB (the raw layer) and moves the file to processed/.",
    "redact": "Masks guest names, emails, phone numbers and booking IDs before any model sees a record.",
    "route": "The mixture-of-experts router picks an expert for each task and records why.",
    "classify": "Answers three questions per record: category, urgency, and whether it tries to give instructions.",
    "extract": "Turns messy values into clean, typed fields; escalates to a stronger model when unsure.",
    "validate": "Pydantic checks every field; values invented for blank sources are rejected.",
    "load": "Writes typed rows to the silver table bookings and one row per import to imports.",
    "decide": "Runs the anomaly z-score on this import's bookings count against the last 60 published imports.",
    "act": "Publishes the import to the gold views, or holds it, opens an incident and waits for a person.",
}


def connect() -> sqlite3.Connection:
    """Read-only: only the agent writes to the database."""
    path = db_path() if CLOUD else DB_PATH
    conn = sqlite3.connect(f"file:{path}?{'immutable=1' if CLOUD else 'mode=ro'}", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


@st.cache_data(ttl=5, show_spinner=False)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    if (db_path() if CLOUD else (DB_PATH if DB_PATH.exists() else None)) is None:
        return pd.DataFrame()
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def database_ready() -> bool:
    if CLOUD and db_path() is None:
        st.info("No data on the `data` branch yet. Start a run: GitHub → Actions → pipeline → Run workflow.")
        st.caption(f"Download problem: {data_problem() or 'none'} · repo secret GH_REPO = `{REPO}`")
        return False
    if CLOUD and data_problem():
        st.warning(f"Data token problem: {data_problem()}. Check GH_DATA_TOKEN in the app's secrets.")
    if not CLOUD and not DB_PATH.exists():
        st.info("No data yet. Start the agent: `uv run python -m agent.run` and the generator: "
                "`uv run python -m generator.generate`.")
        return False
    return True


def passcode_ok(entered: str) -> bool:
    """Cloud only: visitors can watch, only the reviewer can decide."""
    expected = _secret("REVIEWER_PASSCODE")
    return bool(expected) and hmac.compare_digest(entered.encode(), str(expected).encode())


def dispatch_review(run_id: str, decision: str, person: str) -> tuple[bool, str]:
    """Cloud only: starts .github/workflows/review.yml, which applies the decision on the data branch."""
    body = json.dumps({"ref": "main", "inputs": {"run_id": run_id, "decision": decision,
                                                 "reviewer": person.strip()}}).encode()
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/workflows/review.yml/dispatches", data=body, method="POST",
        headers={"Authorization": f"Bearer {_secret('GH_REVIEW_TOKEN')}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "staywatch-dashboard"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status == 204, "review workflow started"
    except urllib.error.HTTPError as err:
        return False, f"GitHub said HTTP {err.code}"
    except urllib.error.URLError:
        return False, "could not reach GitHub"


def show_sql(rows: pd.DataFrame, limit: int = 12) -> None:
    """Every statement run_sql logged, with its parameters beside it."""
    if rows.empty:
        st.caption("No SQL for this step.")
        return
    grouped = rows.groupby("sql_text", sort=False).agg(
        times=("sql_text", "size"), rows_affected=("rows_affected", "sum"),
        params=("params_json", "first"), ms=("duration_ms", "sum")).reset_index()
    for _, r in grouped.head(limit).iterrows():
        left, right = st.columns([3, 2])
        left.code(r["sql_text"], language="sql")
        times = f" (run {r['times']}×; first values shown)" if r["times"] > 1 else ""
        right.caption(f"Parameters{times}")
        right.code(r["params"], language="json")
        right.caption(f"{r['rows_affected']} row(s), {r['ms']} ms")


def plugged_formula(stat: dict) -> str | None:
    """The formula with this run's values substituted, for st.latex."""
    inputs = stat["inputs"]
    if stat["method"] == "z-score" and stat["result"] is not None:
        return (rf"z = \frac{{{inputs['x']:g} - {inputs[r'\bar{x}']}}}{{{inputs['s']}}} = {stat['result']:.2f}"
                rf"\quad \text{{vs threshold }} \pm {stat['threshold']:g}")
    if stat["method"] == "linear regression" and stat["result"] is not None:
        return (rf"\hat{{y}} = {inputs[r'\beta_0']:.2f} {'-' if inputs[r'\beta_1'] < 0 else '+'} "
                rf"{abs(inputs[r'\beta_1']):.3f}\,x, \quad r = {inputs['r']}, \quad p = {inputs['p']}")
    if stat["method"] == "ETS forecast" and stat["result"] is not None:
        return (rf"\hat{{y}}_{{t+1}} = {inputs[r'\hat{y}_{t+1}']} \pm 1.96 \times {inputs[r'\hat{\sigma}_1']}"
                rf" = [{inputs['interval_t+1'][0]},\ {inputs['interval_t+1'][1]}]")
    return None


def show_stat(stat_row: dict) -> None:
    stat = {**stat_row, "inputs": json.loads(stat_row["inputs_json"]) if isinstance(stat_row.get("inputs_json"), str)
            else stat_row.get("inputs", {})}
    st.markdown(f"**Method:** {stat['method']}")
    st.latex(stat["formula_latex"])
    plugged = plugged_formula(stat)
    if plugged:
        st.latex(plugged)
    st.caption("Inputs")
    st.json(stat["inputs"], expanded=False)
    st.info(stat["interpretation"])


def latest_scores() -> list[dict]:
    folder = ROOT / "gold" / "results"
    return [json.loads(p.read_text()) | {"file": p.name} for p in sorted(folder.glob("scores_*.json"))] \
        if folder.exists() else []


def generator_truth() -> pd.DataFrame:
    if CLOUD:
        data, _ = _download("generator_log.csv")
        if not data:
            return pd.DataFrame()
        import io
        return pd.read_csv(io.BytesIO(data))
    path = resolve(SETTINGS["paths"]["generator_log"])
    return pd.read_csv(path) if Path(path).exists() else pd.DataFrame()
