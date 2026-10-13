import csv
import json
import random
import re
from datetime import datetime, timedelta, timezone

import pytest

from agent.settings import load_settings
from generator import generate
from generator.fake_data import INJECTION_MESSAGES


@pytest.fixture
def settings(tmp_path):
    s = load_settings()
    s["paths"]["inbox"] = str(tmp_path / "inbox")
    s["paths"]["generator_log"] = str(tmp_path / "generator_log.csv")
    return s


def read_log(settings):
    with open(settings["paths"]["generator_log"], newline="") as f:
        return list(csv.DictReader(f))


def run_imports(settings, n, seed=1):
    rng, start = random.Random(seed), datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc)
    last = None
    for k in range(n):
        rows = generate.write_import(settings, rng, now=start + timedelta(minutes=k), last_file=last)
        last = generate.resolve(settings["paths"]["inbox"]) / rows[0]["file_name"]
    return read_log(settings)


def test_file_names_are_meaningful_and_unique(settings):
    log = run_imports(settings, 6)
    names = [r["file_name"] for r in log]
    assert len(names) == len(set(names))
    for r in log:
        assert re.fullmatch(r"staywatch_\d{8}-\d{4}(_resend)?(-\d+)?\.(csv|json)", r["file_name"])
    assert names[0] == "staywatch_20261013-1800.csv"


def test_alternates_csv_and_json(settings):
    log = [r for r in run_imports(settings, 4) if not r["is_resend_of"]]
    assert [r["file_format"] for r in log] == ["csv", "json", "csv", "json"]
    json_file = generate.resolve(settings["paths"]["inbox"]) / log[1]["file_name"]
    assert "records" in json.loads(json_file.read_text())


def test_normal_imports_have_5_to_10_records(settings):
    log = [r for r in run_imports(settings, 40) if not r["is_resend_of"] and r["anomaly"] == "0"]
    assert all(5 <= int(r["unique_bookings"]) <= 10 for r in log)


def test_seeded_anomalies_are_recorded(settings):
    settings["generator"]["anomaly_rate"] = 1.0
    log = run_imports(settings, generate.WARMUP_IMPORTS + 3)
    warmup, after = log[: generate.WARMUP_IMPORTS], log[generate.WARMUP_IMPORTS:]
    assert all(r["anomaly"] == "0" for r in warmup)
    for r in after:
        if r["is_resend_of"]:
            continue
        assert r["anomaly"] == "1" and r["anomaly_kind"] in {"spike", "drop"}
        n = int(r["unique_bookings"])
        assert n >= 18 if r["anomaly_kind"] == "spike" else n <= 2


def test_resend_is_byte_identical_copy(settings):
    settings["generator"]["resend_rate"] = 1.0
    log = run_imports(settings, 3)
    inbox = generate.resolve(settings["paths"]["inbox"])
    resends = [r for r in log if r["is_resend_of"]]
    assert resends
    for r in resends:
        assert (inbox / r["file_name"]).read_bytes() == (inbox / r["is_resend_of"]).read_bytes()


def test_injection_records_are_counted(settings):
    settings["generator"]["injection_rate"] = 1.0
    log = run_imports(settings, 1)
    text = (generate.resolve(settings["paths"]["inbox"]) / log[0]["file_name"]).read_text()
    assert int(log[0]["injection_records"]) == int(log[0]["records_written"])
    assert any(m in text for m in INJECTION_MESSAGES)


def test_no_real_data_patterns(settings):
    run_imports(settings, 10)
    for path in generate.resolve(settings["paths"]["inbox"]).iterdir():
        text = path.read_text()
        assert not re.search(r"\b\d{8,}\b", text)       # no real-looking long listing IDs
        emails = re.findall(r"[\w.]+@[\w.]+", text)
        assert all(e.endswith("@example.com") for e in emails)
