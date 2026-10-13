"""Human decisions on held imports. Nothing is deleted: release moves an import into the
gold views, reject keeps it out. Both record who decided and when. Used by
scripts/review.py now and by the dashboard in Milestone 5.
"""
from agent.db import Database, utc_now


def held_imports(db: Database) -> list[dict]:
    rows = db.run_sql(
        "review",
        "SELECT i.run_id, i.file_name, i.bookings_count, i.landed_at, q.reason, q.result_json"
        " FROM imports i JOIN review_queue q ON q.run_id = i.run_id AND q.row_number IS NULL"
        " WHERE i.status = 'held' ORDER BY i.landed_at")
    return [dict(r) for r in rows]


def decide_import(db: Database, run_id: str, decision: str, person: str) -> dict:
    if decision not in ("released", "rejected"):
        raise ValueError("decision must be 'released' or 'rejected'")
    if not person.strip():
        raise ValueError("a person's name is required: the agent never decides on its own")
    db.run_id = run_id
    rows = db.run_sql("review", "SELECT file_name, status FROM imports WHERE run_id = ?", (run_id,))
    if not rows:
        raise ValueError(f"no import with run_id {run_id}")
    if rows[0]["status"] != "held":
        raise ValueError(f"{run_id} is {rows[0]['status']}, not held")
    db.file_name = rows[0]["file_name"]
    now = utc_now()
    db.run_sql("review", "UPDATE imports SET status = ?, decided_by = ?, decided_at = ? WHERE run_id = ?",
               (decision, person, now, run_id))
    db.run_sql("review", "UPDATE review_queue SET status = ? WHERE run_id = ? AND row_number IS NULL",
               (decision, run_id))
    db.run_sql("review", "UPDATE incident_log SET status = ?, decided_by = ?, closed_at = ?"
                         " WHERE run_id = ? AND kind = 'trigger' AND status = 'open'",
               (decision, person, now, run_id))
    log_id = db.start_step("act")
    db.finish_step(log_id, "ok", f"{decision} by {person}" +
                   (": now in gold views" if decision == "released" else ": kept out of gold views"))
    return {"run_id": run_id, "file_name": db.file_name, "status": decision, "decided_by": person}
