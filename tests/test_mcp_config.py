"""Tests for external MCP server configuration loading."""

import json
import logging
import sys
from unittest.mock import patch

import pytest

from app.services.chat import (
    MCPConnectionError,
    _load_mcp_config,
    _load_mcp_tools,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_load_mcp_config_replaces_python_executable(monkeypatch, tmp_path):
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "test_server": {
                        "command": "${PYTHON_EXECUTABLE}",
                        "args": ["-m", "example.server"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(config_path))

    config = _load_mcp_config()

    assert config["mcpServers"]["test_server"]["command"] == sys.executable
    assert config["mcpServers"]["test_server"]["args"] == [
        "-m",
        "example.server",
    ]


def test_load_mcp_config_rejects_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MCP_SERVERS_CONFIG", str(tmp_path / "missing-mcp-config.json")
    )

    with pytest.raises(
        MCPConnectionError,
        match="server configuration cannot be loaded",
    ):
        _load_mcp_config()


def test_load_mcp_config_rejects_empty_server_map(monkeypatch, tmp_path):
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text('{"mcpServers": {}}', encoding="utf-8")
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(config_path))

    with pytest.raises(
        MCPConnectionError,
        match="mcpServers must be a non-empty object",
    ):
        _load_mcp_config()


@pytest.mark.anyio
async def test_mcp_transport_failure_is_logged_without_private_details(
    monkeypatch,
    tmp_path,
    caplog,
):
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "broken_server": {
                        "command": sys.executable,
                        "args": ["-m", "missing.server"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(config_path))

    class BrokenAdapter:
        def __init__(self, config):
            self.config = config

        async def __aenter__(self):
            raise ConnectionError("private transport details")

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    with (
        patch("app.services.chat.MCPAdapter", BrokenAdapter),
        caplog.at_level(logging.ERROR, logger="app.services.chat"),
        pytest.raises(MCPConnectionError, match="clinic tools are unavailable"),
    ):
        await _load_mcp_tools()

    assert "MCP tool discovery failed (error_type=ConnectionError)" in caplog.text
    assert "private transport details" not in caplog.text

