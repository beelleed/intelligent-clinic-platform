import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain.mcp import MCPAdapter
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import ValidationError


load_dotenv()


LOGGER = logging.getLogger(__name__)
PERFORMANCE_LOGGER = logging.getLogger("uvicorn.error")


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MCP_CONFIG_PATH = PROJECT_ROOT / "mcp_config.json"
SYSTEM_PROMPT = """You are an intelligent clinic operations and knowledge assistant.

Follow these rules:
1. For clinic-specific facts, policies, schedules, queue status, procedure status,
   and appointments, use the available MCP tools. Treat tool results as the sole
   source of truth.
2. Never add causes, rules, warnings, promises, or recommended actions that are
   not explicitly supported by the tool results.
3. Do not write source citations yourself. The application adds verified tool
   and document citations after your answer.
4. For a ticket greater than last_issued_number, say it has not been issued.
   You may report hypothetical_queue_positions and hypothetical_wait_minutes
   only as a conditional projection if that ticket is issued and the current
   pace stays the same. Do not call these actual patients ahead or an actual
   wait estimate.
5. If the tools do not provide enough information, say that the available clinic
   information is insufficient. Do not guess.
6. Do not provide medical diagnosis or treatment advice. Direct urgent medical
   concerns to qualified medical professionals or emergency services.
7. You may answer ordinary greetings and explain your capabilities without tools.
8. Operational and appointment tool timestamps use the timezone supplied in the
   tool result (normally America/Los_Angeles). Describe them as Pacific Time and
   do not relabel them as UTC.
9. When a question needs multiple independent tools, request all of those tool
   calls in the same response so they can run in parallel. Do not wait for one
   independent result before requesting the next tool.
Keep answers concise, clear, and faithful to the retrieved evidence."""


class LLMConfigurationError(RuntimeError):
    """Raised when required language-model configuration is missing."""


class LLMRequestError(RuntimeError):
    """Raised when a language-model request cannot produce a response."""


class MCPConnectionError(RuntimeError):
    """Raised when MCP tools cannot be discovered or reached."""


SOURCE_LINE_PATTERN = re.compile(r"(?im)^\s*\[?Sources?\s*:[^\n]*$")
INLINE_SOURCE_PATTERN = re.compile(r"(?i)\[Source\s*:[^\]\n]*\]")


def _document_ids(value: Any) -> list[str]:
    """Find document IDs in structured MCP output without trusting model text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, dict):
        found = []
        document_id = value.get("document_id")
        if isinstance(document_id, str) and document_id:
            found.append(document_id)
        for item in value.values():
            found.extend(_document_ids(item))
        return found
    if isinstance(value, list):
        return [document_id for item in value for document_id in _document_ids(item)]
    return []


def _answer_with_verified_sources(answer: str, messages: list[Any]) -> str:
    """Cite successful tools used in this turn, excluding earlier session turns."""
    latest_question_index = max(
        (index for index, message in enumerate(messages)
         if isinstance(message, HumanMessage)),
        default=-1,
    )
    tool_messages = [
        message for message in messages[latest_question_index + 1:]
        if isinstance(message, ToolMessage) and message.status != "error"
    ]
    if not tool_messages:
        return answer

    sources = []
    for message in tool_messages:
        if message.name and message.name not in sources:
            sources.append(message.name)
        if message.name and message.name.startswith("clinic_knowledge_"):
            for document_id in _document_ids(message.content):
                if document_id not in sources:
                    sources.append(document_id)

    if not sources:
        return answer
    cleaned = SOURCE_LINE_PATTERN.sub("", answer)
    cleaned = INLINE_SOURCE_PATTERN.sub("", cleaned).strip()
    return f"{cleaned}\n\n[Source: {'; '.join(sources)}]"


def _required_environment_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise LLMConfigurationError(f"LLM configuration error: {name} is not set.")
    return value


def _create_model() -> ChatOpenAI:
    api_key = _required_environment_value("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-5-nano"
    reasoning_effort = (
        os.getenv("OPENAI_REASONING_EFFORT", "").strip() or "minimal"
    )
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        reasoning_effort=reasoning_effort,
    )


def _mcp_config_path() -> Path:
    configured_path = os.getenv("MCP_SERVERS_CONFIG", "").strip()
    if not configured_path:
        return DEFAULT_MCP_CONFIG_PATH

    path = Path(configured_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _load_mcp_config() -> dict:
    """Load and minimally validate the configured MCP server definitions."""
    config_path = _mcp_config_path()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MCPConnectionError(
            "MCP configuration error: the server configuration cannot be loaded."
        ) from exc

    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise MCPConnectionError(
            "MCP configuration error: mcpServers must be a non-empty object."
        )

    for server_name, server_config in servers.items():
        if not isinstance(server_config, dict):
            raise MCPConnectionError(
                f"MCP configuration error: '{server_name}' must be an object."
            )
        if server_config.get("command") == "${PYTHON_EXECUTABLE}":
            server_config["command"] = sys.executable

    return config


async def _load_mcp_tools() -> list[BaseTool]:
    """Discover LangChain-compatible tools through the real MCP protocol."""
    try:
        async with MCPAdapter(_load_mcp_config()) as adapter:
            return await adapter.list_tools()
    except MCPConnectionError:
        raise
    except Exception as exc:
        LOGGER.error(
            "MCP tool discovery failed (error_type=%s)",
            type(exc).__name__,
        )
        raise MCPConnectionError(
            "MCP connection error: clinic tools are unavailable."
        ) from exc


def _exception_chain_contains(
    error: BaseException,
    error_types: tuple[type[BaseException], ...],
) -> bool:
    """Check wrapped exceptions without exposing their messages."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, error_types):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _mcp_tool_error_message(error: Exception) -> str:
    """Log safe diagnostics and return a model-visible MCP failure message."""
    error_type = type(error).__name__
    if _exception_chain_contains(
        error,
        (json.JSONDecodeError, ValidationError),
    ):
        LOGGER.error(
            "MCP tool returned an invalid response (error_type=%s)",
            error_type,
        )
        return "MCP tool returned an invalid or schema-incompatible response."

    LOGGER.error(
        "MCP tool execution failed (error_type=%s)",
        error_type,
    )
    return "MCP tool execution failed. Verify the request and try again."


