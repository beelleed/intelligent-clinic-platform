import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain.mcp import MCPAdapter
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import ValidationError


load_dotenv()


LOGGER = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MCP_CONFIG_PATH = PROJECT_ROOT / "mcp_config.json"
SYSTEM_PROMPT = """You are an intelligent clinic operations and knowledge assistant.

Follow these rules:
1. For clinic-specific facts, policies, schedules, queue status, procedure status,
   and appointments, use the available MCP tools. Treat tool results as the sole
   source of truth.
2. Never add causes, rules, warnings, promises, or recommended actions that are
   not explicitly supported by the tool results.
3. When a clinic knowledge tool returns a document_id, cite it in the final
   answer as [Source: document_id].
4. For operational data, identify the supporting tool in the final answer, for
   example [Source: clinic_operations_get_queue_status].
5. If the tools do not provide enough information, say that the available clinic
   information is insufficient. Do not guess.
6. Do not provide medical diagnosis or treatment advice. Direct urgent medical
   concerns to qualified medical professionals or emergency services.
7. You may answer ordinary greetings and explain your capabilities without tools.
8. Operational and appointment tool timestamps use the timezone supplied in the
   tool result (normally America/Los_Angeles). Describe them as Pacific Time and
   do not relabel them as UTC.
Keep answers concise, clear, and faithful to the retrieved evidence."""


class LLMConfigurationError(RuntimeError):
    """Raised when required language-model configuration is missing."""


class LLMRequestError(RuntimeError):
    """Raised when a language-model request cannot produce a response."""


class MCPConnectionError(RuntimeError):
    """Raised when MCP tools cannot be discovered or reached."""


def _required_environment_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise LLMConfigurationError(f"LLM configuration error: {name} is not set.")
    return value


def _create_model() -> ChatOpenAI:
    api_key = _required_environment_value("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-5"
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None

    return ChatOpenAI(model=model, api_key=api_key, base_url=base_url)


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
    async def call_model(state: MessagesState) -> dict[str, list[AIMessage]]:
        model_with_tools = model.bind_tools(tools) if tools else model
        messages = [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
        message = await model_with_tools.ainvoke(messages)
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


async def create_chat_response(query: str, session_id: str) -> str:
    """Send a query through LangGraph with session-scoped memory."""
    config = {"configurable": {"thread_id": session_id}}

    try:
        model = _create_model()
        tools = await _load_mcp_tools()
        chat_graph = _build_chat_graph(model, tools)
        state = await chat_graph.ainvoke(
            {"messages": [HumanMessage(content=query)]},
            config=config,
        )
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

    return message.content
