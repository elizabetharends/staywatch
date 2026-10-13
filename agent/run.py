"""Runs the StayWatch agent loop: scans inbox/ every 15 seconds.

Usage:
    uv run python -m agent.run              # run until stopped (Ctrl+C)
    uv run python -m agent.run --minutes 5  # stop after 5 minutes (demo-length)
    uv run python -m agent.run --once       # one pass, then exit (what GitHub Actions runs)

The loop scans on a timer rather than waiting for file-change events, because those
events do not reach Docker containers on Windows or macOS.
"""
import argparse
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from agent.etl import scan_inbox
from agent.settings import load_settings


def scan_and_report(settings: dict) -> None:
    for r in scan_inbox(settings):
        detail = r["outcome"]
        if r["outcome"] == "landed":
            z = "n/a" if r["z"] is None else f"{r['z']:+.2f}"
            detail = (f"{r['records']} records ({r['bookings_count']} bookings) -> {r['valid']} valid, "
                      f"{r['review']} review, {r['quarantined'] + r['injections']} quarantined; "
                      f"z = {z} -> {r['status'].upper()}; {r['tokens']} tokens")
        print(f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] {r['file_name']}: {detail} ({r['run_id']})",
              flush=True)


def main() -> None:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Run the StayWatch ETL loop.")
    parser.add_argument("--once", action="store_true", help="scan inbox/ once and exit")
    parser.add_argument("--minutes", type=float, default=None, help="stop after N minutes")
    args = parser.parse_args()

    if args.once:
        scan_and_report(settings)
        return

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(scan_and_report, "interval", seconds=settings["agent"]["scan_seconds"],
                      args=[settings], next_run_time=datetime.now(timezone.utc),
                      max_instances=1, coalesce=True)
    print(f"StayWatch agent scanning every {settings['agent']['scan_seconds']} s. Ctrl+C to stop.",
          flush=True)
    scheduler.start()
    stop_at = time.monotonic() + args.minutes * 60 if args.minutes else float("inf")
    try:
        while time.monotonic() < stop_at:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.shutdown(wait=True)  # let a scan in progress finish before exiting
        print("StayWatch agent stopped.", flush=True)


if __name__ == "__main__":
    main()
