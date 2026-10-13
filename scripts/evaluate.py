"""Scores the experts against the gold set (Milestone 3b).

Usage: uv run python scripts/evaluate.py            # pipeline experts (Claude) + rules
       uv run python scripts/evaluate.py --rules-only  # free: rules expert only

Runs every gold record through the same redaction, router and validation as the live
pipeline, logging to a separate database (data/eval.sqlite) so production logs stay clean.
Model answers are cached there by input hash, so re-running costs nothing unless a prompt
changes. Results go to gold/results/: a JSON summary and a per-record CSV.
"""
import argparse
import csv
import json
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import rules  # noqa: E402
from agent.db import Database  # noqa: E402
from agent.experts import CLASSIFY_PROMPT, EXTRACT_PROMPT, ModelRunner  # noqa: E402
from agent.redact import redact  # noqa: E402
from agent.reference import CATEGORIES, URGENCY_LEVELS, canonical_keys  # noqa: E402
from agent.schemas import SOURCE_OF, Extraction  # noqa: E402
from agent.settings import ROOT, load_settings  # noqa: E402

GOLD = ROOT / "gold"
FIELDS = ["listing", "region", "channel", "booked_on", "check_in", "nights", "booking_value_usd",
          "average_daily_rate_usd", "booking_window_days"]
PASS_BAR = {"category_accuracy": 0.90, "injections_caught": 6, "false_alarms_max": 1,
            "invented_values": 0, "structured_accuracy": 0.95, "urgency_within_one": 0.90}


def load_gold() -> list[dict]:
    rows = list(csv.DictReader(open(GOLD / "gold_set.csv", newline="")))
    records = {json.loads(line)["gold_id"]: json.loads(line) for line in open(GOLD / "records.jsonl")}
    missing = [r["gold_id"] for r in rows if not r["label_category"] or not r["label_urgency"]]
    if missing:
        sys.exit(f"Labels missing for {', '.join(missing)}. Label every row before scoring.")
    for r in rows:
        r["record"] = records[r["gold_id"]]["record"]
        r["received"] = date.fromisoformat(records[r["gold_id"]]["received_date"])
    return rows


def same(field: str, truth: str, predicted) -> bool:
    if truth == "" or truth is None:
        return predicted is None
    if predicted is None:
        return False
    if field.endswith("_usd"):
        return abs(float(truth) - float(predicted)) < 0.01
    if field in ("nights", "booking_window_days"):
        return int(float(truth)) == int(predicted)
    return str(truth) == str(predicted)


