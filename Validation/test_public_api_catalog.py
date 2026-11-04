"""Search only no-key leads; never misrepresent a reachable docs page as a live API."""

import asyncio
import json
import sys
from unittest.mock import patch

import pytest

import public_api_catalog
import toolbox_mcp


@pytest.mark.parametrize("offline", [False, True])
def test_future_cache_timestamp_is_not_treated_as_fresh(tmp_path, offline):
    cache = tmp_path / "apis.json"
    old = [{"name": "Cached snapshot"}]
    new = [{"name": "Current snapshot"}]
    cache.write_text(json.dumps(old), encoding="utf-8")
    corrected_time = cache.stat().st_mtime - 3600
    with patch.object(public_api_catalog, "CACHE", cache), \
         patch.object(public_api_catalog.time, "time", return_value=corrected_time), \
         patch.object(public_api_catalog, "urlopen") as fetch:
        if offline:
            fetch.side_effect = OSError("Offline")
        else:
            fetch.return_value.__enter__.return_value.read.return_value = json.dumps(new).encode("utf-8")
        data, fresh = public_api_catalog._catalog()
    fetch.assert_called_once()
    assert data == (old if offline else new)
    assert fresh is (not offline)


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


@pytest.mark.parametrize("max_results", [1.5, "2", None, True, False])
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


def test_catalog_recovers_from_fresh_oversized_cache(tmp_path):
    cache = tmp_path / "apis.json"
    limit = 128
    cache.write_bytes(json.dumps([{"name": "x" * limit}]).encode("utf-8"))
    current_time = cache.stat().st_mtime + 1
    snapshot = [{"name": "Recovered snapshot"}]
    downloaded = json.dumps(snapshot).encode("utf-8")
    original_open = type(cache).open
    read_sizes = []

    class BoundedReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            read_sizes.append(size)
            assert size == limit + 1
            return self.handle.read(size)

    def tracked_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path == cache:
            return BoundedReader(handle)
        return handle

    with patch.object(public_api_catalog, "CACHE", cache), \
         patch.object(public_api_catalog, "MAX_BYTES", limit), \
         patch.object(public_api_catalog.time, "time", return_value=current_time), \
         patch.object(type(cache), "open", new=tracked_open), \
         patch.object(public_api_catalog, "urlopen") as open_url:
        response = open_url.return_value.__enter__.return_value
        response.read.return_value = downloaded
        data, fresh = public_api_catalog._catalog()

    assert read_sizes == [limit + 1]
    open_url.assert_called_once()
    response.read.assert_called_once_with(limit + 1)
    assert data == snapshot
    assert fresh is True
    assert cache.read_bytes() == downloaded


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


def test_catalog_recovers_from_excessively_nested_cache(tmp_path):
    cache = tmp_path / "apis.json"
    depth = sys.getrecursionlimit() + 100
    cache.write_text("[" * depth + "]" * depth, encoding="utf-8")
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


def test_catalog_overlapping_refreshes_do_not_share_temporary_files(tmp_path):
    cache = tmp_path / "apis.json"
    cache.write_text("[]", encoding="utf-8")
    expired = cache.stat().st_mtime + public_api_catalog.MAX_AGE + 1
    outer_snapshot = [{"name": "Outer"}]
    inner_snapshot = [{"name": "Inner snapshot with a much longer name"}]
    outer_raw = json.dumps(outer_snapshot).encode("utf-8")
    inner_raw = json.dumps(inner_snapshot).encode("utf-8")
    interleaved = False

    def overlapping_write(path, raw):
        nonlocal interleaved
        with path.open("wb") as handle:
            if not interleaved:
                interleaved = True
                inner_data, inner_fresh = public_api_catalog._catalog()
                assert inner_data == inner_snapshot
                assert inner_fresh is True
                assert cache.read_bytes() == inner_raw
            return handle.write(raw)

    with patch.object(public_api_catalog, "CACHE", cache), \
         patch.object(public_api_catalog.time, "time", return_value=expired), \
         patch.object(public_api_catalog, "urlopen") as open_url, \
         patch.object(type(cache), "write_bytes", new=overlapping_write):
        response = open_url.return_value.__enter__.return_value
        response.read.side_effect = [outer_raw, inner_raw]
        data, fresh = public_api_catalog._catalog()

    assert open_url.call_count == 2
    assert data == outer_snapshot
    assert fresh is True
    assert cache.read_bytes() == outer_raw


def test_catalog_uses_download_when_cache_replacement_fails(tmp_path):
    cache = tmp_path / "apis.json"
    original = json.dumps([{"name": "Old snapshot"}]).encode("utf-8")
    cache.write_bytes(original)
    expired_time = cache.stat().st_mtime + public_api_catalog.MAX_AGE + 1
    snapshot = [{"name": "Fresh snapshot"}]
    downloaded = json.dumps(snapshot).encode("utf-8")

    with patch.object(public_api_catalog, "CACHE", cache), \
         patch.object(public_api_catalog.time, "time", return_value=expired_time), \
         patch.object(public_api_catalog, "urlopen") as open_url, \
         patch.object(type(cache), "replace", side_effect=PermissionError("Cache is read-only")) as replace:
        response = open_url.return_value.__enter__.return_value
        response.read.return_value = downloaded
        data, fresh = public_api_catalog._catalog()

    open_url.assert_called_once()
    response.read.assert_called_once_with(public_api_catalog.MAX_BYTES + 1)
    replace.assert_called_once_with(cache)
    assert data == snapshot
    assert fresh is True
    assert cache.read_bytes() == original


def test_catalog_recovers_from_deterministic_decoder_recursion(tmp_path):
    cache = tmp_path / "apis.json"
    nested = "[[[]]]"
    cache.write_text(nested, encoding="utf-8")
    snapshot = [{"name": "Recovered snapshot"}]
    downloaded = json.dumps(snapshot).encode("utf-8")
    original_loads = json.loads
    def decode(value, *args, **kwargs):
        if value == nested:
            raise RecursionError("decoder nesting exhausted")
        return original_loads(value, *args, **kwargs)
    with patch.object(public_api_catalog, "CACHE", cache), patch.object(
        public_api_catalog.json, "loads", side_effect=decode
    ), patch.object(public_api_catalog, "urlopen") as open_url:
        open_url.return_value.__enter__.return_value.read.return_value = downloaded
        data, fresh = public_api_catalog._catalog()
    assert data == snapshot and fresh is True
    assert cache.read_bytes() == downloaded


def test_catalog_rejects_nested_download_before_replacing_valid_snapshot(tmp_path):
    cache = tmp_path / "apis.json"
    snapshot = [{"name": "Saved snapshot"}]
    original = json.dumps(snapshot).encode("utf-8")
    cache.write_bytes(original)
    expired = cache.stat().st_mtime + public_api_catalog.MAX_AGE + 1
    with patch.object(public_api_catalog, "CACHE", cache), patch.object(
        public_api_catalog.time, "time", return_value=expired
    ), patch.object(public_api_catalog, "urlopen") as open_url:
        open_url.return_value.__enter__.return_value.read.return_value = b"[[{}]]"
        data, fresh = public_api_catalog._catalog()
    assert data == snapshot and fresh is False
    assert cache.read_bytes() == original
