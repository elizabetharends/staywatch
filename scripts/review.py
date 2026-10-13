"""A person decides on held imports. The agent may hold data, but never releases or deletes it.

Usage:
    uv run python scripts/review.py list
    uv run python scripts/review.py release <run_id> --by "Your Name"
    uv run python scripts/review.py reject  <run_id> --by "Your Name"
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.db import Database  # noqa: E402
from agent.review import decide_import, held_imports  # noqa: E402
from agent.settings import load_settings, resolve  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["list", "release", "reject"])
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--by", default="", help="who is deciding (required for release/reject)")
    args = parser.parse_args()
    db = Database(resolve(load_settings()["paths"]["sqlite"]))
    try:
        if args.action == "list":
            held = held_imports(db)
            if not held:
                print("No imports are waiting for a decision.")
            for h in held:
                detail = json.loads(h["result_json"])
                print(f"{h['run_id']}  {h['file_name']}  {h['bookings_count']} bookings  {h['reason']}")
                print(f"    {detail['interpretation']}")
                print(f"    Explanation ({detail['explained_by']}): {detail['explanation']}\n")
            return
        if not args.run_id:
            sys.exit("Give the run_id of the held import.")
        result = decide_import(db, args.run_id, "released" if args.action == "release" else "rejected", args.by)
        print(f"{result['file_name']} {result['status']} by {result['decided_by']}.")
    except ValueError as err:
        sys.exit(str(err))
    finally:
        db.close()


if __name__ == "__main__":
    main()
