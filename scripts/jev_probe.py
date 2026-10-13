"""Sends ONE invented booking record to Jev and saves the raw response, so the client and
its tests are built on the real response shape instead of a guess.

Usage: uv run python scripts/jev_probe.py
The API key is read from .env and never printed or saved.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
key = os.environ.get("JEV_API_KEY")
if not key:
    sys.exit("JEV_API_KEY is not set in .env")

body = {
    "model": "typesafe-ai/jev",
    "state": {  # invented, already-redacted test record
        "listing": "Coral Loft",
        "check_in": "2026-10-21",
        "guest_message": "Hi! We land around 11pm, is late check-in possible? Also is there parking?",
        "host_note": "",
    },
    "questions": json.loads((ROOT / "prompts" / "jev_questions_v1.json").read_text()),
}
request = urllib.request.Request(
    "https://www.jevai.org/api/v1/decisions", data=json.dumps(body).encode(), method="POST",
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
             "User-Agent": "StayWatch/0.1 (student data-agent project)"},
)
start = time.perf_counter()
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        status, text = response.status, response.read().decode()
except urllib.error.HTTPError as err:
    status, text = err.code, err.read().decode()
latency_ms = round((time.perf_counter() - start) * 1000)

out = ROOT / "tests" / "fixtures" / "jev_probe_response.json"
try:
    parsed = json.loads(text)
except json.JSONDecodeError:
    parsed = {"non_json_body": text[:2000]}
out.write_text(json.dumps({"http_status": status, "latency_ms": latency_ms, "request": body,
                           "response": parsed}, indent=2))
print(f"HTTP {status} in {latency_ms} ms; saved to {out.relative_to(ROOT)}")
