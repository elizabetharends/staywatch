"""Load, Decide, Act and Analyze: the steps after validation.

Load writes typed rows to silver. Decide runs the anomaly z-score on the import's
bookings count. Act publishes the import to the gold views, or holds it, opens an
incident, asks Claude for a plain-English explanation and waits for a person.
"""
import json
import uuid

from agent import stats
from agent.db import Database, utc_now
from agent.experts import ModelRunner

BOOKING_COLUMNS = ["run_id", "file_name", "row_number", "record_hash", "listing", "region", "channel",
                   "booked_on", "check_in", "nights", "booking_value_usd", "average_daily_rate_usd",
                   "booking_window_days", "arrival_time", "summary", "category", "urgency",
                   "category_confidence", "classify_expert", "extract_expert", "status", "loaded_at"]


def load(db: Database, sha: str, landed_at: str, bookings_count: int, rows: list[dict],
         counts: dict) -> None:
    log_id = db.start_step("load", sha256=sha, rows_in=len(rows))
    db.run_sql("load", "INSERT OR REPLACE INTO imports VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'loading', NULL, NULL)",
               (db.run_id, db.file_name, sha, landed_at, bookings_count,
                counts["valid"], counts["review"], counts["quarantined"] + counts["injections"]))
    placeholders = ", ".join("?" * len(BOOKING_COLUMNS))
    for row in rows:
        db.run_sql("load", f"INSERT INTO bookings ({', '.join(BOOKING_COLUMNS)}) VALUES ({placeholders})",
                   tuple(row.get(c) for c in BOOKING_COLUMNS))
    db.finish_step(log_id, "ok", f"{len(rows)} typed rows into silver table bookings; import has "
                                 f"{bookings_count} distinct bookings (the forecast measure)",
                   rows_out=len(rows))


def decide(db: Database, settings: dict, bookings_count: int) -> dict:
    cfg = settings["decide"]
    log_id = db.start_step("decide", rows_in=bookings_count)
    history = [r["bookings_count"] for r in db.run_sql(
        "decide",
        "SELECT bookings_count FROM imports WHERE status IN ('published', 'released') AND run_id != ?"
        " ORDER BY rowid DESC LIMIT ?",
        (db.run_id, cfg["history_imports"]))]
    result = stats.zscore(bookings_count, history, cfg["z_threshold"])
    db.log_stats("decide", result)
    fired = result["result"] is not None and abs(result["result"]) > cfg["z_threshold"]
    action = "hold_and_alert" if fired else "publish"
    db.log_decision(cfg["rule"], result["result"], cfg["z_threshold"], None, fired, action)
    db.finish_step(log_id, "held" if fired else "ok",
                   f"z = {result['result']} vs ±{cfg['z_threshold']}: {action}" if result["result"] is not None
                   else result["interpretation"], rows_out=bookings_count)
    return {**result, "fired": fired, "action": action}


def act(db: Database, runner: ModelRunner, decision: dict) -> str:
    log_id = db.start_step("act")
    if not decision["fired"]:
        db.run_sql("act", "UPDATE imports SET status = 'published' WHERE run_id = ?", (db.run_id,))
        db.finish_step(log_id, "ok", "published to gold views")
        return "published"

    explanation, expert = runner.explain(
        {"file_name": db.file_name, "rule": "anomaly z-score on bookings per import", **decision["inputs"],
         "z": decision["result"], "threshold": decision["threshold"],
         "interpretation": decision["interpretation"]})
    db.run_sql("act", "UPDATE imports SET status = 'held' WHERE run_id = ?", (db.run_id,))
    incident_id = f"inc_{uuid.uuid4().hex[:8]}"
    db.open_incident(incident_id, "trigger", f"{db.file_name}: {decision['interpretation']}")
    db.run_sql("act", "INSERT INTO review_queue VALUES (?, ?, NULL, ?, NULL, ?, 'pending', ?)",
               (db.run_id, db.file_name, f"import held: z = {decision['result']}",
                json.dumps({"incident_id": incident_id, "explanation": explanation, "explained_by": expert,
                            **decision}, default=str), utc_now()))
    db.finish_step(log_id, "held", f"held out of gold, incident {incident_id} opened; explanation by {expert}. "
                                   f"Waiting for a person: scripts/review.py release|reject {db.run_id}")
    return "held"


def analyze(db: Database) -> dict:
    """Regression of nightly rate on booking window over all gold bookings."""
    rows = db.run_sql(
        "analyze",
        "SELECT booking_window_days AS x, average_daily_rate_usd AS y FROM gold_bookings"
        " WHERE booking_window_days IS NOT NULL AND average_daily_rate_usd IS NOT NULL")
    result = stats.regression([r["x"] for r in rows], [r["y"] for r in rows])
    db.log_stats("analyze", result)
    return result
