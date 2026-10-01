"""Static safety checks for the deployable container definition."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_uses_non_root_runtime_and_agent_files() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.count("FROM python:3.13-slim") == 2
    assert " AS builder" in dockerfile
    assert "USER appuser" in dockerfile
    assert "EXPOSE 8080" in dockerfile
    assert 'CMD ["python", "main.py"]' in dockerfile
    assert "MCP_SERVERS_CONFIG=/app/mcp_config.json" in dockerfile
    assert "COPY --chown=appuser:appgroup mcp_servers ./mcp_servers" in dockerfile


def test_docker_build_context_excludes_secrets_and_local_state() -> None:
    ignored_entries = set(
        (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )

    assert ".env" in ignored_entries
    assert ".env.*" in ignored_entries
    assert ".git" in ignored_entries
    assert "env/" in ignored_entries
    assert "data/*.db" in ignored_entries
    assert "data/vector_store.index" in ignored_entries
