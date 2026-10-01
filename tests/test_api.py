from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, SystemMessage

from app.api import app
from app.rate_limit import chat_rate_limiter
from app.services.chat import MCPConnectionError


client = TestClient(app)


@pytest.fixture(autouse=True)
def no_mcp_tools(monkeypatch):
    """Keep API unit tests focused; MCP behavior has dedicated tests."""
    async def load_no_tools():
        return []

    monkeypatch.setattr("app.services.chat._load_mcp_tools", load_no_tools)
    chat_rate_limiter.reset()
    yield
    chat_rate_limiter.reset()


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "demo_mode": False,
        "agent": "enabled",
        "mcp_servers": 3,
        "public_chat_enabled": True,
    }


def test_home_serves_clinic_agent_frontend() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "Intelligent Clinic Assistant" in response.text
    assert 'id="chat-form"' in response.text
    assert 'id="demo-question"' in response.text
    assert 'id="grounding-note"' in response.text
    assert "/static/app.js?v=" in response.text
    assert "/static/style.css?v=" in response.text


def test_frontend_assets_use_chat_contract_and_session_memory() -> None:
    script_response = client.get("/static/app.js")
    stylesheet_response = client.get("/static/style.css")

    assert script_response.status_code == 200
    assert stylesheet_response.status_code == 200
    assert 'fetch("/chat"' in script_response.text
    assert 'fetch("/health")' in script_response.text
    assert 'fetch("/demo/query"' in script_response.text
    assert "session_id: getSessionId()" in script_response.text
    assert "health.public_chat_enabled === false" in script_response.text
    assert "Continue with clinic FAQs" in client.get("/").text
    assert "Ask Lumi" in client.get("/").text
    assert "Chat with Lumi" in script_response.text
    assert "Back to Clinic FAQs" in script_response.text
    assert "Sample clinic FAQ information only" in script_response.text
    assert "Lumi provides sample clinic operations information" in script_response.text
    assert "demoQuestionElement.textContent = question" in script_response.text
    assert "The FAQ data is newer than the running server" in script_response.text
    assert "response.status === 429" in script_response.text
    assert "text/css" in stylesheet_response.headers["content-type"]


