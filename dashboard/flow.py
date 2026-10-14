"""Builds the live flow chart (Graphviz DOT) for one run from its log rows.

Pure function, no Streamlit, so it is easy to test.
"""

COLORS = {  # fill, border: grey waiting, blue running, green done, amber held/warning, red failed
    "waiting": ("#f1f0ec", "#b9b7af"),
    "running": ("#dbe8f9", "#2a78d6"),
    "ok": ("#dcf1dc", "#0ca30c"),
    "warning": ("#fdf0cf", "#e09a00"),
    "held": ("#fdf0cf", "#e09a00"),
    "failed": ("#f7d9d9", "#d03b3b"),
}

NODES = [("detect", "Detect"), ("land", "Land\\n(TinyDB)"), ("redact", "Redact"), ("route", "Route\\n(MoE)"),
         ("classify", "Classify"), ("extract", "Extract"), ("validate", "Validate"), ("load", "Load\\n(SQLite)"),
         ("decide", "Decide")]


def _short(expert: str) -> str:
    return expert.replace("claude-", "").replace("-5-5", " 5.5")


def build_dot(steps: dict[str, dict], usage: list[dict], decision: dict | None, import_row: dict | None,
              file_name: str) -> str:
    """steps: step name -> run_log row (status, rows_in, rows_out). usage: model_usage_log rows."""
    by_task: dict[str, dict[str, int]] = {}
    for u in usage:
        by_task.setdefault(u["task"], {})
        by_task[u["task"]][u["expert"]] = by_task[u["task"]].get(u["expert"], 0) + 1

    def experts(*tasks):
        parts = [f"{_short(e)} ×{n}" for t in tasks for e, n in by_task.get(t, {}).items()]
        return "\\n".join(parts)

    status = {name: steps[name]["status"] for name in steps}
    if usage:
        status["route"] = "ok"
    elif "classify" in steps:
        status["route"] = steps["classify"]["status"]

    labels = {
        "detect": "Detect\\n" + file_name.replace("staywatch_", ""),
        "classify": "Classify\\n" + experts("classify"),
        "extract": "Extract\\n" + experts("extract") + ("\\nescalated: " + experts("escalate") if "escalate" in by_task else ""),
        "route": f"Route (MoE)\\n{len(usage)} call(s)" if usage else "Route (MoE)",
    }
    if decision and decision.get("statistic") is not None:
        labels["decide"] = f"Decide\\nz = {decision['statistic']:+.2f}\\nvs ±{decision['threshold']:g}"
    elif decision:
        labels["decide"] = "Decide\\nnot enough history"

    lines = ['digraph G {', 'rankdir=TB; bgcolor="transparent"; nodesep=0.35; ranksep=0.45; pad=0.1; newrank=true;',
             'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=15, penwidth=2, margin="0.12,0.06"];',
             'edge [fontname="Helvetica", fontsize=13, penwidth=1.4, color="#898781", fontcolor="#52514e"];']
    for key, default in NODES:
        fill, border = COLORS.get(status.get(key, "waiting"), COLORS["waiting"])
        label = labels.get(key, default)
        while label.endswith("\\n"):          # drop empty trailing lines (rstrip would eat a final "n")
            label = label[:-2]
        lines.append(f'{key} [label="{label}", fillcolor="{fill}", color="{border}"];')

    import_row = import_row or {}
    final = import_row.get("status")
    act = steps.get("act", {}).get("status")
    held = final in ("held", "released", "rejected") or act == "held"
    published = final == "published"
    hold_label = {"released": f"Hold and alert\\nreleased by {import_row.get('decided_by')}",
                  "rejected": f"Hold and alert\\nrejected by {import_row.get('decided_by')}"}.get(
        final, "Hold and alert\\nwaiting for a person" if held else "Hold and alert")
    pub_fill, pub_border = COLORS["ok" if published or final == "released" else "waiting"]
    hold_fill, hold_border = COLORS["held" if held else "waiting"]
    lines.append(f'publish [label="Publish\\n(gold views)", fillcolor="{pub_fill}", color="{pub_border}"];')
    lines.append(f'hold [label="{hold_label}", fillcolor="{hold_fill}", color="{hold_border}"];')

    def rows(step):
        r = steps.get(step, {}).get("rows_out")
        return f' [label="{int(r)}"]' if r is not None and r == r else ""

    chain = [k for k, _ in NODES]
    source_of_rows = {"route": "redact", "classify": "redact"}
    for a, b in zip(chain, chain[1:]):
        lines.append(f"{a} -> {b}{rows(source_of_rows.get(a, a))};")
    taken = "publish" if published else "hold" if held else None
    for target in ("publish", "hold"):
        style = "solid" if target == taken else "dashed"
        lines.append(f'decide -> {target} [style={style}];')
    # Three rows so the chart stays readable: intake, processing, outcome
    lines.append("{rank=same; detect; land; redact; route; classify;}")
    lines.append("{rank=same; extract; validate; load; decide;}")
    lines.append("{rank=same; publish; hold;}")
    if final == "released":
        lines.append('hold -> publish [style=solid, label="person released"];')
    lines.append("}")
    return "\n".join(lines)
