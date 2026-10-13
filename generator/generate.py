"""Writes one fake booking import into inbox/ every minute.

Usage:
    uv run python -m generator.generate --minutes 3
    uv run python -m generator.generate --minutes 1 --interval 0 --seed 42   # instant, repeatable
    uv run python -m generator.generate --backfill 60 --seed 42   # 60 past imports, one minute apart, instantly

Each import is logged to generator_log.csv, including whether it is a seeded anomaly,
so the agent's trigger can be scored against the truth later.
"""
import argparse
import csv
import json
import math
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.settings import load_settings, resolve
from generator.fake_data import INJECTION_MESSAGES, make_booking, to_json_keys

PROJECT = "staywatch"
LOG_FIELDS = [
    "import_index", "file_name", "written_at_utc", "file_format", "records_written",
    "unique_bookings", "expected_bookings", "anomaly", "anomaly_kind",
    "duplicate_rows", "injection_records", "is_resend_of",
]
WARMUP_IMPORTS = 10  # no anomalies until there is some history to compare against


def expected_bookings(index: int, rng: random.Random, settings: dict) -> tuple[float, int]:
    """Forecast measure = trend + repeating cycle + noise, kept in the normal 5-10 range."""
    gen = settings["generator"]
    trend = min(0.03 * index, 2.0)
    cycle = 1.5 * math.sin(2 * math.pi * index / 30)  # a 30-import "day"
    expected = 6.0 + trend + cycle
    count = round(expected + rng.gauss(0, 0.6))
    return expected, max(gen["records_min"], min(gen["records_max"], count))


def next_import_index(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    with open(log_path, newline="") as f:
        return sum(1 for row in csv.DictReader(f) if not row["is_resend_of"])


def unique_path(inbox: Path, stem: str, ext: str) -> Path:
    path, n = inbox / f"{stem}.{ext}", 2
    while path.exists():
        path, n = inbox / f"{stem}-{n}.{ext}", n + 1
    return path


def write_records(path: Path, records: list[dict], fmt: str, written_at: datetime) -> None:
    if fmt == "csv":
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows([{k: ("" if v is None else v) for k, v in r.items()} for r in records])
    else:
        export = {"export": "StayWatch channel manager", "generated_at": written_at.isoformat(),
                  "records": [to_json_keys(r) for r in records]}
        path.write_text(json.dumps(export, indent=2, ensure_ascii=False))


def append_log(log_path: Path, row: dict) -> None:
    is_new = not log_path.exists()
    with open(log_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def write_import(settings: dict, rng: random.Random, now: datetime | None = None,
                 last_file: Path | None = None) -> list[dict]:
    """Writes one import (plus, sometimes, a renamed copy of an earlier file). Returns log rows."""
    gen = settings["generator"]
    inbox = resolve(settings["paths"]["inbox"])
    log_path = resolve(settings["paths"]["generator_log"])
    inbox.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)

    index = next_import_index(log_path)
    fmt = "csv" if index % 2 == 0 else "json"
    expected, count = expected_bookings(index, rng, settings)

    anomaly_kind = ""
    if index >= WARMUP_IMPORTS and rng.random() < gen["anomaly_rate"]:
        anomaly_kind = rng.choice(["spike", "drop"])
        count = rng.randint(18, 24) if anomaly_kind == "spike" else rng.randint(1, 2)

    records = [make_booking(now.date(), rng, injection=rng.random() < gen["injection_rate"])
               for _ in range(count)]
    duplicates = 0
    if rng.random() < gen["duplicate_row_rate"]:
        records.insert(rng.randrange(len(records) + 1), dict(rng.choice(records)))
        duplicates = 1

    path = unique_path(inbox, f"{PROJECT}_{now:%Y%m%d-%H%M}", fmt)
    write_records(path, records, fmt, now)
    row = {
        "import_index": index, "file_name": path.name, "written_at_utc": now.isoformat(),
        "file_format": fmt, "records_written": len(records), "unique_bookings": count,
        "expected_bookings": round(expected, 2), "anomaly": int(bool(anomaly_kind)),
        "anomaly_kind": anomaly_kind, "duplicate_rows": duplicates,
        "injection_records": sum(r["guest_message"] in INJECTION_MESSAGES for r in records),
        "is_resend_of": "",
    }
    append_log(log_path, row)
    rows = [row]

    # Now and then the channel manager re-sends an earlier file under a new name.
    # Same bytes, so the agent should detect it by SHA-256 and skip it.
    if last_file and last_file.exists() and rng.random() < gen["resend_rate"]:
        copy = unique_path(inbox, f"{PROJECT}_{now:%Y%m%d-%H%M}_resend", last_file.suffix[1:])
        copy.write_bytes(last_file.read_bytes())
        resend = {**{k: "" for k in LOG_FIELDS}, "file_name": copy.name,
                  "written_at_utc": now.isoformat(), "file_format": last_file.suffix[1:],
                  "is_resend_of": last_file.name, "anomaly": 0}
        append_log(log_path, resend)
        rows.append(resend)
    return rows


def run(minutes: int, interval: float, seed: int | None, settings: dict, backfill: bool = False) -> None:
    rng = random.Random(seed)
    start, last_file = time.monotonic(), None
    inbox = resolve(settings["paths"]["inbox"])
    first = datetime.now(timezone.utc) - timedelta(minutes=minutes - 1)
    for k in range(minutes):
        when = first + timedelta(minutes=k) if backfill else None
        for row in write_import(settings, rng, now=when, last_file=last_file):
            tag = f" (seeded {row['anomaly_kind']})" if row["anomaly_kind"] else ""
            tag = f" (renamed copy of {row['is_resend_of']})" if row["is_resend_of"] else tag
            print(f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] wrote {row['file_name']}"
                  f" ({row['records_written'] or '-'} records){tag}", flush=True)
            if not row["is_resend_of"]:
                last_file = inbox / row["file_name"]
        if k < minutes - 1 and not backfill:
            time.sleep(max(0.0, start + (k + 1) * interval - time.monotonic()))


def main() -> None:
    settings = load_settings()
    gen = settings["generator"]
    parser = argparse.ArgumentParser(description="Write fake StayWatch booking imports.")
    parser.add_argument("--minutes", type=int, default=gen["demo_minutes"],
                        help="number of imports to write, one per interval (keeps runs demo-length)")
    parser.add_argument("--interval", type=float, default=gen["interval_seconds"],
                        help="seconds between imports (default 60)")
    parser.add_argument("--seed", type=int, default=None, help="random seed for repeatable runs")
    parser.add_argument("--backfill", type=int, default=None, metavar="N",
                        help="write N past imports instantly, timestamped one minute apart up to now")
    args = parser.parse_args()
    if args.backfill:
        run(args.backfill, 0, args.seed, settings, backfill=True)
    else:
        run(args.minutes, args.interval, args.seed, settings)


if __name__ == "__main__":
    main()
