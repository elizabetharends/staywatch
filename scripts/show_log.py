"""Prints the latest run_log and sql_log rows, so you can watch the agent until the dashboard exists.

Usage:
    uv run python scripts/show_log.py          # last 15 rows of each
    uv run python scripts/show_log.py --rows 40
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.settings import load_settings, resolve  # noqa: E402


def print_table(title: str, rows: list[sqlite3.Row], widths: dict[str, int]) -> None:
    print(f"\n== {title} ==")
    print("  ".join(name.ljust(w) for name, w in widths.items()))
    for row in rows:
        cells = []
        for name, w in widths.items():
            value = "" if row[name] is None else str(row[name])
            cells.append((value[: w - 1] + "…" if len(value) > w else value).ljust(w))
        print("  ".join(cells))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=15)
    args = parser.parse_args()
    path = resolve(load_settings()["paths"]["sqlite"])
    if not path.exists():
        sys.exit(f"No database yet at {path}. Run the agent first.")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # read-only: only the agent writes
    conn.row_factory = sqlite3.Row

    runs = conn.execute(
        "SELECT * FROM (SELECT rowid, * FROM run_log ORDER BY rowid DESC LIMIT ?) ORDER BY rowid",
        (args.rows,)).fetchall()
    print_table("run_log", runs, {"started_at": 25, "step": 7, "file_name": 34,
                                  "file_sha256": 14, "status": 8, "rows_out": 8, "message": 70})
    sqls = conn.execute(
        "SELECT * FROM (SELECT rowid, * FROM sql_log ORDER BY rowid DESC LIMIT ?) ORDER BY rowid",
        (args.rows,)).fetchall()
    print_table("sql_log", sqls, {"step": 7, "file_name": 34, "sql_text": 70,
                                  "params_json": 30, "rows_affected": 5, "duration_ms": 4})
    conn.close()


if __name__ == "__main__":
    main()
