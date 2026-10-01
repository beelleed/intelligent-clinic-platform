"""Focused tests for LangGraph agent integration with real MCP servers."""

import json
import logging
import sys

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from app.services.chat import (
    _answer_with_verified_sources,
    _load_mcp_tools,
    chat_runtime_ready,
    create_chat_response,
    initialize_chat_runtime,
    shutdown_chat_runtime,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_clinic_database(monkeypatch, tmp_path):
    operations_database = tmp_path / "clinic.db"
    appointments_database = tmp_path / "appointments.db"
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "clinic_operations": {
                        "command": sys.executable,
                        "args": ["-m", "mcp_servers.clinic_operations.server"],
                        "env": {"CLINIC_DB_PATH": str(operations_database)},
                    },
                    "clinic_appointments": {
                        "command": sys.executable,
                        "args": ["-m", "mcp_servers.clinic_appointments.server"],
                        "env": {
                            "CLINIC_APPOINTMENTS_DB_PATH": str(
                                appointments_database
                            )
                        },
                    },
                    "clinic_knowledge": {
                        "command": sys.executable,
                        "args": ["-m", "mcp_servers.clinic_knowledge.server"],
                        "env": {
                            "CLINIC_KNOWLEDGE_SEARCH_MODE": "lexical",
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(config_path))
    monkeypatch.setenv("CLINIC_DB_PATH", str(operations_database))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("CLINIC_KNOWLEDGE_SEARCH_MODE", "lexical")


class ToolCallingModel:
    """Deterministic model double that requests one discovered MCP tool."""

    def __init__(self):
        self.bound_tool_names = []
        self.tool_message = None

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names = [tool.name for tool in tools]
        return self

    async def ainvoke(self, messages):
        tool_messages = [
            message for message in messages if isinstance(message, ToolMessage)
        ]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "clinic_operations_get_queue_status",
                        "args": {
                            "doctor_id": "dr-lee",
                            "patient_number": 21,
                        },
                        "id": "queue-call-1",
                        "type": "tool_call",
                    }
                ],
            )

        self.tool_message = tool_messages[-1]
        return AIMessage(
            content="Dr. Amanda Lee is currently serving number 18."
        )


class MultiServerToolCallingModel:
    """Model double that chains calls across both clinic MCP servers."""

    def __init__(self):
        self.bound_tool_names = []
        self.tool_messages = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names = [tool.name for tool in tools]
        return self

    async def ainvoke(self, messages):
        self.tool_messages = [
            message for message in messages if isinstance(message, ToolMessage)
        ]
        if not self.tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "clinic_knowledge_search_clinic_knowledge",
                        "args": {
                            "query": "Are queue wait times guaranteed?",
                            "max_results": 1,
                        },
                        "id": "knowledge-call-1",
                        "type": "tool_call",
                    }
                ],
            )
        if len(self.tool_messages) == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "clinic_operations_get_queue_status",
                        "args": {
                            "doctor_id": "dr-lee",
                            "patient_number": 21,
                        },
                        "id": "operations-call-1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(
            content=(
                "Wait times are estimates. Dr. Amanda Lee is currently "
                "serving number 18."
            )
        )


class ParallelMultiToolCallingModel:
    """Model double that requests independent tools in one model response."""

    def __init__(self):
        self.bound_tool_names = []
        self.tool_messages = []
        self.call_count = 0

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names = [tool.name for tool in tools]
        return self

    async def ainvoke(self, messages):
        self.call_count += 1
        self.tool_messages = [
            message for message in messages if isinstance(message, ToolMessage)
        ]
        if not self.tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "clinic_knowledge_search_clinic_knowledge",
                        "args": {
                            "query": "Are procedure estimates guaranteed?",
                            "max_results": 1,
                        },
                        "id": "parallel-knowledge-call",
                        "type": "tool_call",
                    },
                    {
                        "name": "clinic_operations_get_procedure_status",
                        "args": {"doctor_id": "dr-chen"},
                        "id": "parallel-operations-call",
                        "type": "tool_call",
                    },
                ],
            )
        return AIMessage(
            content="The procedure is in progress, and estimates are not guaranteed."
        )


class NoToolModel:
    """Model double that answers directly while MCP tools are available."""

    def __init__(self):
        self.bound_tool_names = []
        self.call_count = 0

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names = [tool.name for tool in tools]
        return self

    async def ainvoke(self, messages):
        self.call_count += 1
        return AIMessage(content="Hello! How can I help you today?")


