"""One pass of the ETL loop over inbox/: detect, then land.

Later milestones add redact, route, classify, extract, validate, load, decide and act
after land_file().
"""
import csv
import hashlib
import io
import json
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tinydb import TinyDB

from agent.db import Database, utc_now
from agent.settings import resolve

IMPORT_SUFFIXES = {".csv", ".json"}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_run_id() -> str:
    return f"run_{datetime.now(timezone.utc):%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:6]}"


def move_to(path: Path, folder: Path) -> Path:
    """Moves a file into folder without overwriting anything already there."""
    folder.mkdir(parents=True, exist_ok=True)
    target, n = folder / path.name, 2
    while target.exists():
        target, n = folder / f"{path.stem}-{n}{path.suffix}", n + 1
    shutil.move(path, target)
    return target


def parse_records(path: Path) -> list[dict]:
    """Reads records exactly as they arrived. CSV values stay strings; JSON keeps its own keys."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".csv":
        records = list(csv.DictReader(io.StringIO(text)))
    else:
        data = json.loads(text)
        records = data["records"] if isinstance(data, dict) else data
    if not records or not all(isinstance(r, dict) for r in records):
        raise ValueError("no records found")
    return records


def detect(path: Path, db: Database, settings: dict) -> str | None:
    """Logs name, size and hash. Returns the hash, or None if this content was already landed."""
    sha = sha256_of(path)
    log_id = db.start_step("detect", sha256=sha)
    seen = db.run_sql(
        "detect",
        "SELECT file_name FROM run_log WHERE file_sha256 = ? AND step = 'land' AND status = 'ok' LIMIT 1",
        (sha,),
    )
    size = path.stat().st_size
    if seen:
        original = seen[0]["file_name"]
        moved = move_to(path, resolve(settings["paths"]["processed"]) / "duplicates")
        db.finish_step(log_id, "warning",
                       f"skipped: duplicate of {original} (same SHA-256); {size} bytes; "
                       f"moved to {moved.parent.name}/{moved.name}")
        return None
    db.finish_step(log_id, "ok", f"new file, {size} bytes, sha256 {sha[:12]}…")
    return sha


def land(path: Path, sha: str, db: Database, raw: TinyDB, settings: dict) -> list[dict] | None:
    """Stores every record as-is in TinyDB, then moves the file to processed/ or quarantine/."""
    log_id = db.start_step("land", sha256=sha)
    try:
        records = parse_records(path)
    except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError, csv.Error) as err:
        moved = move_to(path, resolve(settings["paths"]["quarantine"]))
        db.open_incident(f"inc_{uuid.uuid4().hex[:8]}", "failure",
                         f"{path.name} could not be parsed ({err}); moved to quarantine/")
        db.finish_step(log_id, "failed", f"could not parse: {err}; moved to quarantine/{moved.name}",
                       rows_out=0)
        return None

    landed_at = utc_now()
    raw.table("raw_bookings").insert_multiple(
        {"run_id": db.run_id, "file_name": path.name, "file_sha256": sha,
         "row_number": i, "landed_at": landed_at, "record": record}
        for i, record in enumerate(records, start=1)
    )
    moved = move_to(path, resolve(settings["paths"]["processed"]))
    db.finish_step(log_id, "ok",
                   f"{len(records)} raw records stored in TinyDB table raw_bookings; "
                   f"moved to processed/{moved.name}",
                   rows_in=len(records), rows_out=len(records))
    return records


def ready_files(inbox: Path, min_age_seconds: float) -> list[Path]:
    """Import files in name order, skipping any still being written."""
    if not inbox.exists():
        return []
    now = time.time()
    return sorted(p for p in inbox.iterdir()
                  if p.suffix in IMPORT_SUFFIXES and not p.name.startswith(".")
                  and now - p.stat().st_mtime >= min_age_seconds)


def scan_inbox(settings: dict) -> list[dict]:
    """Processes every ready file in inbox/. Returns one summary per file."""
    agent = settings["agent"]
    db = Database(resolve(settings["paths"]["sqlite"]), agent["sql_log_max_text"])
    raw = TinyDB(resolve(settings["paths"]["tinydb"]), indent=1, ensure_ascii=False)
    results = []
    try:
        for path in ready_files(resolve(settings["paths"]["inbox"]), agent["min_file_age_seconds"]):
            db.run_id, db.file_name = new_run_id(), path.name
            sha = detect(path, db, settings)
            records = land(path, sha, db, raw, settings) if sha else None
            outcome = "duplicate" if sha is None else ("landed" if records is not None else "quarantined")
            results.append({"run_id": db.run_id, "file_name": path.name, "outcome": outcome,
                            "records": len(records or [])})
    finally:
        raw.close()
        db.close()
    return results
