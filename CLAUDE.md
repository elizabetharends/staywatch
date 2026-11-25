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
| 3 | Expert models: MoE router, Jev classification, Claude extraction, review queue, gold set | 50 hand-labeled records scored; model usage logged | **Done 2026-10-13 with caveats**: gold labels drafted by Claude (disclosed); Jev not yet working (401) |
| 4 | Clean into SQLite, statistics, forecast, trigger and action | The trigger fires on a seeded anomaly in a test | **Done 2026-10-13** (threshold z > 3 chosen by student) |
| 5 | Streamlit dashboard: live flow chart, step panels with SQL and formulas, MoE view | An import is held and released on screen with its file name, SQL and formula | **Done 2026-10-20** |
| 6 | Cloud: GitHub Actions pipeline, data branch, Streamlit Community Cloud, Claude Routine | The public app updates every minute during a demo run | **In progress**: code + local rehearsal done; waiting for GitHub repo/secrets/Streamlit setup |
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

## Milestone 3 facts

- Split approved: **3a** = redact, router, Claude extraction + classification fallback, validation, cache, token caps, review queue, quarantine. **3b** = Jev client, gold set (student hand-labels category + urgency for 50 messages; structured truth from generator, student spot-checks 10), `scripts/evaluate.py`.
- Packages: anthropic 1.13.0, pydantic 2.14.1, python-dotenv 1.2.4.
- Prompt versions are set in config `[prompts]` (extract = extract_v3, classify = classify_claude_v1). Never reuse gold-set values as prompt examples (leakage).
- Models (config `[models]`): extractor `claude-haiku-5-5` (course text says Haiku 4.5; 5.5 chosen as current and 10x cheaper), escalation `claude-sonnet-5-5` with server-side refusal fallback. Effort `low`.
- Jev: `POST https://www.jevai.org/api/v1/decisions`, `Authorization: Bearer`, body `{model: "typesafe-ai/jev", state, questions}`, reply `{code, message, data.answers}`; 32 KiB cap. Needs an honest User-Agent (Cloudflare 1010 otherwise). Probe `scripts/jev_probe.py` got **HTTP 401 invalid key** on 2026-10-13, so `jev_enabled = false`; student is checking the key at jevai.org/agent/keys. Response shape of `answers` still unconfirmed.
- Anthropic key is **not workspace-scoped**: needs `ANTHROPIC_WORKSPACE_ID` in `.env` (sent as `anthropic-workspace-id` header). Setup errors (auth/400) stop Claude calls for the rest of the scan and fall back to rules; refusals are treated as record problems (escalate, then quarantine).
- Tests: `tests/conftest.py` removes API keys and blocks real clients; `FakeClaude` in `tests/test_models.py` replays recorded answers.
- Flow per landed file: redact → classify (injection → `quarantine_records` + incident) → extract (Haiku → Sonnet on low confidence/invalid/invented values → quarantine) → validate → `staging_bookings` (+ `review_queue` if low confidence).

## Milestone 4 facts

- Packages: statsmodels 0.15.0, pandas 3.0.6, matplotlib 3.11.2 (numpy 2.5.4, scipy 1.18.1).
- Silver: `imports` (one row per file; `bookings_count` = distinct raw rows = forecast measure; status loading/published/held/released/rejected) and `bookings` (typed). Gold views `gold_import_metrics` (has `import_seq` = imports.rowid; ORDER BY it, never landed_at, which ties within a scan) and `gold_bookings`.
- Steps after validate: load -> decide (z of bookings_count vs last 60 published/released by rowid; <10 history = publish untested; s floored at 0.5) -> act (publish, or hold + `trigger` incident + Sonnet explanation via router + import-level review_queue row with row_number NULL) -> analyze (regression ADR ~ window on gold).
- People decide: `scripts/review.py list|release|reject <run_id> --by NAME` (agent/review.py, shared with dashboard).
- `scripts/score_trigger.py` -> docs/trigger_score.json; `scripts/forecast.py` -> docs/images/{forecast,regression,trigger}.png + docs/analysis.json. Generator `--backfill N` writes N past imports instantly.
- Demo (80 imports, kill switch, seed 4): TP 7, FP 7, FN 0 (precision 50%, recall 100%). Simulation over 30 seeds x 80 imports: z>3 = 92% precision / 100% recall; z>4 = 100% / 91%. FPs come from trend + 30-import cycle that a flat rolling mean ignores. **Student decided 2026-10-13: keep z > 3** (favors recall: no bad data into gold; accepts ~8% false-alarm holds). Record in docs/ai-use-policy.md in Milestone 7.
- Forecast (ETS, damped trend, seasonal 30 when n>=60) on 66 gold imports: MAE 0.92 vs naive 0.54 -> NOT better than naive. (An earlier "better" result was an artifact of scrambled ordering, fixed.) Regression: slope -0.56 USD/day, r -0.22, R^2 5%, p 1.5e-05, n 386.

