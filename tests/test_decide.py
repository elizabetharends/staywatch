import csv
import math
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from agent import stats
from agent.db import Database
from agent.decide import decide
from agent.etl import scan_inbox
from agent.review import decide_import, held_imports
from agent.settings import load_settings
from generator import generate


@pytest.fixture
def settings(tmp_path):
    s = load_settings()
    for key in ("inbox", "processed", "quarantine"):
        s["paths"][key] = str(tmp_path / key)
    s["paths"]["generator_log"] = str(tmp_path / "generator_log.csv")
    s["paths"]["sqlite"] = str(tmp_path / "data" / "analytics.sqlite")
    s["paths"]["tinydb"] = str(tmp_path / "data" / "raw.json")
    s["agent"]["min_file_age_seconds"] = 0
    s["generator"]["resend_rate"] = 0.0
    s["generator"]["injection_rate"] = 0.0
    return s


def db_with_history(tmp_path, counts):
    db = Database(tmp_path / "h.sqlite")
    for i, c in enumerate(counts):
        db.conn.execute("INSERT INTO imports (run_id, file_name, landed_at, bookings_count, status)"
                        " VALUES (?, ?, ?, ?, 'published')", (f"run_{i:03d}", f"f{i}.csv", f"2026-10-13T{i:05d}", c))
    db.conn.commit()
    db.run_id, db.file_name = "run_new", "new.csv"
    return db


NORMAL = [6, 7, 7, 8, 6, 7, 9, 8, 7, 6, 7, 8, 8, 7, 6, 7, 8, 9, 7, 6]


# ---------- z-score trigger ----------

@pytest.mark.parametrize("count,fires", [(21, True), (1, True), (7, False), (9, False)])
def test_trigger_fires_on_spike_and_drop_but_not_normal(tmp_path, count, fires):
    db = db_with_history(tmp_path, NORMAL)
    result = decide(db, load_settings(), count)
    assert result["fired"] is fires
    (logged,) = db.conn.execute("SELECT fired, action, threshold FROM decision_log").fetchall()
    assert tuple(logged) == (int(fires), "hold_and_alert" if fires else "publish", 3.0)
    (formula,) = db.conn.execute("SELECT formula_latex FROM stats_log").fetchone()
    assert r"\bar{x}" in formula


def test_not_enough_history_publishes_without_test(tmp_path):
    db = db_with_history(tmp_path, NORMAL[:5])
    result = decide(db, load_settings(), 30)
    assert result["result"] is None and result["fired"] is False


def test_zscore_values():
    r = stats.zscore(10, [6, 8] * 10)
    assert r["inputs"][r"\bar{x}"] == 7 and math.isclose(r["result"], 3 / np.std([6, 8] * 10, ddof=1), rel_tol=1e-3)


# ---------- end to end: generator -> agent -> held -> person ----------

def run_imports(settings, n, anomaly=False, start=0):
    rng = random.Random(start + 99)
    settings["generator"]["anomaly_rate"] = 1.0 if anomaly else 0.0
    t0 = datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc)
    for k in range(start, start + n):
        generate.write_import(settings, rng, now=t0 + timedelta(minutes=k))
    return scan_inbox(settings)


def query(settings, sql, params=()):
    conn = sqlite3.connect(settings["paths"]["sqlite"])
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def test_seeded_anomaly_is_held_and_normal_imports_are_published(settings):
    normal = run_imports(settings, 15)
    assert all(r["status"] == "published" for r in normal)
    (held,) = run_imports(settings, 1, anomaly=True, start=15)
    with open(settings["paths"]["generator_log"]) as f:
        truth = list(csv.DictReader(f))[-1]
    assert truth["anomaly"] == "1"                              # the generator seeded it
    assert held["status"] == "held" and abs(held["z"]) > 3
    assert query(settings, "SELECT kind, status FROM incident_log WHERE kind = 'trigger'") == [("trigger", "open")]
    assert not query(settings, "SELECT 1 FROM gold_import_metrics WHERE run_id = ?", (held["run_id"],))
    assert not query(settings, "SELECT 1 FROM gold_bookings WHERE run_id = ?", (held["run_id"],))


def test_person_releases_or_rejects_a_held_import(settings):
    run_imports(settings, 15)
    (held,) = run_imports(settings, 1, anomaly=True, start=15)
    db = Database(generate.resolve(settings["paths"]["sqlite"]))
    try:
        (queued,) = held_imports(db)
        assert queued["run_id"] == held["run_id"] and "explanation" in queued["result_json"]
        with pytest.raises(ValueError):
            decide_import(db, held["run_id"], "released", "  ")     # a person must be named
        decide_import(db, held["run_id"], "released", "EA")
        with pytest.raises(ValueError):
            decide_import(db, held["run_id"], "rejected", "EA")     # already decided
    finally:
        db.close()
    assert query(settings, "SELECT status, decided_by FROM imports WHERE run_id = ?",
                 (held["run_id"],)) == [("released", "EA")]
    assert query(settings, "SELECT status, decided_by FROM incident_log WHERE kind = 'trigger'") == [("released", "EA")]
    assert query(settings, "SELECT 1 FROM gold_import_metrics WHERE run_id = ?", (held["run_id"],))


def test_held_imports_stay_out_of_the_baseline(settings):
    run_imports(settings, 15)
    run_imports(settings, 1, anomaly=True, start=15)
    db = Database(generate.resolve(settings["paths"]["sqlite"]))
    db.run_id = "probe"
    history = [r[0] for r in db.conn.execute(
        "SELECT bookings_count FROM imports WHERE status IN ('published', 'released')")]
    db.close()
    assert len(history) == 15 and max(history) <= 10


# ---------- regression and forecast ----------

def test_regression_finds_planted_negative_slope():
    rng = np.random.default_rng(1)
    x = rng.integers(1, 90, 200)
    y = 150 * (1.15 - 0.004 * x) + rng.normal(0, 8, 200)
    r = stats.regression(x.tolist(), y.tolist())
    assert r["result"] < 0 and r["inputs"]["p"] < 0.001
    lo, hi = r["inputs"][r"\beta_1\ 95\%\ CI"]
    assert lo < -0.6 < hi                                       # true slope is 150 * -0.004


def test_forecast_has_interval_and_backtest():
    i = np.arange(90)
    y = 6 + np.minimum(0.03 * i, 2) + 1.5 * np.sin(2 * np.pi * i / 30) + np.random.default_rng(2).normal(0, 0.6, 90)
    f = stats.forecast(y.round().tolist(), horizon=10)
    assert f["inputs"]["seasonal_period"] == 30
    lo, hi = f["inputs"]["interval_t+1"]
    assert lo < f["result"] < hi
    assert f["inputs"]["MAE"] > 0 and len(f["path"]["mean"]) == 10


def test_precision_recall():
    r = stats.precision_recall(tp=4, fp=1, fn=0)
    assert r["result"] == 0.8 and "recall 100%" in r["interpretation"]
