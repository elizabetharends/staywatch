"""Decision requests: how a person's Release / Reject click reaches the agent.

The dashboard never writes to SQLite. A click drops a small JSON request into
data/decisions/; the agent applies it on its next scan (it is the only writer) and
moves the file to applied/ or failed/. In the cloud (Milestone 6) the same request
travels through a GitHub Actions workflow_dispatch call instead of a file.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent.db import Database
from agent.review import decide_import
from agent.settings import resolve


def decisions_dir(settings: dict) -> Path:
    return resolve(settings["paths"]["decisions"])


def request_decision(settings: dict, run_id: str, decision: str, person: str) -> Path:
    if decision not in ("released", "rejected"):
        raise ValueError("decision must be 'released' or 'rejected'")
    if not person.strip():
        raise ValueError("type your name: the agent never decides on its own")
    folder = decisions_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    path = folder / f"{now:%Y%m%d-%H%M%S}_{run_id}_{uuid.uuid4().hex[:4]}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"run_id": run_id, "decision": decision, "person": person.strip(),
                               "requested_at": now.isoformat(timespec="seconds")}))
    tmp.rename(path)    # atomic: the agent never sees a half-written request
    return path


def pending_requests(settings: dict) -> list[dict]:
    folder = decisions_dir(settings)
    if not folder.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]


def apply_pending(settings: dict, db: Database) -> list[dict]:
    """Applies every waiting request. Called by the agent at the start of each scan."""
    folder = decisions_dir(settings)
    if not folder.exists():
        return []
    results = []
    for path in sorted(folder.glob("*.json")):
        request = json.loads(path.read_text())
        try:
            result = decide_import(db, request["run_id"], request["decision"], request["person"])
            outcome, target = {**result, "ok": True}, folder / "applied"
        except (ValueError, KeyError) as err:
            outcome, target = {**request, "ok": False, "error": str(err)}, folder / "failed"
        target.mkdir(exist_ok=True)
        path.rename(target / path.name)
        results.append(outcome)
    return results