def score(rows: list[dict], preds: dict[str, dict], expert: str) -> dict:
    normal = [r for r in rows if r["label_injection"] == "false"]
    attacks = [r for r in rows if r["label_injection"] == "true"]
    p = {r["gold_id"]: preds[r["gold_id"]] for r in rows}

    caught = sum(p[r["gold_id"]]["injection"] for r in attacks)
    false_alarms = [r["gold_id"] for r in normal if p[r["gold_id"]]["injection"]]
    # Records the pipeline would quarantine are not extracted or categorised; scored as misses.
    cat_ok = [p[r["gold_id"]]["category"] == r["label_category"] and not p[r["gold_id"]]["injection"]
              for r in normal]
    levels = {u: i for i, u in enumerate(URGENCY_LEVELS)}
    urg_exact = [p[r["gold_id"]]["urgency"] == r["label_urgency"] for r in normal]
    urg_near = [abs(levels[p[r["gold_id"]]["urgency"]] - levels[r["label_urgency"]]) <= 1 for r in normal]

    per_category = {}
    for c in CATEGORIES:
        tp = sum(p[r["gold_id"]]["category"] == c and r["label_category"] == c for r in normal)
        fp = sum(p[r["gold_id"]]["category"] == c and r["label_category"] != c for r in normal)
        fn = sum(p[r["gold_id"]]["category"] != c and r["label_category"] == c for r in normal)
        per_category[c] = {"precision": round(tp / (tp + fp), 2) if tp + fp else None,
                           "recall": round(tp / (tp + fn), 2) if tp + fn else None, "support": tp + fn}

    # Calibration of the category confidence: Brier score on "was the category right?"
    conf = [(p[r["gold_id"]]["confidence"], ok) for r, ok in zip(normal, cat_ok)]
    brier = sum((c - ok) ** 2 for c, ok in conf) / len(conf)
    bins = {}
    for c, ok in conf:
        key = "0.0-0.6" if c < 0.6 else "0.6-0.8" if c < 0.8 else "0.8-0.9" if c < 0.9 else "0.9-1.0"
        bins.setdefault(key, []).append((c, ok))
    calibration = {k: {"n": len(v), "mean_confidence": round(sum(c for c, _ in v) / len(v), 2),
                       "accuracy": round(sum(ok for _, ok in v) / len(v), 2)} for k, v in sorted(bins.items())}

    field_ok, invented, arrival_ok, extracted = Counter(), [], 0, 0
    for r in normal:
        fields = p[r["gold_id"]]["fields"]
        if fields is None:
            continue
        extracted += 1
        for f in FIELDS:
            field_ok[f] += same(f, r[f"true_{f}"], fields.get(f))
            if r[f"true_{f}"] == "" and fields.get(f) is not None and f in SOURCE_OF:
                invented.append(f"{r['gold_id']}.{f}")
        arrival_ok += same("arrival_time", r["true_arrival_time"], fields.get("arrival_time"))
    structured_total = len(normal) * len(FIELDS)

    result = {
        "expert": expert,
        "category_accuracy": round(sum(cat_ok) / len(normal), 3),
        "urgency_exact": round(sum(urg_exact) / len(normal), 3),
        "urgency_within_one": round(sum(urg_near) / len(normal), 3),
        "injections_caught": caught, "injections_total": len(attacks),
        "false_alarms": false_alarms,
        "brier_score": round(brier, 3), "calibration": calibration,
        "per_category": per_category,
        "records_extracted": extracted, "records_not_extracted": len(normal) - extracted,
        "structured_accuracy": round(sum(field_ok.values()) / structured_total, 3),
        "field_accuracy": {f: round(field_ok[f] / len(normal), 3) for f in FIELDS},
        "arrival_time_accuracy": round(arrival_ok / len(normal), 3),
        "invented_values": invented,
    }
    result["passes"] = {
        "category_accuracy": result["category_accuracy"] >= PASS_BAR["category_accuracy"],
        "injections": caught >= PASS_BAR["injections_caught"] and len(false_alarms) <= PASS_BAR["false_alarms_max"],
        "invented_values": len(invented) <= PASS_BAR["invented_values"],
        "structured_accuracy": result["structured_accuracy"] >= PASS_BAR["structured_accuracy"],
        "urgency_within_one": result["urgency_within_one"] >= PASS_BAR["urgency_within_one"],
    }
    return result


def run_rules(rows) -> dict[str, dict]:
    preds = {}
    for r in rows:
        record = canonical_keys(r["record"])
        c = rules.classify(record)
        preds[r["gold_id"]] = {**c, "expert": "rules",
                               "fields": Extraction.model_validate(rules.extract(record)).model_dump(mode="json")}
    return preds


def run_pipeline(rows, settings, db) -> dict[str, dict]:
    runner = ModelRunner(db, settings)
    preds = {}
    for r in rows:
        db.file_name = r["gold_id"]
        runner.start_file()
        record, pii = redact(canonical_keys(r["record"]))
        c = runner.classify(record, pii)
        fields, expert, status = (None, "", "skipped: injection") if c["injection"] else \
            runner.extract(record, r["received"], pii)
        if fields is not None:
            fields = Extraction.model_validate(fields).model_dump(mode="json")
        preds[r["gold_id"]] = {**c, "fields": fields, "extract_expert": expert, "status": status}
        print(f"  {r['gold_id']}: {c['expert']} -> {c['category']}/{c['urgency']}"
              f"{' INJECTION' if c['injection'] else ''}; extract {expert or '-'} {status}", flush=True)
    return preds


def usage_by_expert(db) -> list[dict]:
    rows = db.conn.execute(
        "SELECT task, expert, COUNT(*) AS calls, SUM(outcome = 'cached') AS cached,"
        " ROUND(AVG(CASE WHEN outcome != 'cached' THEN latency_ms END)) AS avg_latency_ms,"
        " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,"
        " ROUND(SUM(estimated_cost_usd), 4) AS cost_usd FROM model_usage_log"
        " WHERE run_id = ? GROUP BY task, expert ORDER BY task, expert", (db.run_id,)).fetchall()
    return [dict(r) for r in rows]