@pytest.mark.parametrize(
    ("demo_id", "source"),
    [
        ("clinic-hours", "clinic-hours"),
        ("parking", "getting-here-and-parking"),
        ("check-in-and-late-arrival", "appointments-and-check-in"),
        ("cancel-or-reschedule", "appointments-and-check-in"),
        ("appointment-requests", "appointments-and-check-in"),
        ("wait-time-estimates", "queue-and-wait-times"),
        ("procedure-status-information", "procedure-status"),
        ("public-information-privacy", "queue-and-wait-times"),
        ("urgent-care", "urgent-care-and-emergencies"),
        ("contact-information", "contact-information"),
    ],
)
def test_predefined_demo_scenarios_are_key_free(
    monkeypatch,
    demo_id: str,
    source: str,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    response = client.post("/demo/query", json={"demo_id": demo_id})

    assert response.status_code == 200
    payload = response.json()
    assert payload["answer"]
    if source is None:
        assert payload["sources"] == []
    else:
        assert payload["sources"][0]["source"] == source


@pytest.mark.parametrize(
    "removed_live_scenario",
    ["queue-status", "procedure-status", "appointments", "doctor-schedule"],
)
def test_demo_rejects_live_operational_scenarios(
    removed_live_scenario: str,
) -> None:
    response = client.post(
        "/demo/query",
        json={"demo_id": removed_live_scenario},
    )

    assert response.status_code == 404


def test_chat_accepts_valid_request(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")

    with patch("app.services.chat.ChatOpenAI") as mock_chat_openai:
        mock_chat_openai.return_value.ainvoke = AsyncMock(
            return_value=AIMessage(content="MCP is a protocol."),
        )

        response = client.post(
            "/chat",
            json={"query": "What is MCP?", "session_id": "test-1"},
        )

    assert response.status_code == 200
    assert response.json() == {"response": "MCP is a protocol."}
    mock_chat_openai.assert_called_once_with(
        model="test-model",
        api_key="test-key",
        base_url="https://example.test/v1",
    )
    mock_chat_openai.return_value.ainvoke.assert_awaited_once()
    messages = mock_chat_openai.return_value.ainvoke.await_args.args[0]
    assert isinstance(messages[0], SystemMessage)
    assert "not explicitly supported by the tool results" in messages[0].content
    assert [message.content for message in messages[1:]] == ["What is MCP?"]


def test_chat_rejects_blank_query() -> None:
    response = client.post(
        "/chat",
        json={"query": "   ", "session_id": "test-1"},
    )

    assert response.status_code == 422


def test_chat_rejects_blank_session_id() -> None:
    response = client.post(
        "/chat",
        json={"query": "Hello", "session_id": "   "},
    )

    assert response.status_code == 422


def test_chat_rejects_overlong_query() -> None:
    response = client.post(
        "/chat",
        json={"query": "x" * 1001, "session_id": "test-1"},
    )

    assert response.status_code == 422


def test_chat_rejects_overlong_session_id() -> None:
    response = client.post(
        "/chat",
        json={"query": "Hello", "session_id": "x" * 129},
    )

    assert response.status_code == 422


def test_chat_can_be_disabled_for_public_demo(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_CHAT_ENABLED", "false")

    response = client.post(
        "/chat",
        json={"query": "Hello", "session_id": "public-demo-test"},
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Live agent requests are disabled for this public demo."
    }


def test_chat_returns_429_after_rate_limit(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_DAY", "30")

    with patch("app.services.chat.ChatOpenAI") as mock_chat_openai:
        mock_chat_openai.return_value.ainvoke = AsyncMock(
            return_value=AIMessage(content="Ready."),
        )
        first_response = client.post(
            "/chat",
            json={"query": "Hello", "session_id": "rate-limit-1"},
        )
        second_response = client.post(
            "/chat",
            json={"query": "Hello again", "session_id": "rate-limit-2"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 429
    assert second_response.json() == {
        "detail": "The per-minute Live Agent request limit has been reached."
    }
    assert int(second_response.headers["retry-after"]) >= 1
    assert second_response.headers["x-rate-limit-reason"] == "minute"


def test_chat_identifies_daily_rate_limit(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "10")
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_DAY", "1")

    with patch("app.services.chat.ChatOpenAI") as mock_chat_openai:
        mock_chat_openai.return_value.ainvoke = AsyncMock(
            return_value=AIMessage(content="Ready."),
        )
        first_response = client.post(
            "/chat",
            json={"query": "Hello", "session_id": "daily-limit-1"},
        )
        second_response = client.post(
            "/chat",
            json={"query": "Hello again", "session_id": "daily-limit-2"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 429
    assert second_response.json() == {
        "detail": "The daily Live Agent request limit has been reached."
    }
    assert second_response.headers["x-rate-limit-reason"] == "day"
    assert int(second_response.headers["retry-after"]) >= 1


def test_chat_uses_default_model_when_model_is_not_configured(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    with patch("app.services.chat.ChatOpenAI") as mock_chat_openai:
        mock_chat_openai.return_value.ainvoke = AsyncMock(
            return_value=AIMessage(content="Ready."),
        )

        response = client.post(
            "/chat",
            json={"query": "Hello", "session_id": "default-model-test"},
        )

    assert response.status_code == 200
    mock_chat_openai.assert_called_once_with(
        model="gpt-5",
        api_key="test-key",
        base_url=None,
    )


def test_chat_returns_controlled_error_when_api_key_is_missing(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "test-model")

    with patch("app.services.chat.ChatOpenAI") as mock_chat_openai:
        response = client.post(
            "/chat",
            json={"query": "Hello", "session_id": "missing-key-test"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "The clinic agent is temporarily unavailable."
    }
    mock_chat_openai.assert_not_called()


def test_chat_returns_controlled_error_when_model_request_fails(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    with patch("app.services.chat.ChatOpenAI") as mock_chat_openai:
        mock_chat_openai.return_value.ainvoke = AsyncMock(
            side_effect=RuntimeError("provider unavailable"),
        )

        response = client.post(
            "/chat",
            json={"query": "Hello", "session_id": "failure-test"},
        )

    assert response.status_code == 502
    assert response.json() == {
        "detail": "The language model service is temporarily unavailable."
    }
    mock_chat_openai.assert_called_once_with(
        model="test-model",
        api_key="test-key",
        base_url=None,
    )


def test_chat_returns_controlled_error_when_mcp_connection_fails(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")

    with (
        patch("app.services.chat.ChatOpenAI"),
        patch(
            "app.services.chat._load_mcp_tools",
            new=AsyncMock(
                side_effect=MCPConnectionError(
                    "MCP connection error: clinic tools are unavailable."
                )
            ),
        ),
    ):
        response = client.post(
            "/chat",
            json={"query": "Who is available?", "session_id": "mcp-failure"},
        )

    assert response.status_code == 502
    assert response.json() == {
        "detail": "Clinic tools are temporarily unavailable."
    }


def test_chat_preserves_memory_within_session_and_isolates_sessions(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    with patch("app.services.chat.ChatOpenAI") as mock_chat_openai:
        mock_chat_openai.return_value.ainvoke = AsyncMock(
            side_effect=[
                AIMessage(content="I will remember that."),
                AIMessage(content="Your favorite color is blue."),
                AIMessage(content="I do not know your favorite color."),
            ],
        )

        first_response = client.post(
            "/chat",
            json={
                "query": "My favorite color is blue.",
                "session_id": "memory-session",
            },
        )
        same_session_response = client.post(
            "/chat",
            json={
                "query": "What is my favorite color?",
                "session_id": "memory-session",
            },
        )
        different_session_response = client.post(
            "/chat",
            json={
                "query": "What is my favorite color?",
                "session_id": "isolated-session",
            },
        )

    assert first_response.status_code == 200
    assert same_session_response.json() == {
        "response": "Your favorite color is blue."
    }
    assert different_session_response.json() == {
        "response": "I do not know your favorite color."
    }

    calls = mock_chat_openai.return_value.ainvoke.await_args_list
    same_session_messages = calls[1].args[0]
    isolated_session_messages = calls[2].args[0]

    same_session_messages = [
        message
        for message in same_session_messages
        if not isinstance(message, SystemMessage)
    ]
    isolated_session_messages = [
        message
        for message in isolated_session_messages
        if not isinstance(message, SystemMessage)
    ]

    assert [message.content for message in same_session_messages] == [
        "My favorite color is blue.",
        "I will remember that.",
        "What is my favorite color?",
    ]
    assert [message.content for message in isolated_session_messages] == [
        "What is my favorite color?"
    ]
