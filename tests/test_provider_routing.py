"""No-network checks for optional two-provider specialist routing."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from app.services.chat import (
    LLMConfigurationError,
    _build_agent_graph,
    _build_runtime_graph,
    _configured_provider_routing,
    _create_nvidia_model,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_openai_remains_default(monkeypatch):
    monkeypatch.delenv("CLINIC_PROVIDER_ROUTING", raising=False)
    monkeypatch.setenv("CLINIC_AGENT_MODE", "supervisor")
    with patch("app.services.chat._build_supervisor_graph") as build:
        _build_runtime_graph(object(), [])
    assert build.call_args.kwargs == {}


def test_domain_route_assigns_only_policy_to_nvidia(monkeypatch):
    monkeypatch.setenv("CLINIC_AGENT_MODE", "supervisor")
    monkeypatch.setenv("CLINIC_PROVIDER_ROUTING", "domain")
    nvidia_model = object()
    with (
        patch("app.services.chat._create_nvidia_model", return_value=nvidia_model),
        patch("app.services.chat._build_supervisor_graph") as build,
    ):
        _build_runtime_graph(object(), [])
    assert build.call_args.kwargs["specialist_models"] == {
        "policy": nvidia_model,
    }


def test_domain_route_rejects_single_agent(monkeypatch):
    monkeypatch.setenv("CLINIC_AGENT_MODE", "single")
    monkeypatch.setenv("CLINIC_PROVIDER_ROUTING", "domain")
    with pytest.raises(LLMConfigurationError, match="requires"):
        _configured_provider_routing()


def test_domain_route_requires_nvidia_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(LLMConfigurationError, match="NVIDIA_API_KEY"):
        _create_nvidia_model()


def test_nvidia_model_uses_explicit_endpoint_and_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-key")
    monkeypatch.setenv("NVIDIA_MODEL", "test-nvidia-model")
    monkeypatch.setenv("NVIDIA_ENABLE_THINKING", "false")
    constructor = Mock(return_value=object())
    with patch("app.services.chat.ChatOpenAI", constructor):
        _create_nvidia_model()
    constructor.assert_called_once_with(
        model="test-nvidia-model",
        api_key="test-nvidia-key",
        base_url="https://integrate.api.nvidia.com/v1",
        temperature=1,
        top_p=0.95,
        max_tokens=4096,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )


def test_nvidia_thinking_mode_is_configurable(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-key")
    monkeypatch.setenv("NVIDIA_ENABLE_THINKING", "true")
    with patch("app.services.chat.ChatOpenAI") as constructor:
        _create_nvidia_model()
    assert constructor.call_args.kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True}
    }
    monkeypatch.setenv("NVIDIA_ENABLE_THINKING", "invalid")
    with pytest.raises(LLMConfigurationError, match="NVIDIA_ENABLE_THINKING"):
        _create_nvidia_model()


def test_invalid_route_rejected(monkeypatch):
    monkeypatch.setenv("CLINIC_PROVIDER_ROUTING", "unknown")
    with pytest.raises(LLMConfigurationError, match="openai or domain"):
        _configured_provider_routing()


@pytest.mark.anyio
async def test_prefetched_specialist_answer_skips_redundant_tool_model_call():
    @tool
    def sample_public_tool() -> str:
        """Return public clinic information."""
        return "verified"

    async def prefetch(_state):
        return {"messages": [
            AIMessage(content="", tool_calls=[{
                "name": "sample_public_tool", "args": {},
                "id": "prefetch-1", "type": "tool_call",
            }]),
            ToolMessage(
                content="verified", name="sample_public_tool",
                tool_call_id="prefetch-1",
            ),
        ]}

    model = ChatOpenAI(model="test-model", api_key="test-key")
    graph = _build_agent_graph(
        model, [sample_public_tool], "Use evidence.", prefetch=prefetch,
        answer_from_prefetched_tools=("sample_public_tool",),
        require_all_prefetched_tools=True,
    )
    with (
        patch.object(ChatOpenAI, "ainvoke", new_callable=AsyncMock) as raw_call,
        patch.object(ChatOpenAI, "bind_tools") as bind_tools,
    ):
        raw_call.return_value = AIMessage(content="Verified answer.")
        state = await graph.ainvoke({"messages": [HumanMessage(content="Question?")]})
    assert state["messages"][-1].content == "Verified answer."
    raw_call.assert_awaited_once()
    bind_tools.assert_not_called()
