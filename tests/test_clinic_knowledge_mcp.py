"""Focused protocol tests for the clinic knowledge MCP server."""

import pytest
from mcp import Client

from mcp_servers.clinic_knowledge import server
from mcp_servers.clinic_knowledge.server import mcp


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_knowledge_directory(monkeypatch, tmp_path):
    knowledge_directory = tmp_path / "knowledge"
    knowledge_directory.mkdir()
    (knowledge_directory / "arrival-policy.md").write_text(
        "# Arrival Policy\n\nPatients should arrive 15 minutes early for check-in.",
        encoding="utf-8",
    )
    (knowledge_directory / "privacy-policy.md").write_text(
        "# Privacy Policy\n\nThe public board uses queue numbers, not patient names.",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLINIC_KNOWLEDGE_DIR", str(knowledge_directory))
    monkeypatch.setenv("CLINIC_KNOWLEDGE_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("CLINIC_KNOWLEDGE_SEARCH_MODE", "lexical")


@pytest.mark.anyio
async def test_tools_are_discoverable_with_valid_input_schemas():
    async with Client(mcp) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert set(tools) == {
        "list_knowledge_documents",
        "search_clinic_knowledge",
        "read_knowledge_document",
    }
    search_schema = tools["search_clinic_knowledge"].input_schema
    assert search_schema["required"] == ["query"]
    assert search_schema["properties"]["query"]["type"] == "string"
    assert search_schema["properties"]["max_results"]["default"] == 3


@pytest.mark.anyio
async def test_list_and_read_tools_use_markdown_files():
    async with Client(mcp) as client:
        listed = await client.call_tool("list_knowledge_documents", {})
        document = await client.call_tool(
            "read_knowledge_document", {"document_id": "arrival-policy"}
        )

    assert listed.is_error is False
    assert listed.structured_content["documents"] == [
        {"document_id": "arrival-policy", "title": "Arrival Policy"},
        {"document_id": "privacy-policy", "title": "Privacy Policy"},
    ]
    assert document.is_error is False
    assert "15 minutes early" in document.structured_content["content"]


@pytest.mark.anyio
async def test_search_returns_ranked_source_grounded_matches():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "search_clinic_knowledge",
            {"query": "When should patients arrive for check-in?", "max_results": 1},
        )

    assert result.is_error is False
    assert result.structured_content["matches"][0]["document_id"] == (
        "arrival-policy"
    )
    assert "15 minutes early" in result.structured_content["matches"][0][
        "snippet"
    ]


def test_semantic_search_uses_faiss_and_preserves_document_sources(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CLINIC_KNOWLEDGE_SEARCH_MODE", "semantic")

    def fake_embeddings(texts):
        vectors = []
        for text in texts:
            normalized = text.casefold()
            if "arrive" in normalized or "check-in" in normalized:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors

    monkeypatch.setattr(server, "create_embeddings", fake_embeddings)

    matches = server._semantic_matches(
        "When should patients arrive for check-in?",
        max_results=1,
    )

    assert matches[0].document_id == "arrival-policy"
    assert "15 minutes early" in matches[0].snippet
    assert matches[0].score == 1.0


@pytest.mark.anyio
async def test_unknown_document_returns_controlled_tool_error():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "read_knowledge_document", {"document_id": "missing"}
        )

    assert result.is_error is True
    assert "Unknown document_id" in result.content[0].text
