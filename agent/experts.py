"""Runs classify and extract for one record: route, cache, call, validate, escalate, log.

Every expert call (including cache hits and the rules fallback) writes one row to
model_usage_log, so the dashboard can show which model did what, when and why.
"""
import hashlib
import json
import uuid
from datetime import date, datetime, timezone

from pydantic import ValidationError

from agent.db import Database
from agent.llm import ClaudeClient, ModelReply, ModelUnavailable
from agent.reference import CATEGORIES, LISTINGS, URGENCY_LEVELS, is_blank
from agent.router import route
from agent.schemas import (CLASSIFICATION_SCHEMA, EXTRACTION_SCHEMA, SOURCE_OF, Classification,
                           Extraction)
from agent.settings import ROOT
from agent import rules

PROMPTS = ROOT / "prompts"
EXTRACT_PROMPT = "extract_v1"
CLASSIFY_PROMPT = "classify_claude_v1"
CLASSIFY_FIELDS = ("listing", "guest_message", "host_note")


def _load_prompt(name: str) -> str:
    text = (PROMPTS / f"{name}.md").read_text()
    return text.format(
        listings=", ".join(LISTINGS),
        categories="\n".join(f"   - {c}" for c in CATEGORIES),
        urgency="\n".join(f"   - {u}" for u in URGENCY_LEVELS),
    )


class InvalidAnswer(Exception):
    pass


