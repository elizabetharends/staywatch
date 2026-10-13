# StayWatch — Governed Data Agent (Data Analytics with AI final project)

Built with the `analytics-final-project` skill. Read this file at the start of every session.

## Project choices

| Item | Choice |
|---|---|
| Project name | **StayWatch** (repo `staywatch`, app title "StayWatch") |
| Case study | Hobby/side business: managing short-term rental (Airbnb-style) listings. Each import is a batch of booking records: listing, guest (fake), check-in/out dates, nights, booking value, average daily rate, booking window, channel, free-text guest message / host note |
| Forecast measure | **Bookings per import** |
| Statistical methods | Correlation / regression (e.g. booking window vs. average daily rate); anomaly detection (z-score) |
| Trigger rule | **Anomaly (z-score)**: hold an import when bookings per import is more than 3 SD from the rolling mean (default chosen; student had no preference) |
| Snowflake | No (default; can be added later) |

## Data rule

Simulated data only. The real Airbnb exports in `~/Downloads/DATC/Data` are **not** used and never copied into this repo. The generator only borrows their column layout and invents every listing, guest, ID and value.

## Milestones

| # | Milestone | Done when | Status |
|---|---|---|---|
| 1 | Repo, environment, data generator (one import per minute) | A new file lands every minute and its name is logged | **Done 2026-10-13** |
| 2 | ETL loop: detect, hash, land in TinyDB, run log, SQL logging | A duplicate file is skipped and shown in the log | **Done 2026-10-13** |
| 3 | Expert models: MoE router, Jev classification, Claude extraction, review queue, gold set | 50 hand-labeled records scored; model usage logged | **Next** |
| 4 | Clean into SQLite, statistics, forecast, trigger and action | The trigger fires on a seeded anomaly in a test | Not started |
| 5 | Streamlit dashboard: live flow chart, step panels with SQL and formulas, MoE view | An import is held and released on screen with its file name, SQL and formula | Not started |
| 6 | Cloud: GitHub Actions pipeline, data branch, Streamlit Community Cloud, Claude Routine | The public app updates every minute during a demo run | Not started |
| 7 | Governance docs, prompt-injection test, 30-second Claude Motion overview, README, BINGO card | `scripts/check_bingo.py` passes on the README | Not started |

## Environment notes

- uv 0.13 installed at `~/.local/bin/uv` (run `source $HOME/.local/bin/env` or restart the shell so `uv` is on PATH). Python 3.12.15 via uv, pinned in `.python-version`. Docker not installed (needed for local Compose, optional).
- Run tests: `uv run pytest -q`. Run generator: `uv run python -m generator.generate --minutes 3` (`--interval 0 --seed 42` for instant, repeatable runs).
- Settings: `config.toml`, overridable by `STAYWATCH_<SECTION>_<KEY>` env vars; loaded by `agent/settings.py`.

## Agent facts (Milestone 2)

- Run: `uv run python -m agent.run` (loop, scans every 15 s), `--minutes N`, or `--once` (for GitHub Actions). Watch logs: `uv run python scripts/show_log.py`.
- Packages: tinydb 4.9.0, apscheduler 3.11.3 (BackgroundScheduler; stopping it from inside its own job crashed, so main thread waits and shuts it down).
- `agent/db.py`: `Database` creates all six log tables in `data/analytics.sqlite` (WAL). `run_sql(step, sql, params)` is the only SQL path and logs to `sql_log`; log-table writes go through `_write_log` and are not logged. `db.run_id` / `db.file_name` are set per file.
- `agent/etl.py`: `scan_inbox()` → per file `detect()` (SHA-256; duplicate = hash already has a `land`/`ok` row → status `warning`, message `skipped: duplicate of X`, moved to `processed/duplicates/`) → `land()` (raw records into TinyDB table `raw_bookings` in `data/raw.json` with run_id, file_name, file_sha256, row_number; file to `processed/`; parse failure → `quarantine/` + `incident_log` kind `failure`). Milestone 3 hooks in after `land()`.
- Files younger than `min_file_age_seconds` (2) are left for the next scan.

## Generator facts (needed by later milestones)

- Files: `inbox/staywatch_<YYYYMMDD-HHMM>.csv|json` (UTC), alternating CSV/JSON. Renamed byte-identical copies are named `..._resend.<ext>`.
- CSV keys are snake_case; JSON keys differ (`bookingId`, `listingName`, `checkIn`, `totalPrice`, `ADR`, `leadTime`, ...) under a top-level `records` list. Map in `generator/fake_data.py:JSON_KEY_NAMES`.
- Messiness: listing/region/channel name variants, 4 date formats, money as `1234.5`, `$1,234.50`, `1234.50 USD`, `1 234.50`; booking window as int, `N days` or `N.N weeks`; blanks as `""`, null or `N/A`; ~20% of imports repeat one row.
- Forecast measure `unique_bookings` = 6 + trend (0.03/import, capped at +2) + 1.5·sin(2π·i/30) + N(0, 0.6), clipped to 5–10.
- Seeded anomalies (after 10 warm-up imports, ~10%): spike 18–24 or drop 1–2 bookings. Ground truth in `generator_log.csv` (`anomaly`, `anomaly_kind`).
- Planted regression signal: ADR falls about 0.4% per day of booking window (capped at 90 days); weekend check-ins +12%.
- ~3% of guest messages are prompt-injection attempts (`INJECTION_MESSAGES`), counted in `injection_records`.

## Session log

- 2026-10-13: Step 1 answers collected, CLAUDE.md created.
- 2026-10-13: Milestone 1 done. 7 generator tests pass; 3-minute demo run wrote 3 files. 
- 2026-10-13: Milestone 2 done. 14 tests pass. Demo: renamed copy `staywatch_20261013-2243_resend.csv` skipped as duplicate of `staywatch_20261013-2243.csv`; live loop picked up files within 15 s. Next: plan Milestone 3 (read `references/models-moe.md`; student needs class Console API key in `.env`).
