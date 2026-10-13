"""The mixture-of-experts router, at the application level.

Some large models use "mixture of experts" inside the network: a learned gate sends
each token to specialist sub-networks. Here the idea is applied to whole tasks: this
readable function sends each task to the expert model best suited to it and returns
the reason, which is logged with every call.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    expert: str     # "jev", a Claude model id, or "rules"
    reason: str


def route(task: str, settings: dict, *, jev_ok: tuple[bool, str] = (False, "disabled"),
          claude_ok: tuple[bool, str] = (True, ""), budget_ok: bool = True,
          escalate: bool = False) -> Route:
    models = settings["models"]
    if models["kill_switch"]:
        return Route("rules", "kill switch is on: no model calls")
    if not budget_ok:
        return Route("rules", "token budget reached")

    if task == "classify":
        if jev_ok[0]:
            return Route("jev", "classification: Jev is fast, cheap and returns probabilities")
        if claude_ok[0]:
            return Route(models["extractor"], f"Jev unavailable ({jev_ok[1]}): Claude answers the same questions")
        return Route("rules", f"Jev unavailable ({jev_ok[1]}) and Claude unavailable ({claude_ok[1]})")

    if task == "extract":
        if not claude_ok[0]:
            return Route("rules", f"Claude unavailable ({claude_ok[1]})")
        if escalate:
            return Route(models["escalation"], "escalation: first extraction was low confidence or invalid")
        return Route(models["extractor"], "extraction: Jev only chooses options, Claude can write values")

    if task == "explain":
        if not claude_ok[0]:
            return Route("rules", f"Claude unavailable ({claude_ok[1]})")
        return Route(models["escalation"], "explaining a held import needs good writing; rare and short")

    raise ValueError(f"unknown task {task!r}")