class ModelRunner:
    def __init__(self, db: Database, settings: dict, claude: ClaudeClient | None = None):
        self.db, self.settings = db, settings
        self.models, self.budget = settings["models"], settings["budget"]
        self.claude = claude or ClaudeClient(settings)
        self.prompts = {EXTRACT_PROMPT: _load_prompt(EXTRACT_PROMPT),
                        CLASSIFY_PROMPT: _load_prompt(CLASSIFY_PROMPT)}
        self.run_tokens = 0
        self.daily_tokens = None
        self.budget_incident_open = False

    # ---------- shared plumbing ----------

    def start_file(self) -> None:
        """The per-run token cap counts from zero for each import file."""
        today = datetime.now(timezone.utc).date().isoformat()
        (row,) = self.db.run_sql(
            "route",
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS used FROM model_usage_log"
            " WHERE called_at >= ?",
            (today,),
        )
        self.daily_tokens = row["used"]
        self.run_tokens = 0
        self.budget_incident_open = False

    def jev_ok(self) -> tuple[bool, str]:
        return (False, "disabled in config") if not self.models["jev_enabled"] else (False, "client not built yet")

    def budget_ok(self) -> bool:
        ok = (self.run_tokens < self.budget["max_tokens_per_run"]
              and self.daily_tokens + self.run_tokens < self.budget["max_tokens_per_day"])
        if not ok and not self.budget_incident_open:
            self.db.open_incident(f"inc_{uuid.uuid4().hex[:8]}", "budget",
                                  f"token cap reached (run {self.run_tokens}, day "
                                  f"{self.daily_tokens + self.run_tokens}); rules take over")
            self.budget_incident_open = True
        return ok

    def _cost(self, model: str, reply: ModelReply) -> float:
        price = self.models.get("prices", {}).get(model, {})
        return round((reply.input_tokens * price.get("input", 0)
                      + reply.output_tokens * price.get("output", 0)) / 1e6, 6)

    def _cached_call(self, step, task, model, prompt, user, schema, max_tokens, reason, pii):
        """Returns (text, call_id). Uses the cache when the same input was answered before."""
        key = hashlib.sha256(json.dumps([task, model, prompt, user], sort_keys=True).encode()).hexdigest()
        hit = self.db.run_sql(step, "SELECT response_text FROM model_cache WHERE cache_key = ?", (key,))
        call_id = uuid.uuid4().hex[:12]
        if hit:
            self.db.log_model_call(call_id=call_id, step=step, task=task, expert=model,
                                   route_reason=f"{reason} [cache hit]", prompt_version=prompt,
                                   input_tokens=0, output_tokens=0, latency_ms=0,
                                   estimated_cost_usd=0.0, pii_redacted=pii, outcome="cached")
            return hit[0]["response_text"], call_id, True
        reply = self.claude.call(model, self.prompts[prompt], user, schema, max_tokens)
        self.run_tokens += reply.input_tokens + reply.output_tokens
        self.db.log_model_call(call_id=call_id, step=step, task=task, expert=reply.served_by or model,
                               route_reason=reason, prompt_version=prompt,
                               input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                               latency_ms=reply.latency_ms, estimated_cost_usd=self._cost(model, reply),
                               pii_redacted=pii, outcome="pending")
        self.db.run_sql(step, "INSERT OR REPLACE INTO model_cache VALUES (?, ?, ?, ?, ?, ?)",
                        (key, task, model, prompt, reply.text, datetime.now(timezone.utc).isoformat()))
        return reply.text, call_id, False

    def _set_outcome(self, call_id: str, outcome: str, confidence: float | None) -> None:
        self.db._write_log("UPDATE model_usage_log SET outcome = ?, confidence = ? WHERE call_id = ?",
                           (outcome, confidence, call_id))

    def _log_rules(self, step, task, reason, confidence, pii):
        self.db.log_model_call(call_id=uuid.uuid4().hex[:12], step=step, task=task, expert="rules",
                               route_reason=reason, prompt_version="rules_v1", input_tokens=0,
                               output_tokens=0, latency_ms=0, confidence=confidence,
                               estimated_cost_usd=0.0, pii_redacted=pii, outcome="valid")

    # ---------- classify ----------

    def classify(self, record: dict, pii: int) -> dict:
        if all(is_blank(record.get(k)) for k in ("guest_message", "host_note")):
            result = {"category": "general", "urgency": "Low", "injection": False, "confidence": 1.0}
            self._log_rules("classify", "classify", "no free text to classify: no model needed", 1.0, pii)
            return {**result, "expert": "rules"}
        r = route("classify", self.settings, jev_ok=self.jev_ok(), claude_ok=self.claude.available(),
                  budget_ok=self.budget_ok())
        if r.expert != "rules":
            user = json.dumps({k: record.get(k) for k in CLASSIFY_FIELDS}, ensure_ascii=False)
            call_id = None
            try:
                text, call_id, cached = self._cached_call(
                    "classify", "classify", r.expert, CLASSIFY_PROMPT, f"<record>\n{user}\n</record>",
                    CLASSIFICATION_SCHEMA, self.models["classify_max_tokens"], r.reason, pii)
                result = Classification.model_validate_json(text).model_dump()
                if not cached:
                    self._set_outcome(call_id, "valid", result["confidence"])
                return {**result, "expert": r.expert}
            except (ModelUnavailable, ValidationError) as err:
                if call_id:
                    self._set_outcome(call_id, "retried", None)
                r = type(r)("rules", f"{r.expert} failed ({str(err).splitlines()[0][:80]})")
        result = Classification.model_validate(rules.classify(record)).model_dump()
        self._log_rules("classify", "classify", r.reason, result["confidence"], pii)
        return {**result, "expert": "rules"}

    # ---------- extract ----------

    @staticmethod
    def invented_fields(record: dict, fields: dict) -> list[str]:
        """Fields with a value although the source was blank: the model made them up."""
        return [f for f, src in SOURCE_OF.items() if is_blank(record.get(src)) and fields.get(f) is not None]

    def _validate_extraction(self, record: dict, text: str) -> dict:
        fields = Extraction.model_validate_json(text).model_dump()
        invented = self.invented_fields(record, fields)
        if invented:
            raise InvalidAnswer(f"invented values for blank fields: {', '.join(invented)}")
        return fields

    def extract(self, record: dict, received: date, pii: int) -> tuple[dict | None, str, str]:
        """Returns (fields or None, expert that produced them, status: valid|review|quarantined)."""
        user = (f"received_date: {received.isoformat()}\n\n<record>\n"
                f"{json.dumps(record, ensure_ascii=False)}\n</record>")
        escalate, last_error = False, ""
        for _attempt in range(2):
            r = route("extract", self.settings, claude_ok=self.claude.available(),
                      budget_ok=self.budget_ok(), escalate=escalate)
            if r.expert == "rules":
                break
            call_id = None
            try:
                text, call_id, cached = self._cached_call(
                    "extract", "escalate" if escalate else "extract", r.expert, EXTRACT_PROMPT, user,
                    EXTRACTION_SCHEMA, self.models["extract_max_tokens"], r.reason, pii)
                fields = self._validate_extraction(record, text)
            except ModelUnavailable as err:
                if err.record_problem:
                    last_error = str(err)
                    if escalate:
                        return None, r.expert, f"quarantined: {last_error}"
                    escalate = True
                    continue
                if call_id:
                    self._set_outcome(call_id, "retried", None)
                if err.setup_error or escalate:
                    # Not the record's fault: clean it with rules instead of quarantining it
                    r = type(r)("rules", f"{r.expert} unavailable ({str(err)[:100]})")
                    break
                escalate = True
                continue
            except (ValidationError, InvalidAnswer) as err:
                last_error = str(err).splitlines()[0][:120]
                if call_id:
                    self._set_outcome(call_id, "escalated" if not escalate else "quarantined", None)
                if escalate:
                    return None, r.expert, f"quarantined: {last_error}"
                escalate = True
                continue
            confidence = 1.0 if fields["confidence"] == "high" else 0.0
            if fields["confidence"] == "low" and not escalate:
                if call_id and not cached:
                    self._set_outcome(call_id, "escalated", confidence)
                escalate = True
                continue
            if call_id and not cached:
                self._set_outcome(call_id, "valid", confidence)
            return fields, r.expert, "valid" if fields["confidence"] == "high" else "review"
        else:
            return None, r.expert, f"quarantined: {last_error}"

        # Rules fallback (kill switch, budget, or Claude unavailable)
        try:
            fields = Extraction.model_validate(rules.extract(record)).model_dump()
        except ValidationError as err:
            self._log_rules("extract", "extract", r.reason, 0.0, pii)
            return None, "rules", f"quarantined: {str(err).splitlines()[0][:120]}"
        self._log_rules("extract", "extract", r.reason, 1.0, pii)
        return fields, "rules", "valid"