class FailingToolModel:
    """Model double that exposes a handled tool failure as its final answer."""

    def __init__(self, tool_name):
        self.tool_name = tool_name
        self.tool_message = None

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        tool_messages = [
            message for message in messages if isinstance(message, ToolMessage)
        ]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": {},
                        "id": "failure-call-1",
                        "type": "tool_call",
                    }
                ],
            )
        self.tool_message = tool_messages[-1]
        return AIMessage(content=str(self.tool_message.content))


@tool
def unavailable_clinic_tool() -> str:
    """Simulate an MCP tool execution failure."""
    raise RuntimeError("internal tool details must not reach the response")


@tool
def malformed_clinic_tool() -> str:
    """Simulate a malformed MCP response payload."""
    raise json.JSONDecodeError("private malformed payload", "x", 0)


@pytest.mark.anyio
async def test_mcp_adapter_discovers_tools_from_all_servers():
    tools = await _load_mcp_tools()
    tools_by_name = {tool.name: tool for tool in tools}

    assert set(tools_by_name) == {
        "clinic_operations_list_doctors",
        "clinic_operations_get_queue_status",
        "clinic_operations_get_procedure_status",
        "clinic_operations_get_doctor_schedule",
        "clinic_appointments_list_available_appointments",
        "clinic_appointments_book_appointment",
        "clinic_knowledge_list_knowledge_documents",
        "clinic_knowledge_search_clinic_knowledge",
        "clinic_knowledge_read_knowledge_document",
    }

    result = await tools_by_name["clinic_operations_get_queue_status"].ainvoke(
        {"doctor_id": "dr-lee", "patient_number": 21}
    )
    assert "18" in str(result)
    assert "36" in str(result)

    knowledge_result = await tools_by_name[
        "clinic_knowledge_search_clinic_knowledge"
    ].ainvoke({"query": "Are wait times guaranteed?", "max_results": 1})
    assert "queue-and-wait-times" in str(knowledge_result)
    assert "estimates, not guarantees" in str(knowledge_result)

    appointment_result = await tools_by_name[
        "clinic_appointments_list_available_appointments"
    ].ainvoke({})
    assert "Dr. Amanda Lee" in str(appointment_result)


@pytest.mark.anyio
async def test_persistent_runtime_reuses_connected_mcp_tools():
    model = ToolCallingModel()

    with patch("app.services.chat._create_model", return_value=model) as create_model:
        await initialize_chat_runtime()
        try:
            assert chat_runtime_ready() is True
            await initialize_chat_runtime()
            create_model.assert_called_once()

            with patch(
                "app.services.chat._load_mcp_tools",
                new=AsyncMock(side_effect=AssertionError("unexpected reload")),
            ):
                response = await create_chat_response(
                    "What is the queue status for Dr. Lee? My number is 21.",
                    "persistent-runtime-test",
                )
        finally:
            await shutdown_chat_runtime()

    assert response == (
        "Dr. Amanda Lee is currently serving number 18.\n\n"
        "[Source: clinic_operations_get_queue_status]"
    )
    assert chat_runtime_ready() is False
    assert model.tool_message is not None


@pytest.mark.anyio
async def test_agent_executes_model_selected_mcp_tool_and_returns_final_answer():
    model = ToolCallingModel()

    with patch("app.services.chat._create_model", return_value=model):
        response = await create_chat_response(
            "What is the queue status for Dr. Lee? My number is 21.",
            "mcp-agent-test",
        )

    assert response == (
        "Dr. Amanda Lee is currently serving number 18.\n\n"
        "[Source: clinic_operations_get_queue_status]"
    )
    assert "clinic_operations_get_queue_status" in model.bound_tool_names
    assert model.tool_message is not None
    assert model.tool_message.name == "clinic_operations_get_queue_status"
    assert "18" in str(model.tool_message.content)


