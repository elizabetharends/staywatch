import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent.db import Database
from agent.decisions import pending_requests, request_decision
from agent.etl import scan_inbox
from agent.settings import load_settings
from dashboard.flow import build_dot
from generator import generate


@pytest.fixture
def settings(tmp_path):
    s = load_settings()
    for key in ("inbox", "processed", "quarantine", "decisions"):
        s["paths"][key] = str(tmp_path / key)
    s["paths"]["generator_log"] = str(tmp_path / "generator_log.csv")
    s["paths"]["sqlite"] = str(tmp_path / "data" / "analytics.sqlite")
    s["paths"]["tinydb"] = str(tmp_path / "data" / "raw.json")
    s["agent"]["min_file_age_seconds"] = 0
    s["generator"].update(resend_rate=0.0, injection_rate=0.0, anomaly_rate=0.0)
    return s


def history_then_spike(settings):
    rng, t0 = random.Random(5), datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc)
    for k in range(15):
        generate.write_import(settings, rng, now=t0 + timedelta(minutes=k))
    scan_inbox(settings)
    generate.write_import(settings, rng, now=t0 + timedelta(minutes=15), force_anomaly="spike")
    (held,) = scan_inbox(settings)
    return held


def status_of(settings, run_id):
    conn = sqlite3.connect(settings["paths"]["sqlite"])
    row = conn.execute("SELECT status, decided_by FROM imports WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    return row


def test_release_click_reaches_the_agent_through_a_request_file(settings):
    held = history_then_spike(settings)
    assert held["status"] == "held"
    request_decision(settings, held["run_id"], "released", "EA")
    assert pending_requests(settings)[0]["person"] == "EA"
    assert status_of(settings, held["run_id"]) == ("held", None)       # nothing changes until the agent runs
    (applied,) = scan_inbox(settings)
    assert "released by EA" in applied["outcome"]
    assert status_of(settings, held["run_id"]) == ("released", "EA")
    assert pending_requests(settings) == []


def test_bad_request_is_moved_to_failed_not_applied(settings):
    held = history_then_spike(settings)
    request_decision(settings, "run_does_not_exist", "rejected", "EA")
    (result,) = scan_inbox(settings)
    assert "failed" in result["outcome"]
    assert status_of(settings, held["run_id"]) == ("held", None)


def test_request_needs_a_name(settings):
    with pytest.raises(ValueError):
        request_decision(settings, "run_x", "released", " ")


def test_dashboard_connection_is_read_only(settings, monkeypatch):
    history_then_spike(settings)
    import dashboard.common as common
    monkeypatch.setattr(common, "DB_PATH", generate.resolve(settings["paths"]["sqlite"]))
    with common.connect() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE imports SET status = 'released'")


def test_flow_chart_shows_hold_branch_and_values(settings):
    held = history_then_spike(settings)
    db = Database(generate.resolve(settings["paths"]["sqlite"]))
    rows = lambda sql: [dict(r) for r in db.conn.execute(sql, (held["run_id"],))]  # noqa: E731
    steps = {r["step"]: r for r in rows("SELECT * FROM run_log WHERE run_id = ? ORDER BY rowid")}
    dot = build_dot(steps, rows("SELECT * FROM model_usage_log WHERE run_id = ?"),
                    rows("SELECT * FROM decision_log WHERE run_id = ?")[0],
                    rows("SELECT * FROM imports WHERE run_id = ?")[0], held["file_name"])
    db.close()
    assert "decide -> hold [style=solid]" in dot and "decide -> publish [style=dashed]" in dot
    assert f"z = {held['z']:+.2f}" in dot and "waiting for a person" in dot
    assert held["file_name"].replace("staywatch_", "") in dot


def test_flow_chart_for_a_published_run_and_an_empty_run():
    steps = {s: {"status": "ok", "rows_out": 7} for s in
             ["detect", "land", "redact", "classify", "extract", "validate", "load", "decide", "act"]}
    dot = build_dot(steps, [], {"statistic": 0.4, "threshold": 3.0}, {"status": "published"}, "f.csv")
    assert "decide -> publish [style=solid]" in dot and "decide -> hold [style=dashed]" in dot
    empty = build_dot({}, [], None, None, "")
    assert empty.count('fillcolor="#f1f0ec"') == 11                    # every node grey: waiting