def _build_chat_graph(model: ChatOpenAI, tools: list[BaseTool]):
    model_with_tools = (
        model.bind_tools(tools, parallel_tool_calls=True) if tools else model
    )

    async def call_model(state: MessagesState) -> dict[str, list[AIMessage]]:
        messages = [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
        started_at = time.perf_counter()
        message = await model_with_tools.ainvoke(messages)
        PERFORMANCE_LOGGER.info(
            "Clinic model call completed "
            "(duration_ms=%d, message_count=%d, tool_call_count=%d)",
            round((time.perf_counter() - started_at) * 1000),
            len(messages),
            len(message.tool_calls),
        )
        return {"messages": [message]}

    builder = StateGraph(MessagesState)
    builder.add_node("model", call_model)
    builder.add_edge(START, "model")

    if tools:
        builder.add_node(
            "tools",
            ToolNode(tools, handle_tool_errors=_mcp_tool_error_message),
        )
        builder.add_conditional_edges("model", tools_condition)
        builder.add_edge("tools", "model")

    return builder.compile(checkpointer=chat_memory)


chat_memory = MemorySaver()


_runtime_adapter: MCPAdapter | None = None
_runtime_graph: Any | None = None
_runtime_invocation_lock: asyncio.Lock | None = None


def chat_runtime_ready() -> bool:
    """Return whether the process has a persistent model, graph, and MCP clients."""
    return _runtime_adapter is not None and _runtime_graph is not None


async def initialize_chat_runtime() -> None:
    """Initialize one reusable agent graph and keep its MCP clients connected."""
    global _runtime_adapter, _runtime_graph, _runtime_invocation_lock

    if chat_runtime_ready():
        return

    started_at = time.perf_counter()
    model = _create_model()
    adapter = MCPAdapter(_load_mcp_config())

    try:
        await adapter.__aenter__()
        tools = await adapter.list_tools()
        graph = _build_chat_graph(model, tools)
    except Exception:
        await adapter.__aexit__(*sys.exc_info())
        raise

    _runtime_adapter = adapter
    _runtime_graph = graph
    _runtime_invocation_lock = asyncio.Lock()
    PERFORMANCE_LOGGER.info(
        "Persistent clinic agent runtime initialized "
        "(tool_count=%d, duration_ms=%d)",
        len(tools),
        round((time.perf_counter() - started_at) * 1000),
    )


async def shutdown_chat_runtime() -> None:
    """Close persistent MCP clients during application shutdown."""
    global _runtime_adapter, _runtime_graph, _runtime_invocation_lock

    adapter = _runtime_adapter
    _runtime_adapter = None
    _runtime_graph = None
    _runtime_invocation_lock = None
    if adapter is not None:
        await adapter.__aexit__(None, None, None)
        PERFORMANCE_LOGGER.info("Persistent clinic agent runtime stopped")


async def create_chat_response(query: str, session_id: str) -> str:
    """Send a query through LangGraph with session-scoped memory."""
    config = {"configurable": {"thread_id": session_id}}
    started_at = time.perf_counter()
    using_persistent_runtime = chat_runtime_ready()

    try:
        if using_persistent_runtime:
            chat_graph = _runtime_graph
            invocation_lock = _runtime_invocation_lock
        else:
            model = _create_model()
            tools = await _load_mcp_tools()
            chat_graph = _build_chat_graph(model, tools)
            invocation_lock = None

        if chat_graph is None:
            raise RuntimeError("The clinic agent graph is unavailable.")

        input_state = {"messages": [HumanMessage(content=query)]}
        if invocation_lock is None:
            state = await chat_graph.ainvoke(input_state, config=config)
        else:
            # A connected stdio MCP client reuses its server sessions. Serialize
            # agent turns so concurrent requests cannot mix MCP session context.
            async with invocation_lock:
                state = await chat_graph.ainvoke(input_state, config=config)
    except (LLMConfigurationError, MCPConnectionError):
        raise
    except Exception as exc:
        LOGGER.error(
            "Agent model request failed (error_type=%s)",
            type(exc).__name__,
        )
        raise LLMRequestError(
            "The language model request failed. "
            "Check the model configuration and provider availability."
        ) from exc

    message = state["messages"][-1]

    if not isinstance(message.content, str) or not message.content.strip():
        raise LLMRequestError("The language model returned an invalid response.")

    PERFORMANCE_LOGGER.info(
        "Clinic agent response completed (duration_ms=%d, persistent_runtime=%s)",
        round((time.perf_counter() - started_at) * 1000),
        using_persistent_runtime,
    )
    return _answer_with_verified_sources(message.content, state["messages"])
