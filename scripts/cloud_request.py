"""Writes one validated Release/Reject request file (run by .github/workflows/review.yml).

Inputs arrive as environment variables, never pasted into shell commands, so a crafted
value cannot run code in the workflow.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

run_id = os.environ.get("RUN_ID", "")
decision = os.environ.get("DECISION", "")
person = os.environ.get("REVIEWER", "").strip()
folder = Path(sys.argv[1] if len(sys.argv) > 1 else "requests")

if not re.fullmatch(r"run_\d{8}-\d{6}_[0-9a-f]{6}", run_id):
    sys.exit(f"invalid run_id {run_id!r}")
if decision not in ("released", "rejected"):
    sys.exit(f"invalid decision {decision!r}")
if not re.fullmatch(r"[\w .'-]{1,60}", person):
    sys.exit("reviewer name must be 1-60 letters, digits, spaces or . ' -")

now = datetime.now(timezone.utc)
folder.mkdir(parents=True, exist_ok=True)
path = folder / f"{now:%Y%m%d-%H%M%S}_{run_id}.json"
path.write_text(json.dumps({"run_id": run_id, "decision": decision, "person": person,
                            "requested_at": now.isoformat(timespec="seconds")}))
print(path)
