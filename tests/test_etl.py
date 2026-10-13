import json
import random
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from tinydb import TinyDB

from agent.db import Database
from agent.etl import scan_inbox, sha256_of
from agent.settings import load_settings
from generator import generate


@pytest.fixture
def settings(tmp_path):
    s = load_settings()
    for key in ("inbox", "processed", "quarantine"):
        s["paths"][key] = str(tmp_path / key)
    s["paths"]["generator_log"] = str(tmp_path / "generator_log.csv")
    s["paths"]["sqlite"] = str(tmp_path / "data" / "analytics.sqlite")
    s["paths"]["tinydb"] = str(tmp_path / "data" / "raw.json")
    s["agent"]["min_file_age_seconds"] = 0
    return s


@pytest.fixture
def inbox(settings):
    path = generate.resolve(settings["paths"]["inbox"])
    path.mkdir(parents=True)
    return path


def write_imports(settings, n, seed=3):
    rng, start = random.Random(seed), datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc)
    return [generate.write_import(settings, rng, now=start + timedelta(minutes=k))[0]["file_name"]
            for k in range(n)]


def query(settings, sql, params=()):
    conn = sqlite3.connect(settings["paths"]["sqlite"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def test_hash_depends_only_on_content(tmp_path):
    a, b, c = tmp_path / "a.csv", tmp_path / "renamed.csv", tmp_path / "c.csv"
    a.write_text("x,y\n1,2\n")
    b.write_text("x,y\n1,2\n")
    c.write_text("x,y\n1,3\n")
    assert sha256_of(a) == sha256_of(b) != sha256_of(c)


def test_new_files_land_in_tinydb_unchanged(settings, inbox):
    (name,) = write_imports(settings, 1)
    original = (inbox / name).read_text()
    results = scan_inbox(settings)
    assert results[0]["outcome"] == "landed"

    raw = TinyDB(settings["paths"]["tinydb"])
    docs = raw.table("raw_bookings").all()
    raw.close()
    assert len(docs) == results[0]["records"]
    assert all(d["file_name"] == name and d["run_id"] == results[0]["run_id"] for d in docs)
    assert docs[0]["record"]["booking_id"] in original      # stored as it arrived, not cleaned
    assert not (inbox / name).exists()
    assert (generate.resolve(settings["paths"]["processed"]) / name).exists()


def test_renamed_duplicate_is_skipped_and_logged(settings, inbox):
    (name,) = write_imports(settings, 1)
    scan_inbox(settings)
    processed = generate.resolve(settings["paths"]["processed"])
    shutil.copy(processed / name, inbox / "staywatch_20261013-1801_resend.csv")

    results = scan_inbox(settings)
    assert results == [{**results[0], "outcome": "duplicate", "records": 0}]
    (row,) = query(settings, "SELECT * FROM run_log WHERE file_name = ? AND step = 'detect'",
                   ("staywatch_20261013-1801_resend.csv",))
    assert row["status"] == "warning"
    assert f"duplicate of {name}" in row["message"]
    assert row["file_sha256"] == sha256_of(processed / name)
    assert not query(settings, "SELECT 1 FROM run_log WHERE file_name = ? AND step = 'land'",
                     ("staywatch_20261013-1801_resend.csv",))
    assert (processed / "duplicates" / "staywatch_20261013-1801_resend.csv").exists()

    raw = TinyDB(settings["paths"]["tinydb"])
    files = {d["file_name"] for d in raw.table("raw_bookings").all()}
    raw.close()
    assert files == {name}                                  # nothing landed twice


def test_unparseable_file_is_quarantined(settings, inbox):
    (inbox / "staywatch_20261013-1800.json").write_text("{not valid json")
    results = scan_inbox(settings)
    assert results[0]["outcome"] == "quarantined"
    assert (generate.resolve(settings["paths"]["quarantine"]) / "staywatch_20261013-1800.json").exists()
    (land,) = query(settings, "SELECT * FROM run_log WHERE step = 'land'")
    assert land["status"] == "failed"
    (incident,) = query(settings, "SELECT * FROM incident_log")
    assert incident["kind"] == "failure" and incident["status"] == "open"


def test_ignores_non_import_and_hidden_files(settings, inbox):
    (inbox / ".DS_Store").write_text("x")
    (inbox / "notes.txt").write_text("x")
    assert scan_inbox(settings) == []


def test_sql_log_records_placeholders_but_not_log_writes(settings, inbox):
    write_imports(settings, 2)
    scan_inbox(settings)
    rows = query(settings, "SELECT * FROM sql_log")
    assert rows, "the duplicate check should be logged"
    for r in rows:
        assert r["sql_text"].count("?") == len(json.loads(r["params_json"]))   # every value is a placeholder
        assert not r["sql_text"].startswith(("INSERT INTO sql_log", "INSERT INTO run_log",
                                             "UPDATE run_log"))
        assert r["run_id"] and r["file_name"]
        json.loads(r["params_json"])


def test_long_params_are_shortened_in_sql_log(tmp_path):
    db = Database(tmp_path / "t.sqlite", max_text=10)
    db.run_id, db.file_name = "run_x", "f.csv"
    db.run_sql("test", "SELECT ? AS v", ("a" * 50,))
    (params,) = db.conn.execute("SELECT params_json FROM sql_log").fetchone()
    db.close()
    assert json.loads(params) == ["a" * 10 + "…"]


def test_injection_records_are_quarantined_with_incident(settings, inbox):
    settings["generator"]["injection_rate"] = 1.0
    write_imports(settings, 1)
    (result,) = scan_inbox(settings)          # no API key in tests, so the rules expert classifies
    assert result["injections"] == result["bookings_count"]     # duplicate rows count once
    assert result["valid"] == result["review"] == 0
    assert len(query(settings, "SELECT * FROM quarantine_records")) == result["bookings_count"]
    kinds = {r["kind"] for r in query(settings, "SELECT kind FROM incident_log")}
    assert kinds == {"injection"}
    assert not query(settings, "SELECT * FROM bookings")


def test_landed_records_are_redacted_classified_and_staged(settings, inbox):
    settings["generator"]["injection_rate"] = 0.0
    write_imports(settings, 1)
    (result,) = scan_inbox(settings)
    staged = query(settings, "SELECT * FROM bookings")
    assert len(staged) == result["valid"] + result["review"] > 0
    steps = [r["step"] for r in query(settings, "SELECT step FROM run_log ORDER BY rowid")]
    assert steps == ["detect", "land", "redact", "classify", "extract", "validate", "load", "decide", "act"]
    for row in query(settings, "SELECT record_json FROM review_queue"):
        assert "@example.com" not in row["record_json"]          # no personal data in SQLite