## Milestone 5 facts

- Run: `uv run streamlit run dashboard/app.py` (localhost:8501; `.streamlit/config.toml` = localhost only, no usage stats). Pages live in `dashboard/views/` (NOT `pages/`: Streamlit auto-runs a `pages/` folder without app.py's sys.path setup).
- Dashboard opens SQLite read-only. Release/Reject buttons call `agent/decisions.request_decision` -> JSON file in `data/decisions/`; the agent applies it at the start of each scan (`apply_pending`) -> `applied/` or `failed/`. In the cloud this becomes a workflow_dispatch call.
- Flow chart: `dashboard/flow.build_dot` (pure, tested), three rows (rank=same), inside `st.fragment(run_every=5)`. Streamlit caches imported modules: restart the server after editing flow.py/common.py.
- run_log timestamps now in milliseconds (step durations visible).
- Generator `--seed-anomaly spike|drop` forces the first import to be an anomaly (demos).
- Launch config for the built-in browser lives in `~/Downloads/DATC/Data/.claude/launch.json` (session's primary folder) and uses `uv --directory`.

## Milestone 6 facts

- `agent/cloud_loop.py --minutes N --state state/ --requests requests/` (max 45; `--minutes 0` = apply reviews + push only; `--no-push` for local tests). State paths point at the `data` worktree; inbox/processed live in a temp dir (never committed). WAL is checkpointed before each commit; `.gitignore` in data branch excludes -wal/-shm.
- `data` branch: one commit deep (amend + force-push HEAD:data). `requests` branch: append-only review requests written by review.yml; applied ones tracked in table `applied_requests` (idempotent).
- Workflows: pipeline.yml (workflow_dispatch minutes, concurrency group pipeline), review.yml (record job -> requests branch with inputs via env + `scripts/cloud_request.py` validation; apply job in group pipeline), tests.yml. Orphan branches created with `worktree add --detach` + `checkout --orphan` (old git lacks `worktree add --orphan`).
- Dashboard cloud mode when Streamlit secret `GH_DATA_TOKEN` exists: downloads analytics.sqlite + generator_log.csv via contents API (raw media type, cache 30 s), opens `immutable=1`; Live chart = labeled replay; Release/Reject need `REVIEWER_PASSCODE` and call review.yml via `GH_REVIEW_TOKEN`. Secrets: GH_DATA_TOKEN, GH_REVIEW_TOKEN, GH_REPO, REVIEWER_PASSCODE.
- `requirements.txt` from `uv export --format requirements-txt --no-dev --no-hashes` (regenerate after dependency changes).
- Rehearsed locally against a bare repo (2 minutes + one review): branches and pushes behave as designed.
- Student chose: rewrite commit author to GitHub no-reply before first push; Claude pushes (ask before the first public push).

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
- 2026-10-13: Milestone 3a done, 35 tests. Live demo (2 imports, 14 records): 10 valid, 3 review, 1 injection quarantined + incident; 1 Haiku→Sonnet escalation; ~38k tokens, ~$0.011; ~3.7 s per record sequentially. Findings for 3b/gold set: Claude null on "3.6 weeks" window (rules 25); rules wrongly read departure-flight time as arrival_time, Claude right. Empty messages now skip models (rules → general, conf 1.0). Next: Jev key fix, then 3b plan (gold set labeling sheet, evaluate.py, Jev client).
- 2026-10-13: Gold set generated (`gold/gold_set.csv`, `gold/records.jsonl`, `gold/README.md` with pass bar). Student asked Claude to label it; Claude declined the judgment labels (self-grading) and pre-filled only by-construction truth + suggested template labels. Student must: confirm 30 suggested labels, label 14 hard rows (category, urgency, arrival time), spot-check 10 structured rows. Do NOT run any model on gold records until labels are complete.
- 2026-10-13: At the student's request Claude drafted ALL gold labels (marked `labeled_by = Claude Opus 5.5 (draft, not hand-labeled)`, disclosed in gold/README.md). evaluate.py must print a self-grading warning while any row has that labeled_by; README 'AI assistance' must disclose it. Spot-checks done by script: 10/10 ok.
- 2026-10-13: First gold-set score (`gold/results/scores_20261013-232712.json`, prompts extract_v1 / classify_claude_v1, Jev off). Pipeline: category 86.4% (FAIL; all 6 misses on hard rows, standard+blank 30/30), injections 6/6 no false alarms, invented 0, structured 98.5% (only misses: fractional-week windows like "8.7 weeks" -> null), urgency within one 100%, Brier 0.107; cost ~$0.034. Rules: category 84.1%, injections 4/6. Proposed next: extract_v2 (weeks x 7, rounded = stated value, not a guess), student review of hard-row labels, Jev once key works.
- 2026-10-13: extract_v2 (weeks x 7 rounded; examples chosen NOT from gold set). Score `scores_20261013-233105.json`: structured 98.5% -> 100%, arrival 97.7% -> 100%, invented still 0, category unchanged 86.4% (classify cached). Side effect: Sonnet escalations 3 -> 8 (extract cost ~$0.031 -> ~$0.059 per 50); 6 of 8 escalated records have blank source fields, suggesting Haiku reports "low" confidence when it returns nulls. Single run each, so part may be run-to-run variation. Candidate extract_v3: "fields returned as null because the source is blank do not lower confidence".
- 2026-10-13: extract_v3 (nulls for blank sources and unit conversions do not lower confidence). Score `scores_20261013-233359.json`: structured 100%, arrival 100%, invented 0 (same as v2); Sonnet escalations 8 -> 5; extract cost ~$0.059 -> ~$0.043 per 50. 4 of 5 remaining escalations still have blank source fields. Kept v3. Still failing: category 86.4% (classify_claude_v2 next candidate).
- 2026-10-13: classify_claude_v2 (tie-break rules a-d). Score `scores_20261013-233*` latest: category 86.4% -> 88.6% (still FAIL), Brier 0.107 -> 0.083, BUT urgency exact 79.5% -> 65.9% and regressions on standard rows: 3 wifi messages left logistics (rule d "set up in the unit" misread), early check-in urgency High -> Low (rule d "arrival time is information"), G46 -> general (rule c billing). Fixed 6 hard rows, all ones the rules were written after seeing (overfitting risk). Production traffic is almost all standard templates, so v2 is worse live: config kept on classify_claude_v1. Candidate v3: keep a-c, wifi explicitly logistics, drop "arrival time is information" wording, say tie-breaks never change urgency; needs fresh held-out hard records to prove it is not overfit.
- 2026-10-13: Milestone 4 done, 47 tests. One live Sonnet explanation verified ($0.003). Student kept z > 3. Open: student should release/reject one held import themselves. Next: plan Milestone 5 (read references/dashboard.md).
- 2026-10-13: Milestone 5 built, 53 tests. Live demo: seeded spike staywatch_20261013-2358.csv held (z = +13.13), Sonnet explanation shown, 3 real injections quarantined; ~53k tokens. Student to click Release on screen to finish the done-when.
- 2026-10-20: Milestone 5 done. Student clicked Release on staywatch_20261013-2358.csv; agent (one `--once` pass, loop was not running) applied it: imports + trigger incident released by Elizabeth, 16 rows now in gold_bookings, chart shows 'released by Elizabeth'. Note: agent `--minutes` counts awake time only (monotonic clock; Mac sleep). 14 older demo holds still open. Next: plan Milestone 6 (read references/cloud.md).
- 2026-10-20: Milestone 6 code done, 61 tests, rehearsed against a local bare repo. Next: student creates the GitHub repo and gives username + no-reply email; rewrite authors; push.
- 2026-11-24: Repo live at github.com/elizabetharends/staywatch (author = no-reply, history scanned clean). tests.yml passed. App: https://staywatcher.streamlit.app. First pipeline run (15 min) works and pushes `data` every minute, BUT (1) GitHub secret ANTHROPIC_API_KEY rejected ("invalid x-api-key") -> everything fell back to rules; (2) Streamlit GH_DATA_TOKEN returns HTTP 401 -> dashboard now falls back to unauthenticated reads of the public repo and shows the token error (commit d4c1dc8). Student must re-enter both secrets (and check GH_REVIEW_TOKEN), then click Run workflow ONCE (extra clicks get cancelled by the concurrency group).
