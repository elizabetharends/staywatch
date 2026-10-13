"""Tests never call live models: they cost money and the results change.

The real API keys are removed from the environment for every test, and any attempt to
build a real Anthropic client fails loudly. Tests that need model answers inject a
FakeClaude with recorded responses.
"""
import pytest


@pytest.fixture(autouse=True)
def no_live_model_calls(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    def refuse(*args, **kwargs):
        raise RuntimeError("live model call attempted in a test")

    monkeypatch.setattr("anthropic.Anthropic.__init__", refuse)
