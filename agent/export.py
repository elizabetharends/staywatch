"""Writes exports/daily_summary.json: what the daily report (Claude Routine) reads."""
import json
from datetime import datetime, timezone
from pathlib import Path

from agent import stats
from agent.db import Database


def daily_summary(db: Database, settings: dict) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    q = lambda sql, params=(): [dict(r) for r in db.conn.execute(sql, params)]  # noqa: E731
    imports = q("SELECT status, COUNT(*) AS n, SUM(bookings_count) AS bookings FROM imports"
                " WHERE landed_at >= ? GROUP BY status", (today,))
    held = q("SELECT file_name, bookings_count, status, decided_by, decided_at FROM imports"
             " WHERE landed_at >= ? AND status IN ('held', 'released', 'rejected') ORDER BY rowid", (today,))
    incidents = q("SELECT kind, status, COUNT(*) AS n FROM incident_log WHERE opened_at >= ?"
                  " GROUP BY kind, status", (today,))
    usage = q("SELECT expert, task, COUNT(*) AS calls, SUM(input_tokens + output_tokens) AS tokens,"
              " ROUND(SUM(estimated_cost_usd), 4) AS cost_usd, ROUND(AVG(latency_ms)) AS avg_latency_ms"
              " FROM model_usage_log WHERE called_at >= ? GROUP BY expert, task", (today,))
    tokens = sum(u["tokens"] or 0 for u in usage)

    # Latest forecast vs actual: forecast the newest published import from the ones before it
    series = [r["bookings_count"] for r in q("SELECT bookings_count FROM gold_import_metrics ORDER BY import_seq")]
    forecast_check = None
    if len(series) > 20:
        f = stats.forecast(series[:-1], horizon=1)
        if f["path"]:
            lo, hi = f["path"]["lower"][0], f["path"]["upper"][0]
            forecast_check = {"forecast": round(f["path"]["mean"][0], 2), "interval": [round(lo, 2), round(hi, 2)],
                              "actual": series[-1], "inside_interval": lo <= series[-1] <= hi,
                              "beats_naive_in_backtest": f["inputs"]["MAE"] < f["inputs"]["MAE_naive"]}
    return {
        "date": today, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "imports_by_status": imports, "held_imports": held, "incidents": incidents,
        "model_usage": usage, "tokens_today": tokens,
        "token_budget_per_day": settings["budget"]["max_tokens_per_day"],
        "over_budget": tokens >= settings["budget"]["max_tokens_per_day"],
        "kill_switch": settings["models"]["kill_switch"], "jev_enabled": settings["models"]["jev_enabled"],
        "trigger_rule": settings["decide"], "forecast_check": forecast_check,
        "open_review_items": q("SELECT COUNT(*) AS n FROM review_queue WHERE status = 'pending'")[0]["n"],
    }


def write_summary(db: Database, settings: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "daily_summary.json"
    path.write_text(json.dumps(daily_summary(db, settings), indent=2, default=str))
    return path
