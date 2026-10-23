"""Focused tests for LangGraph agent integration with real MCP servers."""

import json
import logging
import sys

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from app.services.chat import (
    SUPERVISOR_PROMPT,
    SYSTEM_PROMPT,
    _answer_with_verified_sources,
    _build_chat_graph,
    _build_supervisor_graph,
    _procedure_followup_question,
    _doctor_question,
    _load_mcp_tools,
    _required_specialists,
    _supervisor_answer_ready_tools,
    chat_runtime_ready,
    create_chat_response,
    initialize_chat_runtime,
    shutdown_chat_runtime,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_minimum_routes_cover_cross_domain_questions_without_matching_patient_as_ent():
    assert _required_specialists(
        "Is there a Dermatology appointment, where does the doctor work, "
        "and does listing a slot mean I am booked?"
    ) == ("operations", "appointments", "policy")
    assert _required_specialists(
        "Which doctors provide Dermatology and ENT, and where are their schedules?"
    ) == ("operations",)
    assert _required_specialists("What number is Dr. Lee serving?") == ("operations",)
    assert _required_specialists(
        "Dr. Lee is serving 18. What if I miss my number?"
    ) == ("operations", "policy")
    assert _doctor_question("I am a patient with a policy question") is False


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["single", "supervisor"])
async def test_procedure_followup_fetches_again_even_when_model_uses_old_answer(mode):
    reads = []

    @tool("clinic_operations_list_doctors")
    def directory() -> dict:
        """Return the verified directory."""
        return {"doctors": [{"id": "dr-chen", "name": "Dr. Michael Chen",
                             "specialty": "General Surgery"}]}

    @tool("clinic_operations_get_procedure_status")
    def procedure(doctor_id: str) -> dict:
        """Return a newly read procedure snapshot."""
        reads.append(doctor_id)
        return {"doctor_id": doctor_id, "doctor_name": "Dr. Michael Chen",
                "status": "in_progress", "estimated_remaining_minutes": 45 if len(reads) == 1 else 43,
                "updated_at": f"2026-10-09T16:0{len(reads)}:00-07:00"}

    @tool("clinic_operations_get_queue_status")
    def queue(doctor_id: str) -> dict:
        """Return queue information."""
        return {}

    @tool("clinic_knowledge_search_clinic_knowledge")
    def knowledge(query: str) -> dict:
        """Search public policies."""
        return {"matches": []}

    @tool("clinic_appointments_list_available_appointments")
    def appointments(doctor_id: str) -> dict:
        """Return appointment availability."""
        return {"slots": []}

    class StaleAnswerModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="I cannot refresh the status; previous remaining time was 45 minutes.")

    model = StaleAnswerModel()
    tools = [directory, procedure, queue, knowledge, appointments]
    graph = (_build_chat_graph(model, tools) if mode == "single"
             else _build_supervisor_graph(model, tools))
    config = {"configurable": {"thread_id": f"fresh-procedure-{mode}"}}
    await graph.ainvoke({"messages": [HumanMessage(
        content="What is Dr. Chen's current procedure status?"
    )]}, config=config)
    state = await graph.ainvoke({"messages": [HumanMessage(
        content="Please check again. How many minutes remain now?"
    )]}, config=config)
    answer = _answer_with_verified_sources(state["messages"][-1].content, state["messages"])
    assert reads == ["dr-chen", "dr-chen"]
    assert "43 minutes" in answer
    assert "16:02:00" in answer
    assert "cannot refresh" not in answer
    assert "not guaranteed" in answer
    assert answer.endswith("[Source: clinic_operations_get_procedure_status]")


def test_procedure_followup_does_not_guess_context_or_use_failed_refresh():
    followup = HumanMessage(content="Please check again. How many minutes remain now?")
    assert _procedure_followup_question({"messages": [followup]}) is None
    previous = ToolMessage(
        content=json.dumps({"doctor_id": "dr-chen", "doctor_name": "Dr. Michael Chen",
                            "estimated_remaining_minutes": 45}),
        name="clinic_operations_get_procedure_status", tool_call_id="previous",
    )
    history = [HumanMessage(content="Dr. Chen's procedure status?"), previous, followup]
    assert "dr-chen" in _procedure_followup_question({"messages": history})
    answer = _answer_with_verified_sources("Still 45 minutes.", history)
    assert "could not verify refreshed" in answer
    assert "45 minutes" not in answer
    assert "[Source:" not in answer
    refreshed = ToolMessage(
        content=json.dumps({"doctor_id": "dr-chen", "doctor_name": "Dr. Michael Chen",
                            "status": "available", "estimated_remaining_minutes": None,
                            "updated_at": "2026-10-09T16:05:00-07:00"}),
        name="clinic_operations_get_procedure_status", tool_call_id="refreshed",
    )
    refreshed_answer = _answer_with_verified_sources("Still 45 minutes.", [*history, refreshed])
    assert "status: available" in refreshed_answer
    assert "No remaining-time estimate" in refreshed_answer
    assert "45 minutes" not in refreshed_answer
    history.insert(2, HumanMessage(content="Where can I park?"))
    assert _procedure_followup_question({"messages": history}) is None


