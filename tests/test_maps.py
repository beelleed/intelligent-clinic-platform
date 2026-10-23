import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.rate_limit import chat_rate_limiter
from app.services.maps import format_places, maps_enabled, safe_maps_url, search_places


@pytest.fixture(autouse=True)
def maps_environment(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-only-placeholder")
    monkeypatch.setenv("PUBLIC_CHAT_ENABLED", "true")
    chat_rate_limiter.reset()
    yield
    chat_rate_limiter.reset()


def place_data():
    return {"summary": "A public location [0].", "places": [{"googleMapsLinks": {"placeUrl": "https://maps.google.com/?cid=1"}, "attribution": {"title": "Public place"}}]}


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY")
    assert not maps_enabled()
    assert TestClient(app).post("/maps/search", json={"query": "parking in Palo Alto"}).status_code == 503


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://google.com.evil.test/", "http://maps.google.com/", "https://user:pass@maps.google.com/"])
def test_unsafe_sources_rejected(url):
    assert not safe_maps_url(url)


def test_attribution_is_required():
    with pytest.raises(ValueError):
        format_places({"summary": "Unattributed content", "places": []})
    assert format_places(place_data())["sources"][0]["index"] == 0


@pytest.mark.parametrize("query_key", ["text_query", "textQuery"])
def test_remote_mcp_schema_and_source_handling(query_key):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.list_tools.return_value = [SimpleNamespace(name="search_places", inputSchema={"properties": {query_key: {}}})]
    client.call_tool.return_value = SimpleNamespace(structured_content=place_data())
    with patch("app.services.maps.Client", return_value=client):
        result = asyncio.run(search_places("parking in Palo Alto"))
    client.call_tool.assert_awaited_once_with("search_places", {query_key: "parking in Palo Alto"})
    assert result["summary"] == "A public location [0]."


def test_maps_shares_quota_and_does_not_cache():
    client = TestClient(app)
    with patch("app.api.search_places", AsyncMock(return_value=format_places(place_data()))) as search:
        for _ in range(3):
            response = client.post("/maps/search", json={"query": "parking in Palo Alto"})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
        assert client.post("/chat", json={"query": "hello", "session_id": "new-session"}).status_code == 429
        assert search.await_count == 3


def test_remote_error_does_not_expose_key():
    with patch("app.api.search_places", AsyncMock(side_effect=RuntimeError("secret test-only-placeholder"))):
        response = TestClient(app).post("/maps/search", json={"query": "parking in Palo Alto"})
    assert response.status_code == 502
    assert "test-only-placeholder" not in response.text


def test_public_switch_disables_maps(monkeypatch):
    monkeypatch.setenv("PUBLIC_CHAT_ENABLED", "false")
    client = TestClient(app)
    assert not client.get("/maps/status").json()["configured"]
    assert client.post("/maps/search", json={"query": "parking in Palo Alto"}).status_code == 403
