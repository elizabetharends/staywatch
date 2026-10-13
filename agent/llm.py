"""Thin wrappers around the model APIs. Each returns the raw text plus usage, so the
caller can log tokens, latency and cost. Tests swap these for recorded responses.
"""
import os
import time
from dataclasses import dataclass

import anthropic
from dotenv import load_dotenv

from agent.settings import ROOT

load_dotenv(ROOT / ".env")


@dataclass
class ModelReply:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    stop_reason: str
    served_by: str          # the model that actually answered (differs after a fallback)


class ModelUnavailable(Exception):
    """The expert could not answer (network, auth, refusal...). The router decides what next.

    setup_error=True means retrying cannot help (bad key, missing workspace, bad request),
    so the client stops calling the model for the rest of this scan.
    """

    def __init__(self, message: str, setup_error: bool = False, record_problem: bool = False):
        super().__init__(message)
        self.setup_error = setup_error
        self.record_problem = record_problem   # e.g. a refusal: the record's content is the issue


class ClaudeClient:
    def __init__(self, settings: dict):
        self.models = settings["models"]
        self.client, self.down_reason = None, "ANTHROPIC_API_KEY not set"
        if os.environ.get("ANTHROPIC_API_KEY"):
            # Keys that aren't scoped to a workspace need the workspace ID on every request
            workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
            self.client = anthropic.Anthropic(
                timeout=60.0, max_retries=2,
                default_headers={"anthropic-workspace-id": workspace} if workspace else None)
            self.down_reason = ""

    def available(self) -> tuple[bool, str]:
        return (not self.down_reason, self.down_reason)

    def _setup_error(self, message: str) -> ModelUnavailable:
        self.down_reason = message[:120]
        return ModelUnavailable(message, setup_error=True)

    def call(self, model: str, system: str, user: str, schema: dict, max_tokens: int) -> ModelReply:
        if self.down_reason:
            raise ModelUnavailable(self.down_reason, setup_error=True)
        kwargs = dict(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        effort = self.models.get("effort", {}).get(model)
        if effort:
            kwargs["output_config"]["effort"] = effort
        start = time.perf_counter()
        try:
            if self.models.get("fallbacks", {}).get(model):
                response = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                response = self.client.messages.create(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as err:
            raise self._setup_error(f"authentication failed: {_api_message(err)}") from err
        except anthropic.RateLimitError as err:
            raise ModelUnavailable("rate limited") from err
        except anthropic.BadRequestError as err:
            raise self._setup_error(f"bad request: {_api_message(err)}") from err
        except anthropic.APIStatusError as err:
            raise ModelUnavailable(f"API error {err.status_code}: {_api_message(err)}") from err
        except anthropic.APIConnectionError as err:
            raise ModelUnavailable("network error") from err
        latency_ms = round((time.perf_counter() - start) * 1000)
        if response.stop_reason == "refusal":
            raise ModelUnavailable("model declined the request (refusal)", record_problem=True)
        text = next((b.text for b in response.content if b.type == "text"), "")
        return ModelReply(text=text, input_tokens=response.usage.input_tokens,
                          output_tokens=response.usage.output_tokens, latency_ms=latency_ms,
                          stop_reason=response.stop_reason or "", served_by=response.model)


def _api_message(err: anthropic.APIStatusError) -> str:
    body = err.body if isinstance(err.body, dict) else {}
    return str(body.get("error", {}).get("message") or err.message)