@pytest.mark.parametrize("question", [
    "Extend Dr. Chen's procedure by 30 minutes and publish the update.",
    "Can you publish a revised procedure estimate?",
])
def test_unsupported_staff_actions_do_not_offer_execution_choices(question):
    answer = _answer_with_verified_sources(
        "Choose option 1 and I will extend the estimate and publish a note.",
        [HumanMessage(content=question)],
    )
    assert "no staff update or publishing tools" in answer
    assert "no update was published" in answer
    assert "option 1" not in answer


def test_staff_procedure_policy_question_remains_answerable():
    answer = "Authorized staff can update estimates through the clinic's own system."
    assert _answer_with_verified_sources(answer, [HumanMessage(
        content="Can staff extend a procedure estimate?"
    )]) == answer


def test_supervisor_stops_reconsulting_after_queue_and_missed_policy_evidence():
    question = (
        "Dr. Lee is serving 18 and I have ticket 21. How many patients are "
        "ahead of me, and what is the clinic policy if I miss my number?"
    )
    assert _supervisor_answer_ready_tools(question) == (
        "consult_operations_agent", "consult_policy_agent",
    )
    assert _supervisor_answer_ready_tools("What are your clinic hours?") == (
        "consult_operations_agent", "consult_appointments_agent",
        "consult_policy_agent",
    )


@pytest.mark.anyio
async def test_supervisor_reads_known_missed_number_document_directly(monkeypatch):
    monkeypatch.setenv("CLINIC_AGENT_MODE", "supervisor")
    model = SupervisorRoutingModel()
    question = (
        "Dr. Lee is serving 18 and I have ticket 21. How many patients are "
        "ahead of me, and what is the clinic policy if I miss my number?"
    )

    with patch("app.services.chat._create_model", return_value=model):
        await initialize_chat_runtime()
        try:
            response = await create_chat_response(
                question, "supervisor-missed-number-direct-test",
            )
        finally:
            await shutdown_chat_runtime()

    assert "cannot confirm whether ticket 21" in response
    assert "ask the front desk how to rejoin" in response
    assert "queue-and-wait-times" in response
    policy_reports = [
        report for report in model.manager_reports
        if "queue-and-wait-times" in report["verified_sources"]
    ]
    assert len(policy_reports) == 1
    assert policy_reports[0]["verified_data"][0]["result"]["document_id"] == (
        "queue-and-wait-times"
    )


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
    monkeypatch.setenv("CLINIC_AGENT_MODE", "single")
    monkeypatch.setenv("CLINIC_PROVIDER_ROUTING", "openai")
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
            message for message in messages
            if isinstance(message, ToolMessage)
            and message.name != "clinic_operations_list_doctors"
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
            message for message in messages
            if isinstance(message, ToolMessage)
            and message.name != "clinic_operations_list_doctors"
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
            message for message in messages
            if isinstance(message, ToolMessage)
            and message.name != "clinic_operations_list_doctors"
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


class PrefetchedEvidenceModel:
    """Inspect evidence that must exist before the first model call."""

    def __init__(self):
        self.evidence = {}

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.evidence = {
            message.name: str(message.content)
            for message in messages if isinstance(message, ToolMessage)
        }
        return AIMessage(content="The requested clinic evidence was retrieved.")


