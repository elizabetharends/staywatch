"""The agent in the cloud: the same pipeline as local mode, run once a minute inside one
GitHub Actions job, with state kept on the `data` branch.

Usage (what .github/workflows/pipeline.yml runs):
    uv run python -m agent.cloud_loop --minutes 30 --state state/ --requests requests/
    uv run python -m agent.cloud_loop --minutes 0 --state state/ --requests requests/   # apply reviews only

state/     worktree of the `data` branch: raw.json, analytics.sqlite, generator_log.csv, exports/
requests/  worktree of the `requests` branch: Release/Reject requests written by review.yml
"""
import argparse
import json
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from agent.db import Database, utc_now
from agent.etl import scan_inbox
from agent.export import write_summary
from agent.review import decide_import
from agent.settings import load_settings
from generator.generate import write_import

MAX_MINUTES = 45      # keeps every run demo-length and inside the job timeout


def cloud_settings(state: Path, work: Path) -> dict:
    settings = load_settings()
    settings["paths"].update(
        sqlite=str(state / "analytics.sqlite"), tinydb=str(state / "raw.json"),
        generator_log=str(state / "generator_log.csv"),
        inbox=str(work / "inbox"), processed=str(work / "processed"), quarantine=str(work / "quarantine"),
        decisions=str(work / "decisions"))
    settings["agent"]["min_file_age_seconds"] = 0    # we wrote the file ourselves and closed it
    return settings


def apply_requests(requests_dir: Path, db: Database) -> list[dict]:
    """Applies review requests not applied before. The requests branch only ever grows;
    which requests were applied is recorded in the database, so applying is idempotent."""
    if not requests_dir.exists():
        return []
    db.conn.execute("CREATE TABLE IF NOT EXISTS applied_requests (request TEXT PRIMARY KEY, ok INTEGER,"
                    " detail TEXT, applied_at TEXT)")
    done = {r[0] for r in db.conn.execute("SELECT request FROM applied_requests")}
    results = []
    for path in sorted(requests_dir.glob("*.json")):
        if path.name in done:
            continue
        request = json.loads(path.read_text())
        try:
            result = decide_import(db, request["run_id"], request["decision"], request["person"])
            ok, detail = True, f"{result['status']} by {result['decided_by']}"
        except (ValueError, KeyError) as err:
            ok, detail = False, str(err)
        db.conn.execute("INSERT INTO applied_requests VALUES (?, ?, ?, ?)", (path.name, int(ok), detail, utc_now()))
        db.conn.commit()
        results.append({"request": path.name, "ok": ok, "detail": detail})
    return results


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def sync_requests(requests_dir: Path) -> None:
    if (requests_dir / ".git").exists():
        if git("fetch", "origin", "requests", cwd=requests_dir, check=False).returncode == 0:
            git("reset", "--hard", "origin/requests", cwd=requests_dir, check=False)


def push_state(state: Path, message: str) -> None:
    """One commit deep: amend and force-push the data branch only (never main)."""
    (state / ".gitignore").write_text("*-wal\n*-shm\n")
    git("add", "-A", cwd=state)
    has_head = git("rev-parse", "--verify", "HEAD", cwd=state, check=False).returncode == 0
    git("commit", "--amend", "-m", message, cwd=state) if has_head else git("commit", "-m", message, cwd=state)
    git("push", "--force", "origin", "HEAD:data", cwd=state)


def one_cycle(settings: dict, state: Path, requests_dir: Path, rng: random.Random | None, push: bool) -> dict:
    sync_requests(requests_dir)
    db = Database(Path(settings["paths"]["sqlite"]))
    reviews = apply_requests(requests_dir, db)
    db.close()
    results = []
    if rng is not None:
        write_import(settings, rng)
        results = scan_inbox(settings)
    db = Database(Path(settings["paths"]["sqlite"]))
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # everything into the .sqlite file before commit
    write_summary(db, settings, state / "exports")
    db.close()
    if push:
        push_state(state, f"state {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    return {"reviews": reviews, "imports": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, required=True, help=f"imports to run, one per minute (max {MAX_MINUTES})")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--requests", type=Path, default=Path("requests"))
    parser.add_argument("--no-push", action="store_true", help="for local testing")
    args = parser.parse_args()
    minutes = max(0, min(args.minutes, MAX_MINUTES))
    args.state.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="staywatch_"))
    settings = cloud_settings(args.state.resolve(), work)
    rng = random.Random()

    if minutes == 0:
        out = one_cycle(settings, args.state, args.requests, None, not args.no_push)
        print(json.dumps(out["reviews"]), flush=True)
        return
    start = time.monotonic()
    for k in range(minutes):
        out = one_cycle(settings, args.state, args.requests, rng, not args.no_push)
        for r in out["reviews"]:
            print(f"review {r['request']}: {r['detail']}", flush=True)
        for r in out["imports"]:
            print(f"[{k + 1}/{minutes}] {r['file_name']}: {r['outcome']} {r.get('status', '')} "
                  f"z={r.get('z')} tokens={r.get('tokens', 0)}", flush=True)
        if k < minutes - 1:
            time.sleep(max(0.0, start + (k + 1) * 60 - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main())
