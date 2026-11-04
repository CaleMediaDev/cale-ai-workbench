"""Search only no-key leads; never misrepresent a reachable docs page as a live API."""

import asyncio
import json
from unittest.mock import patch

import pytest

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


@pytest.mark.parametrize("max_results", [1.5, "2", None])
def test_search_public_apis_rejects_non_integer_limits_before_catalog_access(max_results):
    with patch.object(public_api_catalog, "_catalog") as catalog:
        with pytest.raises(ValueError, match="Result count must be between 1 and 20"):
            public_api_catalog.search_public_apis("weather", max_results=max_results)
    catalog.assert_not_called()


def test_catalog_preserves_stale_snapshot_on_incompatible_encoding(tmp_path):
    cache = tmp_path / "apis.json"
    snapshot = [{"name": "Last good snapshot"}]
    original = json.dumps(snapshot).encode("utf-8")
    cache.write_bytes(original)
    expired_time = cache.stat().st_mtime + public_api_catalog.MAX_AGE + 1
    downloaded = json.dumps([{"name": "Replacement"}]).encode("utf-16")

    with patch.object(public_api_catalog, "CACHE", cache), \
         patch.object(public_api_catalog.time, "time", return_value=expired_time), \
         patch.object(public_api_catalog, "urlopen") as open_url:
        response = open_url.return_value.__enter__.return_value
        response.read.return_value = downloaded
        data, fresh = public_api_catalog._catalog()

    open_url.assert_called_once()
    response.read.assert_called_once_with(public_api_catalog.MAX_BYTES + 1)
    assert data == snapshot
    assert fresh is False
    assert cache.read_bytes() == original


def test_catalog_recovers_from_fresh_malformed_cache(tmp_path):
    cache = tmp_path / "apis.json"
    cache.write_bytes(b'{"incomplete":')
    current_time = cache.stat().st_mtime + 1
    snapshot = [{"name": "Recovered snapshot"}]
    downloaded = json.dumps(snapshot).encode("utf-8")

    with patch.object(public_api_catalog, "CACHE", cache), \
         patch.object(public_api_catalog.time, "time", return_value=current_time), \
         patch.object(public_api_catalog, "urlopen") as open_url:
        response = open_url.return_value.__enter__.return_value
        response.read.return_value = downloaded
        data, fresh = public_api_catalog._catalog()

    open_url.assert_called_once()
    response.read.assert_called_once_with(public_api_catalog.MAX_BYTES + 1)
    assert data == snapshot
    assert fresh is True
    assert cache.read_bytes() == downloaded
