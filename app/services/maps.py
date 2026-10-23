"""Opt-in, turn-only Google Maps MCP search, without LLM or database storage."""

import asyncio
import json
import os
from urllib.parse import urlparse

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ENDPOINT = "https://mapstools.googleapis.com/mcp"


def maps_enabled() -> bool:
    return (
        os.getenv("GOOGLE_MAPS_ENABLED", "false").lower() == "true"
        and bool(os.getenv("GOOGLE_MAPS_API_KEY", "").strip())
    )


def safe_maps_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname in {
        "maps.google.com", "www.google.com", "maps.google.co.uk", "maps.app.goo.gl"
    } and not parsed.username and not parsed.password


def format_places(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("places"), list):
        raise ValueError("Invalid Maps response")
    sources = []
    for index, place in enumerate(data.get("places", [])):
        if not isinstance(place, dict):
            raise ValueError("Invalid Maps place")
        links = place.get("googleMapsLinks", place.get("google_maps_links", {}))
        attribution = place.get("attribution", {})
        if not isinstance(attribution, dict):
            attribution = {}
        url = links.get("placeUrl", links.get("place_url")) if isinstance(links, dict) else None
        if safe_maps_url(url):
            sources.append({"title": attribution.get("title") or f"Place {index + 1}", "url": url, "index": index})
        else:
            raise ValueError("Maps source unavailable")
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("No usable Maps summary returned")
    # Never display Google content without its corresponding source links.
    if not sources:
        raise ValueError("Maps attribution unavailable")
    return {"summary": summary, "sources": sources}


async def search_places(query: str) -> dict:
    if not maps_enabled():
        raise ValueError("Maps lookup is not configured")
    transport = StreamableHttpTransport(
        ENDPOINT, headers={"X-Goog-Api-Key": os.environ["GOOGLE_MAPS_API_KEY"]}
    )
    async with asyncio.timeout(25):
        async with Client(transport) as client:
            # Discover the published schema rather than assuming snake/camel case.
            tools = await client.list_tools()
            tool = next(item for item in tools if item.name == "search_places")
            properties = tool.inputSchema.get("properties", {})
            query_key = "text_query" if "text_query" in properties else "textQuery"
            result = await client.call_tool("search_places", {query_key: query})
            data = result.structured_content
            if data is None:
                data = next(json.loads(block.text) for block in result.content if getattr(block, "type", None) == "text")
            return format_places(data)