@pytest.mark.anyio
async def test_agent_chains_tool_calls_across_both_mcp_servers():
    model = MultiServerToolCallingModel()

    with patch("app.services.chat._create_model", return_value=model):
        response = await create_chat_response(
            "Are wait estimates guaranteed, and what is Dr. Lee's queue status?",
            "multi-server-agent-test",
        )

    assert response == (
        "Wait times are estimates. Dr. Amanda Lee is currently serving number 18."
        "\n\n[Source: clinic_knowledge_search_clinic_knowledge; "
        "queue-and-wait-times; clinic_operations_get_queue_status]"
    )
    assert "clinic_knowledge_search_clinic_knowledge" in model.bound_tool_names
    assert "clinic_operations_get_queue_status" in model.bound_tool_names
    assert [message.name for message in model.tool_messages] == [
        "clinic_knowledge_search_clinic_knowledge",
        "clinic_operations_get_queue_status",
    ]
    assert "queue-and-wait-times" in str(model.tool_messages[0].content)
    assert "18" in str(model.tool_messages[1].content)


@pytest.mark.anyio
async def test_agent_executes_independent_tool_calls_in_one_parallel_step():
    model = ParallelMultiToolCallingModel()

    with patch("app.services.chat._create_model", return_value=model):
        response = await create_chat_response(
            "What is Dr. Chen's procedure status, and are estimates guaranteed?",
            "parallel-tools-agent-test",
        )

    assert response == (
        "The procedure is in progress, and estimates are not guaranteed."
        "\n\n[Source: clinic_knowledge_search_clinic_knowledge; "
        "procedure-status; clinic_operations_get_procedure_status]"
    )
    assert model.call_count == 2
    assert {message.name for message in model.tool_messages} == {
        "clinic_knowledge_search_clinic_knowledge",
        "clinic_operations_get_procedure_status",
    }


@pytest.mark.anyio
async def test_agent_can_answer_without_calling_tools_when_none_are_needed():
    model = NoToolModel()

    with patch("app.services.chat._create_model", return_value=model):
        response = await create_chat_response("Hello!", "no-tool-agent-test")

    assert response == "Hello! How can I help you today?"
    assert len(model.bound_tool_names) == 9
    assert model.call_count == 1


def test_verified_sources_replace_model_citations_and_ignore_prior_turns():
    messages = [
        HumanMessage(content="Earlier question"),
        ToolMessage(
            content='{"doctor_id": "dr-lee"}',
            name="clinic_operations_get_queue_status",
            tool_call_id="old-call",
        ),
        HumanMessage(content="Current question"),
        ToolMessage(
            content='{"matches": [{"document_id": "procedure-status"}]}',
            name="clinic_knowledge_search_clinic_knowledge",
            tool_call_id="knowledge-call",
        ),
        ToolMessage(
            content='{"doctor_id": "dr-chen"}',
            name="clinic_operations_get_procedure_status",
            tool_call_id="procedure-call",
        ),
    ]

    answer = _answer_with_verified_sources(
        "In progress. [Source: invented-id]\nSource: [dr-chen procedure status]",
        messages,
    )

    assert answer == (
        "In progress.\n\n[Source: clinic_knowledge_search_clinic_knowledge; "
        "procedure-status; clinic_operations_get_procedure_status]"
    )


@pytest.mark.anyio
async def test_agent_returns_safe_message_for_tool_execution_failure(caplog):
    model = FailingToolModel(unavailable_clinic_tool.name)

    with (
        patch("app.services.chat._create_model", return_value=model),
        patch(
            "app.services.chat._load_mcp_tools",
            new=AsyncMock(return_value=[unavailable_clinic_tool]),
        ),
        caplog.at_level(logging.ERROR, logger="app.services.chat"),
    ):
        response = await create_chat_response(
            "Use the unavailable clinic tool.",
            "tool-failure-test",
        )

    assert response == (
        "MCP tool execution failed. Verify the request and try again."
    )
    assert "MCP tool execution failed (error_type=RuntimeError)" in caplog.text
    assert "internal tool details" not in caplog.text
    assert model.tool_message.status == "error"


@pytest.mark.anyio
async def test_agent_returns_safe_message_for_malformed_mcp_response(caplog):
    model = FailingToolModel(malformed_clinic_tool.name)

    with (
        patch("app.services.chat._create_model", return_value=model),
        patch(
            "app.services.chat._load_mcp_tools",
            new=AsyncMock(return_value=[malformed_clinic_tool]),
        ),
        caplog.at_level(logging.ERROR, logger="app.services.chat"),
    ):
        response = await create_chat_response(
            "Use the malformed clinic tool.",
            "malformed-response-test",
        )

    assert response == (
        "MCP tool returned an invalid or schema-incompatible response."
    )
    assert "invalid response (error_type=JSONDecodeError)" in caplog.text
    assert "private malformed payload" not in caplog.text
    assert model.tool_message.status == "error"
