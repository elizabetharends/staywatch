"""Scores the anomaly trigger against the generator's ground truth (generator_log.csv).

Usage: uv run python scripts/score_trigger.py
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import stats  # noqa: E402
from agent.db import Database  # noqa: E402
from agent.settings import ROOT, load_settings, resolve  # noqa: E402


def main() -> None:
    settings = load_settings()
    with open(resolve(settings["paths"]["generator_log"]), newline="") as f:
        truth = {r["file_name"]: r for r in csv.DictReader(f) if not r["is_resend_of"]}
    db = Database(resolve(settings["paths"]["sqlite"]))
    rows = db.conn.execute(
        "SELECT i.file_name, d.statistic, d.fired FROM decision_log d JOIN imports i ON i.run_id = d.run_id"
        " ORDER BY i.landed_at").fetchall()
    tp = fp = fn = tn = 0
    misses, false_alarms = [], []
    for r in rows:
        seeded = truth.get(r["file_name"], {}).get("anomaly") == "1"
        if r["fired"] and seeded:
            tp += 1
        elif r["fired"]:
            fp += 1
            false_alarms.append(f"{r['file_name']} (z={r['statistic']})")
        elif seeded:
            fn += 1
            misses.append(f"{r['file_name']} (z={r['statistic']}, {truth[r['file_name']]['anomaly_kind']})")
        else:
            tn += 1
    result = stats.precision_recall(tp, fp, fn)
    db.run_id = "trigger_score"
    db.log_stats("score", result)
    db.close()
    print(f"{len(rows)} decisions scored against generator_log.csv")
    print(f"TP {tp}  FP {fp}  FN {fn}  TN {tn}")
    print(result["interpretation"])
    if misses:
        print("Missed:", ", ".join(misses))
    if false_alarms:
        print("False alarms:", ", ".join(false_alarms))
    out = ROOT / "docs" / "trigger_score.json"
    out.write_text(json.dumps({"TP": tp, "FP": fp, "FN": fn, "TN": tn, "precision": result["result"],
                               "interpretation": result["interpretation"], "misses": misses,
                               "false_alarms": false_alarms, "rule": settings["decide"]}, indent=2))
    print(f"Saved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