class FailingToolModel:
    """Model double that exposes a handled tool failure as its final answer."""

    def __init__(self, tool_name):
        self.tool_name = tool_name
        self.tool_message = None

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        tool_messages = [
            message for message in messages
            if isinstance(message, ToolMessage)
            and message.name != "clinic_operations_list_doctors"
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


class SupervisorRoutingModel:
    """Deterministic manager and three specialist model doubles."""

    def __init__(self):
        self.bound_tool_sets = []
        self.manager_reports = []

    def bind_tools(self, tools, **kwargs):
        names = {item.name for item in tools}
        self.bound_tool_sets.append(names)
        return _BoundSupervisorRoutingModel(names, self)


class _BoundSupervisorRoutingModel:
    def __init__(self, names, owner):
        self.names = names
        self.owner = owner

    async def ainvoke(self, messages):
        tool_messages = [
            message for message in messages
            if isinstance(message, ToolMessage)
            and message.name != "clinic_operations_list_doctors"
        ]
        if "consult_policy_agent" in self.names:
            if not tool_messages:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": f"consult_{name}_agent",
                            "args": {"question": "Check the synthetic outpatient question."},
                            "id": f"consult-{name}",
                            "type": "tool_call",
                        }
                        for name in ("policy", "operations", "appointments")
                    ],
                )
            self.owner.manager_reports = [
                json.loads(message.content) for message in tool_messages
            ]
            return AIMessage(
                content="The clinic has documented services, doctors, and sample slots."
            )
        if not tool_messages:
            if "clinic_knowledge_search_clinic_knowledge" in self.names:
                name = "clinic_knowledge_search_clinic_knowledge"
                args = {"query": "specialties and locations", "max_results": 1}
            elif "clinic_appointments_list_available_appointments" in self.names:
                name = "clinic_appointments_list_available_appointments"
                args = {"doctor_id": "dr-rivera"}
            else:
                name = "clinic_operations_get_doctor_schedule"
                args = {"doctor_id": "dr-rivera"}
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": args, "id": f"call-{name}", "type": "tool_call"}
                ],
            )
        return AIMessage(content="Verified specialist finding.")


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
    assert "cannot be confirmed" in str(result)
    assert "patients_ahead" not in str(result)

    knowledge_result = await tools_by_name[
        "clinic_knowledge_read_knowledge_document"
    ].ainvoke({"document_id": "queue-and-wait-times"})
    assert "queue-and-wait-times" in str(knowledge_result)
    assert "Their difference does not tell" in str(knowledge_result)
    assert "ask the front desk how to rejoin" in str(knowledge_result)

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

    assert "Dr. Amanda Lee: now serving number 18" in response
    assert "cannot confirm whether ticket 21" in response
    assert "does not tell us how many patients are ahead" in response
    assert response.endswith("[Source: clinic_operations_get_queue_status]")
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

    assert "Dr. Amanda Lee: now serving number 18" in response
    assert "cannot confirm whether ticket 21" in response
    assert "does not tell us how many patients are ahead" in response
    assert response.endswith("[Source: clinic_operations_get_queue_status]")
    assert "clinic_operations_get_queue_status" in model.bound_tool_names
    assert model.tool_message is not None
    assert model.tool_message.name == "clinic_operations_get_queue_status"
    assert "18" in str(model.tool_message.content)


@pytest.mark.anyio
async def test_local_evaluation_collects_model_usage_without_exposing_it_in_answer():
    class UsageModel(NoToolModel):
        model_name = "test-model"

        async def ainvoke(self, messages):
            return AIMessage(
                content="Hello from the clinic.",
                usage_metadata={
                    "input_tokens": 12, "output_tokens": 5, "total_tokens": 17,
                },
            )

    usage = []
    with patch("app.services.chat._create_model", return_value=UsageModel()):
        answer = await create_chat_response(
            "Hello", "usage-local-eval-test", usage_sink=usage
        )
    assert answer == "Hello from the clinic."
    assert len(usage) == 1
    assert {key: value for key, value in usage[0].items() if key != "duration_ms"} == {
        "agent": "single", "provider": "openai", "model": "test-model",
        "input_tokens": 12, "output_tokens": 5, "total_tokens": 17,
        "input_token_details": None,
    }
    assert usage[0]["duration_ms"] >= 0