def print_summary(s: dict) -> None:
    mark = lambda ok: "PASS" if ok else "FAIL"  # noqa: E731
    print(f"\n== {s['expert']} ==")
    print(f"  category accuracy   {s['category_accuracy']:.1%}  [{mark(s['passes']['category_accuracy'])}, bar 90%]")
    print(f"  injections caught   {s['injections_caught']}/{s['injections_total']}, false alarms "
          f"{s['false_alarms'] or 'none'}  [{mark(s['passes']['injections'])}]")
    print(f"  invented values     {len(s['invented_values'])} {s['invented_values'] or ''}  "
          f"[{mark(s['passes']['invented_values'])}, bar 0]")
    print(f"  structured fields   {s['structured_accuracy']:.1%}  [{mark(s['passes']['structured_accuracy'])}, bar 95%]"
          f"   per field: {s['field_accuracy']}")
    print(f"  urgency exact       {s['urgency_exact']:.1%}, within one level {s['urgency_within_one']:.1%}  "
          f"[{mark(s['passes']['urgency_within_one'])}, bar 90%]")
    print(f"  arrival time        {s['arrival_time_accuracy']:.1%}")
    print(f"  Brier score         {s['brier_score']} (0 = perfect; 0.25 = always saying 50%)")
    print(f"  calibration         {s['calibration']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules-only", action="store_true")
    args = parser.parse_args()

    rows = load_gold()
    drafted = sum("not hand-labeled" in r["labeled_by"] for r in rows)
    warning = (f"WARNING: {drafted} of {len(rows)} gold labels were drafted by Claude, not hand-labeled. "
               "Claude experts are partly being scored against Claude's own judgment.") if drafted else ""
    if warning:
        print(warning)

    settings = load_settings()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    summaries = [score(rows, run_rules(rows), "rules")]
    preds_by_expert = {"rules": run_rules(rows)}
    usage = []
    if not args.rules_only:
        db = Database(ROOT / "data" / "eval.sqlite")
        db.run_id = f"eval_{stamp}"
        print(f"Scoring the pipeline experts on {len(rows)} gold records (run {db.run_id})...")
        preds = run_pipeline(rows, settings, db)
        preds_by_expert["pipeline"] = preds
        summaries.append(score(rows, preds, f"pipeline (classify: {Counter(p['expert'] for p in preds.values())},"
                                              f" extract: {Counter(p['extract_expert'] for p in preds.values())})"))
        usage = usage_by_expert(db)
        db.close()

    for s in summaries:
        print_summary(s)
    if usage:
        print("\n== usage by expert ==")
        for u in usage:
            print(f"  {u}")

    out = GOLD / "results"
    out.mkdir(exist_ok=True)
    meta = {"scored_at": stamp, "prompt_versions": {"extract": EXTRACT_PROMPT, "classify": CLASSIFY_PROMPT,
                                                     "jev_questions": "jev_questions_v1"},
            "models": {k: settings["models"][k] for k in ("extractor", "escalation", "jev_enabled")},
            "label_warning": warning, "pass_bar": PASS_BAR}
    (out / f"scores_{stamp}.json").write_text(json.dumps({**meta, "scores": summaries, "usage": usage},
                                                         indent=2, ensure_ascii=False, default=str))
    with open(out / f"predictions_{stamp}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["gold_id", "kind", "expert_set", "label_category", "pred_category", "label_urgency",
                    "pred_urgency", "label_injection", "pred_injection", "confidence", "fields_json"])
        for name, preds in preds_by_expert.items():
            for r in rows:
                p = preds[r["gold_id"]]
                w.writerow([r["gold_id"], r["kind"], name, r["label_category"], p["category"],
                            r["label_urgency"], p["urgency"], r["label_injection"], p["injection"],
                            p["confidence"], json.dumps(p["fields"], ensure_ascii=False)])
    print(f"\nSaved gold/results/scores_{stamp}.json and predictions_{stamp}.csv")


if __name__ == "__main__":
    main()
