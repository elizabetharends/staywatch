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
from agent.decide import act, analyze, decide, load
from agent.decisions import apply_pending
from agent.experts import ModelRunner
from agent.redact import redact
from agent.reference import canonical_keys
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


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def record_hash(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def process(records: list[dict], sha: str, db: Database, runner: ModelRunner, settings: dict) -> dict:
    """Everything after landing: redact, classify, extract, validate, load, decide, act, analyze."""
    runner.start_file()
    landed_at = utc_now()
    received = datetime.now(timezone.utc).date()
    review_below = settings["models"]["review_confidence"]

    # Exact duplicate rows inside one file are one booking, counted once.
    unique, seen = [], set()
    for row_number, raw_record in enumerate(records, start=1):
        h = record_hash(raw_record)
        if h not in seen:
            seen.add(h)
            unique.append((row_number, h, raw_record))
    bookings_count = len(unique)

    log_id = db.start_step("redact", sha256=sha, rows_in=len(records))
    redacted = [(row_number, h, *redact(canonical_keys(r))) for row_number, h, r in unique]
    db.finish_step(log_id, "ok", f"{sum(r[3] for r in redacted)} personal values masked before any model call; "
                                 f"{len(records) - len(unique)} exact duplicate row(s) dropped",
                   rows_out=len(redacted), rows_rejected=len(records) - len(unique))

    log_id = db.start_step("classify", sha256=sha, rows_in=len(redacted))
    classified, injections, experts = [], 0, {}
    for row_number, h, record, pii in redacted:
        result = runner.classify(record, pii)
        experts[result["expert"]] = experts.get(result["expert"], 0) + 1
        if result["injection"]:
            injections += 1
            db.run_sql("classify", "INSERT INTO quarantine_records VALUES (?, ?, ?, ?, ?, ?)",
                       (db.run_id, db.file_name, row_number, "injection attempt", _json(record), utc_now()))
            db.open_incident(f"inc_{uuid.uuid4().hex[:8]}", "injection",
                             f"{db.file_name} row {row_number}: record tries to give instructions; quarantined")
            continue
        classified.append((row_number, h, record, pii, result))
    db.finish_step(log_id, "warning" if injections else "ok",
                   f"experts {experts}; {injections} injection attempt(s) quarantined",
                   rows_out=len(classified), rows_rejected=injections)

    log_id = db.start_step("extract", sha256=sha, rows_in=len(classified))
    outcomes = {"valid": 0, "review": 0, "quarantined": 0}
    by_expert, booking_rows = {}, []
    for row_number, h, record, pii, result in classified:
        fields, expert, status = runner.extract(record, received, pii)
        by_expert[expert] = by_expert.get(expert, 0) + 1
        if status.startswith("quarantined"):
            outcomes["quarantined"] += 1
            db.run_sql("validate", "INSERT INTO quarantine_records VALUES (?, ?, ?, ?, ?, ?)",
                       (db.run_id, db.file_name, row_number, status, _json(record), utc_now()))
            continue
        reasons = []
        if status == "review":
            reasons.append("extraction confidence low after escalation")
        if result["confidence"] < review_below:
            reasons.append(f"category confidence {result['confidence']:.2f} < {review_below}")
        status = "review" if reasons else "valid"
        outcomes[status] += 1
        booking_rows.append({
            "run_id": db.run_id, "file_name": db.file_name, "row_number": row_number, "record_hash": h,
            **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in fields.items()
               if k != "confidence"},
            "category": result["category"], "urgency": result["urgency"],
            "category_confidence": result["confidence"], "classify_expert": result["expert"],
            "extract_expert": expert, "status": status, "loaded_at": utc_now(),
        })
        if reasons:
            db.run_sql("validate", "INSERT INTO review_queue VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                       (db.run_id, db.file_name, row_number, "; ".join(reasons), _json(record),
                        _json({"classification": result, "fields": fields}), utc_now()))
    db.finish_step(log_id, "ok", f"experts {by_expert}", rows_out=len(classified) - outcomes["quarantined"],
                   rows_rejected=outcomes["quarantined"])

    log_id = db.start_step("validate", sha256=sha, rows_in=len(classified))
    db.finish_step(log_id, "warning" if outcomes["quarantined"] or outcomes["review"] else "ok",
                   f"{outcomes['valid']} valid, {outcomes['review']} to review queue, "
                   f"{outcomes['quarantined']} quarantined (Pydantic + no invented values)",
                   rows_out=outcomes["valid"] + outcomes["review"], rows_rejected=outcomes["quarantined"])

    counts = {**outcomes, "injections": injections}
    load(db, sha, landed_at, bookings_count, booking_rows, counts)
    decision = decide(db, settings, bookings_count)
    status = act(db, runner, decision)
    if status == "published":
        analyze(db)
    return {**counts, "bookings_count": bookings_count, "z": decision["result"], "status": status,
            "tokens": runner.run_tokens}


def ready_files(inbox: Path, min_age_seconds: float) -> list[Path]:
    """Import files in name order, skipping any still being written."""
    if not inbox.exists():
        return []
    now = time.time()
    ready = [p for p in inbox.iterdir()
             if p.suffix in IMPORT_SUFFIXES and not p.name.startswith(".")
             and now - p.stat().st_mtime >= min_age_seconds]
    # Oldest first, so the time series stays in order (name sort would put "-10" before "-2")
    return sorted(ready, key=lambda p: (p.stat().st_mtime_ns, p.name))


def scan_inbox(settings: dict) -> list[dict]:
    """Processes every ready file in inbox/. Returns one summary per file."""
    agent = settings["agent"]
    db = Database(resolve(settings["paths"]["sqlite"]), agent["sql_log_max_text"])
    raw = TinyDB(resolve(settings["paths"]["tinydb"]), indent=1, ensure_ascii=False)
    runner = ModelRunner(db, settings)
    results = []
    try:
        for d in apply_pending(settings, db):      # people's Release / Reject clicks first
            results.append({"run_id": d["run_id"], "file_name": d.get("file_name", ""),
                            "outcome": f"decision {d.get('status', 'failed')} by {d.get('decided_by', d.get('person'))}"
                                       + ("" if d["ok"] else f" (failed: {d['error']})"), "records": 0})
        for path in ready_files(resolve(settings["paths"]["inbox"]), agent["min_file_age_seconds"]):
            db.run_id, db.file_name = new_run_id(), path.name
            sha = detect(path, db, settings)
            records = land(path, sha, db, raw, settings) if sha else None
            outcome = "duplicate" if sha is None else ("landed" if records is not None else "quarantined")
            summary = process(records, sha, db, runner, settings) if records else {}
            results.append({"run_id": db.run_id, "file_name": path.name, "outcome": outcome,
                            "records": len(records or []), **summary})
    finally:
        raw.close()
        db.close()
    return results
