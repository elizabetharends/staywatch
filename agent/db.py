"""SQLite setup, the run_sql helper and step logging.

Every query the agent runs goes through run_sql(), which records it in sql_log with
its ? placeholders and values, so every step is visible. Writes to the log tables
themselves go through _write_log() and are not logged again (that would loop forever).
"""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_log (
  run_id TEXT, step TEXT,
  file_name TEXT, file_sha256 TEXT,
  started_at TEXT, finished_at TEXT,
  rows_in INTEGER, rows_out INTEGER, rows_rejected INTEGER,
  status TEXT,
  message TEXT
);
CREATE TABLE IF NOT EXISTS sql_log (
  run_id TEXT, step TEXT, file_name TEXT,
  sql_text TEXT,
  params_json TEXT,
  rows_affected INTEGER, duration_ms INTEGER, executed_at TEXT
);
CREATE TABLE IF NOT EXISTS model_usage_log (
  call_id TEXT, run_id TEXT, step TEXT,
  task TEXT, expert TEXT, route_reason TEXT, prompt_version TEXT,
  input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER,
  confidence REAL, estimated_cost_usd REAL,
  pii_redacted INTEGER, outcome TEXT,
  called_at TEXT
);
CREATE TABLE IF NOT EXISTS stats_log (
  run_id TEXT, step TEXT, method TEXT,
  formula_latex TEXT, inputs_json TEXT,
  result REAL, threshold REAL,
  interpretation TEXT, computed_at TEXT
);
CREATE TABLE IF NOT EXISTS decision_log (
  run_id TEXT, rule TEXT,
  statistic REAL, threshold REAL, p_value REAL,
  fired INTEGER, action TEXT,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS incident_log (
  incident_id TEXT, run_id TEXT, kind TEXT,
  detail TEXT, status TEXT,
  decided_by TEXT, opened_at TEXT, closed_at TEXT
);
CREATE INDEX IF NOT EXISTS run_log_sha ON run_log (file_sha256, step, status);

-- Model answers cached by input hash, so a re-run costs nothing
CREATE TABLE IF NOT EXISTS model_cache (
  cache_key TEXT PRIMARY KEY, task TEXT, expert TEXT, prompt_version TEXT,
  response_text TEXT, created_at TEXT
);
-- Records a person must look at (low confidence); nothing here is deleted automatically
CREATE TABLE IF NOT EXISTS review_queue (
  run_id TEXT, file_name TEXT, row_number INTEGER, reason TEXT,
  record_json TEXT, result_json TEXT, status TEXT, created_at TEXT
);
-- Records held out of the pipeline (injection attempts, failed validation)
CREATE TABLE IF NOT EXISTS quarantine_records (
  run_id TEXT, file_name TEXT, row_number INTEGER, reason TEXT,
  record_json TEXT, created_at TEXT
);
-- Silver: one row per import file, with the forecast measure and the decision status
CREATE TABLE IF NOT EXISTS imports (
  run_id TEXT PRIMARY KEY, file_name TEXT, file_sha256 TEXT, landed_at TEXT,
  bookings_count INTEGER,    -- distinct booking rows in the file (the forecast measure)
  records_valid INTEGER, records_review INTEGER, records_quarantined INTEGER,
  status TEXT,               -- loading, published, held, released, rejected
  decided_by TEXT, decided_at TEXT
);
-- Silver: typed, validated bookings (valid or review)
CREATE TABLE IF NOT EXISTS bookings (
  run_id TEXT, file_name TEXT, row_number INTEGER, record_hash TEXT,
  listing TEXT, region TEXT, channel TEXT, booked_on TEXT, check_in TEXT, nights INTEGER,
  booking_value_usd REAL, average_daily_rate_usd REAL, booking_window_days INTEGER,
  arrival_time TEXT, summary TEXT,
  category TEXT, urgency TEXT, category_confidence REAL,
  classify_expert TEXT, extract_expert TEXT, status TEXT, loaded_at TEXT
);
-- Gold: only imports that were published, or held and then released by a person
DROP VIEW IF EXISTS gold_import_metrics;
CREATE VIEW gold_import_metrics AS
  SELECT rowid AS import_seq, run_id, file_name, landed_at, bookings_count, records_valid, records_review,
         records_quarantined, status
  FROM imports WHERE status IN ('published', 'released');
DROP VIEW IF EXISTS gold_bookings;
CREATE VIEW gold_bookings AS
  SELECT b.* FROM bookings b JOIN imports i ON i.run_id = b.run_id
  WHERE i.status IN ('published', 'released');
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path, max_text: int = 80):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")  # dashboard can read while the agent writes
        self.conn.executescript(SCHEMA)
        self.max_text = max_text
        self.run_id: str | None = None      # set per file, so every log row knows its import
        self.file_name: str | None = None

    def close(self) -> None:
        self.conn.close()

    def _shorten(self, value):
        if isinstance(value, str) and len(value) > self.max_text:
            return value[: self.max_text] + "…"
        return value

    def run_sql(self, step: str, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        """Runs one statement with ? placeholders and records it in sql_log."""
        start = time.perf_counter()
        cursor = self.conn.execute(sql, params)
        rows = cursor.fetchall()
        affected = len(rows) if cursor.description else cursor.rowcount
        self.conn.commit()
        duration_ms = round((time.perf_counter() - start) * 1000)
        self._write_log(
            "INSERT INTO sql_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, step, self.file_name, " ".join(sql.split()),
             json.dumps([self._shorten(p) for p in params], ensure_ascii=False),
             affected, duration_ms, utc_now()),
        )
        return rows

    def _write_log(self, sql: str, params: tuple) -> int:
        cursor = self.conn.execute(sql, params)
        self.conn.commit()
        return cursor.lastrowid

    def start_step(self, step: str, sha256: str | None = None, rows_in: int | None = None) -> int:
        return self._write_log(
            "INSERT INTO run_log (run_id, step, file_name, file_sha256, started_at, rows_in, status)"
            " VALUES (?, ?, ?, ?, ?, ?, 'running')",
            (self.run_id, step, self.file_name, sha256, utc_now(), rows_in),
        )

    def finish_step(self, log_id: int, status: str, message: str = "", rows_in: int | None = None,
                    rows_out: int | None = None, rows_rejected: int | None = None) -> None:
        self._write_log(
            "UPDATE run_log SET finished_at = ?, status = ?, message = ?,"
            " rows_in = COALESCE(?, rows_in), rows_out = ?, rows_rejected = ? WHERE rowid = ?",
            (utc_now(), status, message, rows_in, rows_out, rows_rejected, log_id),
        )

    def log_model_call(self, **row) -> None:
        columns = ["call_id", "run_id", "step", "task", "expert", "route_reason", "prompt_version",
                   "input_tokens", "output_tokens", "latency_ms", "confidence",
                   "estimated_cost_usd", "pii_redacted", "outcome", "called_at"]
        row = {"run_id": self.run_id, "called_at": utc_now(), **row}
        self._write_log(
            f"INSERT INTO model_usage_log ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            tuple(row.get(c) for c in columns),
        )

    def log_stats(self, step: str, result: dict) -> None:
        self._write_log(
            "INSERT INTO stats_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, step, result["method"], result["formula_latex"],
             json.dumps(result["inputs"], ensure_ascii=False, default=str), result["result"],
             result["threshold"], result["interpretation"], utc_now()),
        )

    def log_decision(self, rule: str, statistic, threshold, p_value, fired: bool, action: str) -> None:
        self._write_log(
            "INSERT INTO decision_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, rule, statistic, threshold, p_value, int(fired), action, utc_now()),
        )

    def open_incident(self, incident_id: str, kind: str, detail: str) -> None:
        self._write_log(
            "INSERT INTO incident_log (incident_id, run_id, kind, detail, status, opened_at)"
            " VALUES (?, ?, ?, ?, 'open', ?)",
            (incident_id, self.run_id, kind, detail, utc_now()),
        )