@pytest.mark.anyio
async def test_agent_chains_tool_calls_across_both_mcp_servers():
    model = MultiServerToolCallingModel()

    with patch("app.services.chat._create_model", return_value=model):
        response = await create_chat_response(
            "Please combine clinic knowledge and operational evidence.",
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
            "Please combine two independent clinic tools.",
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


@pytest.mark.anyio
async def test_single_agent_prefetches_schedule_slots_and_policy_for_mixed_question():
    model = PrefetchedEvidenceModel()
    with patch("app.services.chat._create_model", return_value=model):
        response = await create_chat_response(
            "Is there a Dermatology appointment available, where does the doctor "
            "normally work, and does listing a slot mean I am booked?",
            "single-prefetch-cross-domain-test",
        )

    assert "dr-rivera" in model.evidence["clinic_operations_list_doctors"]
    assert "Specialty Suite" in model.evidence[
        "clinic_operations_get_doctor_schedule"
    ]
    assert "dr-rivera" in model.evidence[
        "clinic_appointments_list_available_appointments"
    ]
    assert model.evidence["clinic_knowledge_search_clinic_knowledge"]
    assert "not confirmed" in model.evidence[
        "clinic_knowledge_read_knowledge_document"
    ]
    assert "clinic_operations_get_doctor_schedule" in response
    assert "clinic_appointments_list_available_appointments" in response


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


def test_queue_only_answer_uses_verified_snapshot_instead_of_model_arithmetic():
    snapshot = {
        "doctor_id": "dr-lee", "doctor_name": "Dr. Amanda Lee",
        "current_number": 18, "last_issued_number": 24,
        "updated_at": "2026-10-05T15:00:00-07:00",
    }
    messages = [
        HumanMessage(content="Dr. Lee is serving 18 and I hold 21. Can you prove exactly three patients are ahead of me?"),
        ToolMessage(
            content=json.dumps(snapshot),
            name="clinic_operations_get_queue_status",
            tool_call_id="queue-call",
        ),
    ]
    answer = _answer_with_verified_sources("Yes, three people are ahead of you.", messages)
    assert "cannot confirm whether ticket 21" in answer
    assert "does not tell us how many patients are ahead" in answer
    assert "three people" not in answer
    assert answer.endswith("[Source: clinic_operations_get_queue_status]")

    messages[0] = HumanMessage(content="For Dr. Lee, my ticket is 28. What is my actual wait?")
    answer = _answer_with_verified_sources("Ticket 28 is in the queue.", messages)
    assert "Ticket 28 is above the last issued number" in answer
    assert "Ticket 28 is in the queue" not in answer


@pytest.mark.parametrize("nested", [False, True])
def test_unknown_doctor_queue_rejects_speculation_and_invented_number(nested):
    directory = {"doctors": [{
        "id": "dr-chen", "name": "Dr. Michael Chen", "specialty": "General Surgery",
    }]}
    content = directory
    tool_name = "clinic_operations_list_doctors"
    if nested:
        content = {
            "verified_sources": [tool_name],
            "verified_data": [{"tool": tool_name, "result": directory}],
        }
        tool_name = "consult_operations_agent"
    messages = [
        HumanMessage(content="What number is Dr. Patel serving at this clinic?"),
        ToolMessage(content=json.dumps(content), name=tool_name, tool_call_id="directory"),
    ]
    answer = _answer_with_verified_sources(
        "Dr. Patel is serving 12. He may be listed under another name. Patel.", messages,
    )
    assert "not listed in the verified clinic doctor directory" in answer
    assert "No queue number is available" in answer
    assert "Dr. Michael Chen" in answer
    assert "serving 12" not in answer
    assert "another name" not in answer
    assert answer.endswith("[Source: clinic_operations_list_doctors]")


def test_unknown_doctor_queue_requires_current_valid_directory():
    question = HumanMessage(content="What number is Dr. Patel serving?")
    directory = ToolMessage(
        content=json.dumps({"doctors": [{
            "id": "dr-patel", "name": "Dr. Priya Patel", "specialty": "Family Medicine",
        }]}),
        name="clinic_operations_list_doctors", tool_call_id="directory",
    )
    answer = "The queue status has not been retrieved."
    for messages in ([question], [directory, question], [question, directory]):
        assert "not listed" not in _answer_with_verified_sources(answer, messages)
    malformed = ToolMessage(
        content='{"doctors": [{"id": "dr-chen"}]}',
        name="clinic_operations_list_doctors", tool_call_id="invalid-directory",
    )
    assert "not listed" not in _answer_with_verified_sources(answer, [question, malformed])


def test_supervisor_queue_answer_uses_nested_verified_snapshot():
    snapshot = {
        "doctor_id": "dr-lee", "doctor_name": "Dr. Amanda Lee",
        "current_number": 18, "last_issued_number": 24,
        "updated_at": "2026-10-05T15:00:00-07:00",
    }
    report = {
        "verified_sources": ["clinic_operations_get_queue_status"],
        "verified_data": [{
            "tool": "clinic_operations_get_queue_status",
            "result": json.dumps(snapshot),
        }],
    }
    messages = [
        HumanMessage(content="Dr. Lee is serving 18 and I hold 21. How many people are ahead?"),
        ToolMessage(
            content=json.dumps(report),
            name="consult_operations_agent",
            tool_call_id="consult-call",
        ),
    ]
    answer = _answer_with_verified_sources("Exactly two people are ahead.", messages)
    assert "cannot confirm whether ticket 21" in answer
    assert "Exactly two" not in answer
    assert answer.endswith("[Source: clinic_operations_get_queue_status]")


def test_mixed_queue_and_missed_number_policy_uses_verified_document():
    snapshot = {
        "doctor_id": "dr-lee", "doctor_name": "Dr. Amanda Lee",
        "current_number": 18, "last_issued_number": 24,
        "updated_at": "2026-10-05T15:00:00-07:00",
    }
    document = {
        "document_id": "queue-and-wait-times",
        "title": "Queue and Wait Times",
        "content": (
            "# Queue and Wait Times\n\n## Missed number\n\n"
            "If you miss your number, ask the front desk how to rejoin the queue. "
            "Clinic staff decide whether to call the number again or reschedule."
        ),
    }
    question = (
        "Dr. Lee is serving 18 and I have ticket 21. How many patients are "
        "ahead of me, and what is the clinic policy if I miss my number?"
    )
    messages = [
        HumanMessage(content=question),
        ToolMessage(
            content=json.dumps(snapshot),
            name="clinic_operations_get_queue_status",
            tool_call_id="queue-call",
        ),
        ToolMessage(
            content=json.dumps(document),
            name="clinic_knowledge_read_knowledge_document",
            tool_call_id="policy-call",
        ),
    ]
    answer = _answer_with_verified_sources(
        "Three people are ahead, and we automatically issue a new ticket.", messages,
    )
    assert "cannot confirm whether ticket 21" in answer
    assert "ask the front desk how to rejoin" in answer
    assert "Three people" not in answer
    assert "automatically issue" not in answer
    assert answer.endswith(
        "[Source: clinic_operations_get_queue_status; "
        "clinic_knowledge_read_knowledge_document; queue-and-wait-times]"
    )

    messages[2] = ToolMessage(
        content=json.dumps({
            "verified_sources": ["clinic_knowledge_read_knowledge_document"],
            "verified_data": [{
                "tool": "clinic_knowledge_read_knowledge_document",
                "result": json.dumps(document),
            }],
        }),
        name="consult_policy_agent",
        tool_call_id="consult-call",
    )
    assert "ask the front desk how to rejoin" in _answer_with_verified_sources(
        "We automatically issue a new ticket.", messages,
    )

    messages.pop()
    answer_without_document = _answer_with_verified_sources(
        "We automatically issue a new ticket.", messages,
    )
    assert "policy could not be verified" in answer_without_document
    assert "automatically issue" not in answer_without_document
    assert answer_without_document.endswith("[Source: clinic_operations_get_queue_status]")


def test_recurring_schedule_response_includes_today_presence_caveat_once():
    messages = [
        HumanMessage(content="Where does Dr. Rivera normally work?"),
        ToolMessage(
            content='{"doctor_id": "dr-rivera", "location": "Specialty Suite"}',
            name="clinic_operations_get_doctor_schedule",
            tool_call_id="schedule-call",
        ),
    ]

    answer = _answer_with_verified_sources(
        "Dr. Rivera's recurring schedule lists Specialty Suite.", messages,
    )
    assert "Recurring schedules do not confirm a doctor's presence or location today." in answer
    assert answer.endswith("[Source: clinic_operations_get_doctor_schedule]")

    answer_with_caveat = _answer_with_verified_sources(
        "The recurring schedule does not guarantee today's presence.", messages,
    )
    assert answer_with_caveat.count("today") == 1


def test_schedule_only_answers_do_not_infer_live_location_from_other_tools():
    question = (
        "Which synthetic doctors provide Dermatology and ENT services, and "
        "where do their recurring schedules place them?"
    )
    directory = {"doctors": [
        {"id": "dr-park", "name": "Dr. Daniel Park", "specialty": "Otolaryngology (ENT)"},
        {"id": "dr-rivera", "name": "Dr. Sofia Rivera", "specialty": "Dermatology"},
    ]}
    schedules = [
        {"doctor_id": "dr-park", "schedule": [
            {"weekday": "Tuesday", "start_time": "13:00", "end_time": "17:00",
             "location": "Clinic B"},
        ]},
        {"doctor_id": "dr-rivera", "schedule": [
            {"weekday": "Monday", "start_time": "13:00", "end_time": "17:00",
             "location": "Specialty Suite"},
        ]},
    ]
    evidence = [
        {"tool": "clinic_operations_list_doctors", "result": json.dumps(directory)},
        *(
            {"tool": "clinic_operations_get_doctor_schedule", "result": json.dumps(item)}
            for item in schedules
        ),
    ]
    messages = [
        HumanMessage(content=question),
        ToolMessage(
            content=json.dumps({
                "verified_sources": [
                    "clinic_operations_list_doctors",
                    "clinic_operations_get_doctor_schedule",
                ],
                "verified_data": evidence,
            }),
            name="consult_operations_agent", tool_call_id="consult-operations",
        ),
    ]
    for ungrounded_answer in (
        "A current queue/slot view is needed to find today's location.",
        "View the current day's schedule in the clinic's live portal.",
    ):
        answer = _answer_with_verified_sources(ungrounded_answer, messages)
        assert "Dr. Sofia Rivera (Dermatology)" in answer
        assert "Monday 13:00–17:00 at Specialty Suite" in answer
        assert "Dr. Daniel Park (Otolaryngology (ENT))" in answer
        assert "Tuesday 13:00–17:00 at Clinic B" in answer
        assert "contact the clinic directly" in answer
        assert "queue/slot view" not in answer
        assert "live portal" not in answer


def test_agent_prompts_limit_booking_offers_and_today_location_inferences():
    assert "do not establish today's location" in SYSTEM_PROMPT
    assert "No available tool retrieves a doctor's actual live location" in SYSTEM_PROMPT
    assert '"Synthetic" describes this sample clinic data' in SYSTEM_PROMPT
    assert "Never offer to book, reserve, request, arrange, or confirm" in SUPERVISOR_PROMPT
    assert "Neither queue status nor available" in SUPERVISOR_PROMPT
    assert "Never offer to check or retrieve that location" in SUPERVISOR_PROMPT
    assert '"Synthetic" describes the sample dataset' in SUPERVISOR_PROMPT


def test_answer_removes_unsolicited_follow_up_offers():
    messages = [
        HumanMessage(content="Are slots available?"),
        ToolMessage(
            content='{"doctor_id": "dr-rivera"}',
            name="clinic_appointments_list_available_appointments",
            tool_call_id="slots-call",
        ),
    ]
    answer = _answer_with_verified_sources(
        "Dr. Rivera has two listed slots. Listing is not a booking.\n\n"
        "If you want, I can:\n- Retrieve her actual location today?\n"
        "- Help you request a booking?",
        messages,
    )
    assert "Dr. Rivera has two listed slots" in answer
    assert "Retrieve her actual location" not in answer
    assert "Help you request a booking" not in answer
    assert answer.endswith("[Source: clinic_appointments_list_available_appointments]")

    answer = _answer_with_verified_sources(
        "Two slots are listed. For the latest, I can fetch slots again if you'd like.\n"
        "- If you want to proceed, I can place a booking request.\n"
        "Listing does not confirm a booking.",
        messages,
    )
    assert "fetch slots again" not in answer
    assert "place a booking request" not in answer
    assert "Listing does not confirm a booking." in answer

    answer = _answer_with_verified_sources(
        "Recurring schedules do not confirm today's location. "
        "If you’d like, I can fetch current queue/status or look for slots.\n"
        "- If you need today's location, I can check that in read-only mode.",
        messages,
    )
    assert "Recurring schedules do not confirm today's location." in answer
    assert "fetch current queue" not in answer
    assert "check that in read-only mode" not in answer


@pytest.mark.parametrize("nested", [False, True])
def test_empty_appointment_lookup_is_not_confused_with_listed_doctor(nested):
    question = (
        "Is there a Dermatology appointment available, where does that doctor "
        "normally work, and does viewing a slot mean my appointment is confirmed?"
    )
    evidence = [
        ("clinic_operations_list_doctors", {"doctors": [{
            "id": "dr-rivera", "name": "Dr. Sofia Rivera", "specialty": "Dermatology",
        }]}),
        ("clinic_operations_get_doctor_schedule", {
            "doctor_id": "dr-rivera", "doctor_name": "Dr. Sofia Rivera",
            "schedule": [{"weekday": "Thursday", "start_time": "09:00",
                          "end_time": "12:00", "location": "Specialty Suite"}],
        }),
        ("clinic_appointments_list_available_appointments", {"slots": []}),
    ]
    messages = [HumanMessage(content=question)]
    if nested:
        messages.append(ToolMessage(
            content=json.dumps({
                "verified_sources": [name for name, _ in evidence],
                "verified_data": [{"tool": name, "result": result}
                                  for name, result in evidence],
            }),
            name="consult_appointments_agent", tool_call_id="consult-slots",
        ))
    else:
        messages.extend(ToolMessage(
            content=json.dumps(result), name=name, tool_call_id=name,
        ) for name, result in evidence)
    answer = _answer_with_verified_sources(
        "Dermatology appointment availability: Yes, Dr. Rivera is listed.", messages,
    )
    assert answer.startswith("No available future appointment slots are listed")
    assert "Dr. Sofia Rivera (Dermatology)" in answer
    assert "Thursday 09:00–12:00 at Specialty Suite" in answer
    assert "Viewing a slot does not book it" in answer
    assert "do not confirm a doctor's presence" in answer
    assert "availability: Yes" not in answer


def test_empty_appointment_answer_requires_successful_current_lookup():
    directory = ToolMessage(
        content=json.dumps({"doctors": [{
            "id": "dr-rivera", "name": "Dr. Sofia Rivera", "specialty": "Dermatology",
        }]}), name="clinic_operations_list_doctors", tool_call_id="directory",
    )
    question = HumanMessage(content="Is a Dermatology appointment available?")
    empty_lookup = ToolMessage(
        content='{"slots": []}', name="clinic_appointments_list_available_appointments",
        tool_call_id="slots",
    )
    failed_lookup = ToolMessage(
        content='{"slots": []}', name="clinic_appointments_list_available_appointments",
        tool_call_id="failed-slots", status="error",
    )
    for messages in (
        [question, directory],
        [empty_lookup, question, directory],
        [question, directory, failed_lookup],
    ):
        answer = _answer_with_verified_sources("Availability could not be verified.", messages)
        assert "No available future appointment slots" not in answer


def test_supervisor_corrects_denied_slots_using_only_verified_tool_data():
    directory = {
        "doctors": [
            {"id": "dr-rivera", "name": "Dr. Sofia Rivera", "specialty": "Dermatology"},
            {"id": "dr-park", "name": "Dr. Daniel Park", "specialty": "ENT"},
        ],
    }
    schedule = {
        "doctor_id": "dr-rivera", "doctor_name": "Dr. Sofia Rivera",
        "schedule": [
            {"weekday": "Thursday", "start_time": "09:00", "end_time": "12:00",
             "location": "Specialty Suite"},
        ],
    }
    appointments = {
        "slots": [
            {"slot_id": 5, "doctor_id": "dr-rivera",
             "doctor_name": "Dr. Sofia Rivera", "appointment_date": "2026-10-08",
             "start_time": "09:00", "end_time": "09:30", "timezone": "America/Los_Angeles"},
        ],
    }
    report = {
        "verified_sources": [
            "clinic_operations_list_doctors",
            "clinic_operations_get_doctor_schedule",
            "clinic_appointments_list_available_appointments",
        ],
        "verified_data": [
            {"tool": "clinic_operations_list_doctors", "result": json.dumps(directory)},
            {"tool": "clinic_operations_get_doctor_schedule", "result": json.dumps(schedule)},
            {"tool": "clinic_operations_get_doctor_schedule", "result": json.dumps(schedule)},
            {"tool": "clinic_appointments_list_available_appointments",
             "result": json.dumps([{"type": "text", "text": json.dumps(appointments)}])},
        ],
    }
    messages = [
        HumanMessage(content=(
            "Is there a synthetic Dermatology appointment available, where does "
            "the doctor normally work, and would listing a slot mean I am booked?"
        )),
        ToolMessage(
            content=json.dumps(report), name="consult_appointments_agent",
            tool_call_id="consult-appointments",
        ),
    ]
    answer = _answer_with_verified_sources(
        "The slots are not provided here unless requested.", messages,
    )
    assert "Dr. Sofia Rivera: 2026-10-08 09:00–09:30 Pacific Time" in answer
    assert "Thursday 09:00–12:00 at Specialty Suite" in answer
    assert answer.count("Thursday 09:00–12:00 at Specialty Suite") == 1
    assert "Viewing a slot does not book it" in answer
    assert "Recurring schedules do not confirm" in answer
    assert "slots are not provided" not in answer
    assert "Dr. Daniel Park" not in answer


def test_correct_slot_answer_is_not_replaced():
    messages = [
        HumanMessage(content="Are Dermatology appointments available?"),
        ToolMessage(
            content=json.dumps({"doctors": [
                {"id": "dr-rivera", "name": "Dr. Sofia Rivera",
                 "specialty": "Dermatology"},
            ]}),
            name="clinic_operations_list_doctors", tool_call_id="directory",
        ),
        ToolMessage(
            content=json.dumps({"slots": [
                {"slot_id": 5, "doctor_id": "dr-rivera",
                 "doctor_name": "Dr. Sofia Rivera",
                 "appointment_date": "2026-10-08", "start_time": "09:00",
                 "end_time": "09:30"},
            ]}),
            name="clinic_appointments_list_available_appointments",
            tool_call_id="slots",
        ),
    ]
    answer = _answer_with_verified_sources(
        "Yes. Dr. Rivera has a slot on 2026-10-08 at 09:00.", messages,
    )
    assert answer.startswith("Yes. Dr. Rivera has a slot")


def test_empty_knowledge_search_is_not_cited_as_evidence():
    messages = [
        HumanMessage(content="Question with no matching clinic policy"),
        ToolMessage(
            content='{"matches": []}',
            name="clinic_knowledge_search_clinic_knowledge",
            tool_call_id="empty-search",
        ),
    ]

    assert _answer_with_verified_sources(
        "No policy was found. [Source: invented-policy]", messages
    ) == "No policy was found."


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


@pytest.mark.anyio
async def test_supervisor_delegates_to_three_scoped_agents(monkeypatch):
    monkeypatch.setenv("CLINIC_AGENT_MODE", "supervisor")
    model = SupervisorRoutingModel()

    with patch("app.services.chat._create_model", return_value=model):
        await initialize_chat_runtime()
        try:
            response = await create_chat_response(
                "Which specialty has an appointment, where is it, and does listing a slot mean I am booked?",
                "supervisor-three-specialists-test",
            )
        finally:
            await shutdown_chat_runtime()

    assert "The clinic has documented services" in response
    assert "clinic_knowledge_search_clinic_knowledge" in response
    assert "clinic_operations_list_doctors" in response
    assert "clinic_operations_get_doctor_schedule" in response
    assert "clinic_appointments_list_available_appointments" in response
    assert any(
        item["tool"] == "clinic_appointments_list_available_appointments"
        and "dr-rivera" in str(item["result"])
        for report in model.manager_reports
        for item in report["verified_data"]
    )
    assert "clinic_appointments_book_appointment" not in set.union(
        *model.bound_tool_sets
    )
    assert len(model.bound_tool_sets) == 4
    assert {"consult_policy_agent", "consult_operations_agent", "consult_appointments_agent"} in model.bound_tool_sets
    assert {
        "clinic_knowledge_list_knowledge_documents",
        "clinic_knowledge_search_clinic_knowledge",
        "clinic_knowledge_read_knowledge_document",
    } in model.bound_tool_sets
    assert {
        "clinic_operations_list_doctors",
        "clinic_operations_get_queue_status",
        "clinic_operations_get_procedure_status",
        "clinic_operations_get_doctor_schedule",
    } in model.bound_tool_sets
    assert {
        "clinic_operations_list_doctors",
        "clinic_appointments_list_available_appointments",
    } in model.bound_tool_sets


@pytest.mark.anyio
async def test_domain_routing_uses_nvidia_only_for_policy_specialist(monkeypatch):
    monkeypatch.setenv("CLINIC_AGENT_MODE", "supervisor")
    monkeypatch.setenv("CLINIC_PROVIDER_ROUTING", "domain")
    openai_model = SupervisorRoutingModel()
    nvidia_model = SupervisorRoutingModel()

    with (
        patch("app.services.chat._create_model", return_value=openai_model),
        patch("app.services.chat._create_nvidia_model", return_value=nvidia_model),
    ):
        await initialize_chat_runtime()
        try:
            response = await create_chat_response(
                "Which specialty has an appointment, where is it, and "
                "does listing a slot mean I am booked?",
                "supervisor-nvidia-policy-test",
            )
        finally:
            await shutdown_chat_runtime()

    policy_tools = {
        "clinic_knowledge_list_knowledge_documents",
        "clinic_knowledge_search_clinic_knowledge",
        "clinic_knowledge_read_knowledge_document",
    }
    assert policy_tools in nvidia_model.bound_tool_sets
    assert policy_tools not in openai_model.bound_tool_sets
    assert len(nvidia_model.bound_tool_sets) == 1
    assert len(openai_model.bound_tool_sets) == 3
    assert "clinic_knowledge_search_clinic_knowledge" in response
