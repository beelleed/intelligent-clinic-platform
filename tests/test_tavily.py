import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.services.tavily import (
    DOMAINS, TOOL_NAME, TURN_BUDGET, allowed_url, append_external_sources,
    clean_results, search_public_sources, tavily_enabled,
)


def test_optional_configuration(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert not tavily_enabled()
    monkeypatch.setenv("TAVILY_API_KEY", "test-only")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    assert tavily_enabled()
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    assert not tavily_enabled()


def test_capabilities_are_safe_and_respect_public_switch(monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import app
    monkeypatch.setenv("TAVILY_API_KEY", "test-only-private-value")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_CHAT_ENABLED", "true")
    client = TestClient(app)
    response = client.get("/capabilities")
    assert response.json() == {"official_resources": True}
    assert response.headers["cache-control"] == "no-store"
    assert "test-only-private-value" not in response.text
    monkeypatch.setenv("PUBLIC_CHAT_ENABLED", "false")
    assert client.get("/capabilities").json() == {"official_resources": False}


def test_resource_entry_is_review_before_send():
    from fastapi.testclient import TestClient
    from app.api import app
    client = TestClient(app)
    assert 'id="official-resources"' in client.get("/").text
    script = client.get("/static/app.js").text
    handler = script.split('document.getElementById("official-resources-button").addEventListener', 1)[1].split('});', 1)[0]
    assert "questionInput.value" in handler
    assert "askAgent(" not in handler


@pytest.mark.parametrize("url", ["https://cdc.gov.evil.test/x", "http://cdc.gov/", "javascript:alert(1)", "https://user:password@cdc.gov/", "https://example.com/"])
def test_allowlist_rejects_untrusted_sources(url):
    assert not allowed_url(url)


def test_clean_results_caps_size_and_preserves_only_official_urls():
    data = {"results": [{"url": "https://evil.test/", "content": "fake"}] + [{"url": "https://www.cdc.gov/" + str(i), "title": "Official", "content": "x" * 4000} for i in range(5)]}
    result = clean_results(data)
    assert len(result["results"]) == 3
    assert all(len(item["content"]) == 1800 for item in result["results"])


def test_mcp_search_budget_fixed_parameters_and_safe_failures(monkeypatch):
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "test-only")
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.list_tools.return_value = [SimpleNamespace(name="tavily_search")]
    client.call_tool.return_value = SimpleNamespace(structured_content={"results": [{"url": "https://www.cdc.gov/clean-hands/", "title": "CDC", "content": "Public handwashing resource."}]})

    async def run():
        token = TURN_BUDGET.set({"used": 0})
        try:
            first = await search_public_sources("CDC official handwashing resources")
            second = await search_public_sources("CDC more handwashing resources")
            return first, second
        finally:
            TURN_BUDGET.reset(token)

    with patch("app.services.tavily.Client", return_value=client):
        first, second = asyncio.run(run())
    assert first["results"]
    assert not second["results"] and "One external" in second["error"]
    arguments = client.call_tool.call_args.args[1]
    assert arguments["search_depth"] == "basic"
    assert arguments["max_results"] == 3
    assert arguments["include_domains"] == list(DOMAINS)
    assert client.call_tool.await_count == 1


@pytest.mark.parametrize("query", ["my symptoms and dosage", "Email belle@example.com", "Call 1234567890", "x"])
def test_sensitive_or_invalid_query_never_connects(monkeypatch, query):
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "test-only")
    with patch("app.services.tavily.Client") as client:
        assert not asyncio.run(search_public_sources(query))["results"]
        client.assert_not_called()


def test_failed_transport_has_no_secret_in_result_or_logs(monkeypatch, caplog):
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "test-only")
    with patch("app.services.tavily.Client", side_effect=RuntimeError("secret-private-value")):
        result = asyncio.run(search_public_sources("CDC official resources"))
    assert result["results"] == []
    assert "secret-private-value" not in json.dumps(result) + caplog.text


def test_only_current_successful_tool_urls_are_appended():
    evidence = json.dumps({"results": [{"url": "https://www.cdc.gov/", "title": "CDC"}]})
    messages = [HumanMessage(content="prior"), ToolMessage(content=evidence, name=TOOL_NAME, tool_call_id="old"), HumanMessage(content="current"), AIMessage(content="https://invented.test")]
    assert append_external_sources("Answer", messages) == "Answer"
    messages.append(ToolMessage(content=evidence, name=TOOL_NAME, tool_call_id="new"))
    assert "External reference: https://www.cdc.gov/" in append_external_sources("Answer", messages)


def test_runtime_adds_optional_search_to_single_agent(monkeypatch):
    from app.services.chat import _build_runtime_graph
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "test-only")
    with patch("app.services.chat._build_chat_graph") as build:
        _build_runtime_graph(object(), [])
    assert [tool.name for tool in build.call_args.args[1]] == [TOOL_NAME]


def test_supervisor_manager_receives_external_tool(monkeypatch):
    from langchain_core.tools import StructuredTool
    from app.services.chat import _build_supervisor_graph
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "test-only")
    tools = [StructuredTool.from_function(lambda: "{}", name=name, description="Test fixture") for name in (
        "clinic_knowledge_search_clinic_knowledge", "clinic_operations_list_doctors",
        "clinic_operations_get_queue_status", "clinic_appointments_list_available_appointments",
    )]
    with patch("app.services.chat._build_agent_graph", return_value=object()) as build:
        _build_supervisor_graph(object(), tools)
    names = [item.name for item in build.call_args.args[1]]
    assert TOOL_NAME in names
    assert "consult_operations_agent" in names


def test_forged_external_reference_is_removed_without_successful_search():
    result = append_external_sources("Answer\nExternal reference: https://www.cdc.gov/invented", [HumanMessage(content="hello")])
    assert result == "Answer"
