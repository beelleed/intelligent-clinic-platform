"""Keep test defaults independent of a developer's ignored local .env."""

import pytest


@pytest.fixture(autouse=True)
def default_agent_configuration(monkeypatch):
    monkeypatch.setenv("CLINIC_AGENT_MODE", "single")
    monkeypatch.setenv("CLINIC_PROVIDER_ROUTING", "openai")
    monkeypatch.setenv("TAVILY_ENABLED", "false")
