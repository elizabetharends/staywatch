import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from agent import cloud_loop
from agent.db import Database
from generator import generate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cloud(tmp_path):
    state, work, requests = tmp_path / "state", tmp_path / "work", tmp_path / "requests"
    state.mkdir()
    settings = cloud_loop.cloud_settings(state, work)
    settings["generator"].update(resend_rate=0.0, injection_rate=0.0, anomaly_rate=0.0)
    return settings, state, requests


def test_state_paths_live_on_the_data_branch_worktree(cloud):
    settings, state, _ = cloud
    for key in ("sqlite", "tinydb", "generator_log"):
        assert Path(settings["paths"][key]).parent == state
    assert not Path(settings["paths"]["inbox"]).is_relative_to(state)     # work files are never committed
    assert settings["agent"]["min_file_age_seconds"] == 0


def test_one_cycle_writes_state_and_summary_without_pushing(cloud):
    settings, state, requests = cloud
    rng = random.Random(1)
    for _ in range(3):
        out = cloud_loop.one_cycle(settings, state, requests, rng, push=False)
        assert out["imports"][0]["status"] == "published"
    assert (state / "analytics.sqlite").exists() and (state / "raw.json").exists()
    assert not (state / "analytics.sqlite-wal").exists() or (state / "analytics.sqlite-wal").stat().st_size == 0
    summary = json.loads((state / "exports" / "daily_summary.json").read_text())
    assert summary["imports_by_status"][0]["n"] == 3 and summary["kill_switch"] is False


def held_run(settings, state, requests):
    rng = random.Random(2)
    for _ in range(12):
        cloud_loop.one_cycle(settings, state, requests, rng, push=False)
    generate.write_import(settings, rng, force_anomaly="spike")
    from agent.etl import scan_inbox
    (held,) = scan_inbox(settings)
    assert held["status"] == "held"
    return held


def test_review_requests_apply_once(cloud):
    settings, state, requests = cloud
    held = held_run(settings, state, requests)
    requests.mkdir()
    (requests / "20261020-120000_req.json").write_text(json.dumps(
        {"run_id": held["run_id"], "decision": "released", "person": "EA"}))
    first = cloud_loop.one_cycle(settings, state, requests, None, push=False)
    assert first["reviews"] == [{"request": "20261020-120000_req.json", "ok": True, "detail": "released by EA"}]
    again = cloud_loop.one_cycle(settings, state, requests, None, push=False)
    assert again["reviews"] == []                                         # idempotent
    db = Database(state / "analytics.sqlite")
    assert tuple(db.conn.execute("SELECT status, decided_by FROM imports WHERE run_id = ?",
                                 (held["run_id"],)).fetchone()) == ("released", "EA")
    db.close()


@pytest.mark.parametrize("env,ok", [
    ({"RUN_ID": "run_20261013-235821_4d24ee", "DECISION": "released", "REVIEWER": "Elizabeth"}, True),
    ({"RUN_ID": "run_x; rm -rf /", "DECISION": "released", "REVIEWER": "E"}, False),
    ({"RUN_ID": "run_20261013-235821_4d24ee", "DECISION": "delete", "REVIEWER": "E"}, False),
    ({"RUN_ID": "run_20261013-235821_4d24ee", "DECISION": "rejected", "REVIEWER": "$(whoami)"}, False),
])
def test_cloud_request_validates_workflow_inputs(tmp_path, env, ok):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "cloud_request.py"), str(tmp_path)],
                            env={**os.environ, **env}, capture_output=True, text=True)
    assert (result.returncode == 0) is ok
    assert len(list(tmp_path.glob("*.json"))) == (1 if ok else 0)


def test_workflows_pass_inputs_through_env_not_shell():
    review = (ROOT / ".github" / "workflows" / "review.yml").read_text()
    for line in review.splitlines():
        if "${{ inputs." in line:
            assert line.strip().split(":")[0] in {"RUN_ID", "DECISION", "REVIEWER"}, line
    assert "group: pipeline" in (ROOT / ".github" / "workflows" / "pipeline.yml").read_text()
