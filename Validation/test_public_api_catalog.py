"""Search only no-key leads; never misrepresent a reachable docs page as a live API."""

import asyncio
import json
from unittest.mock import patch

import public_api_catalog
import toolbox_mcp


def test_search_public_apis_sorts_non_string_names(tmp_path):
    cache = tmp_path / "apis.json"
    cache.write_text(json.dumps([
        {"name": name, "description": "Weather data", "category": "Weather",
         "url": "https://example.org/docs", "auth": "none", "status": "up"}
        for name in ["Zulu", None, 42]
    ]), encoding="utf-8")
    with patch.object(public_api_catalog, "CACHE", cache):
        result = public_api_catalog.search_public_apis("weather")
    assert result["count"] == 3
    assert [item["name"] for item in result["results"]] == [42, None, "Zulu"]


def test_search_public_apis_filters_auth_and_keeps_caveat(tmp_path):
    cache = tmp_path / "apis.json"
    cache.write_text(json.dumps([
        {"name": "Open Weather", "description": "Forecast data", "category": "Weather",
         "url": "https://example.org/docs", "auth": "none", "status": "up"},
        {"name": "Paid Weather", "description": "Forecast data", "category": "Weather",
         "url": "https://example.com", "auth": "apiKey", "status": "up"},
        {"name": "Dead Weather", "description": "Forecast data", "category": "Weather",
         "url": "https://example.net", "auth": "none", "status": "down"},
    ]), encoding="utf-8")
    with patch.object(public_api_catalog, "CACHE", cache):
        result = asyncio.run(toolbox_mcp.toolbox_call("local.search_public_apis",
                           json.dumps({"query": "weather"})))
    assert [item["name"] for item in result["results"]] == ["Open Weather"]
    assert "research leads" in result["note"]
