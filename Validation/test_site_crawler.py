"""Local crawler boundaries and durable research results."""

import asyncio
import sys
import types
from urllib.parse import urlsplit

import pytest

import project_memory
import site_crawler


def test_url_scope_and_stable_path(tmp_path, monkeypatch):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    url = site_crawler.normalized_url("https://example.com/a#part", "example.com")
    assert url == "https://example.com/a"
    with pytest.raises(ValueError, match="hostname"):
        site_crawler.normalized_url("https://other.example/a", "example.com")
    assert site_crawler.page_path(tmp_path, url) == site_crawler.page_path(tmp_path, url)


@pytest.mark.parametrize("second_href", [
    "https://example.com/second", "/second", "second", "../second",
])
def test_crawl_saves_pages_and_searchable_memory(tmp_path, monkeypatch, second_href):
    def check_absolute_url(url):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("An absolute HTTP URL is required")
        return parsed, ["93.184.215.14"]

    monkeypatch.setattr(site_crawler, "check_url", check_absolute_url)
    seen = []

    class FakeCrawler:
        def __init__(self, config):
            self.config = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def arun(self, url, config):
            seen.append(url)
            assert config.check_robots_txt
            links = [{"href": second_href}, {"href": "https://other.example/no"}]
            return types.SimpleNamespace(
                success=True, markdown=types.SimpleNamespace(fit_markdown="A distinctive silver penguin"),
                links={"internal": links}, error_message="",
            )

    fake = types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    )
    monkeypatch.setitem(sys.modules, "crawl4ai", fake)
    result = asyncio.run(site_crawler.crawl_site(tmp_path, "https://example.com", max_pages=2))
    assert seen == ["https://example.com", "https://example.com/second"]
    assert len(result["pages"]) == 2
    assert all("silver penguin" in open(item["file"], encoding="utf-8").read() for item in result["pages"])
    assert project_memory.search(tmp_path, "silver penguin", kind="file")


def test_redirected_page_resolves_links_against_final_url(tmp_path, monkeypatch):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    monkeypatch.setattr(project_memory, "index_file", lambda *_: None)
    start = "https://example.com/old"
    final_url = "https://example.com/docs/"
    seen = []

    class FakeCrawler:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def arun(self, url, config):
            seen.append(url)
            return types.SimpleNamespace(
                success=True, url=final_url if url == start else url,
                markdown="Readable page", error_message="",
                links={"internal": [{"href": "guide"}]} if url == start else {},
            )

    monkeypatch.setitem(sys.modules, "crawl4ai", types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    ))
    result = asyncio.run(site_crawler.crawl_site(tmp_path, start, max_pages=2))
    assert seen == [start, "https://example.com/docs/guide"]
    assert result["errors"] == []
    assert len(result["pages"]) == 2


@pytest.mark.parametrize("failure", ["unsuccessful", "exception", "empty"])
def test_failed_pages_consume_the_crawl_budget(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    monkeypatch.setattr(project_memory, "index_file", lambda *_: None)
    seen = []
    start = "https://example.com"

    class FakeCrawler:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def arun(self, url, config):
            seen.append(url)
            assert config.check_robots_txt
            if url == start:
                return types.SimpleNamespace(
                    success=True, markdown="Index page", error_message="",
                    links={"internal": [{"href": f"{start}/broken-{i}"} for i in range(40)]},
                )
            if failure == "exception":
                raise RuntimeError("Page timed out")
            return types.SimpleNamespace(
                success=failure != "unsuccessful", markdown="", links={},
                error_message="Page failed",
            )

    monkeypatch.setitem(sys.modules, "crawl4ai", types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    ))
    result = asyncio.run(site_crawler.crawl_site(tmp_path, start, max_pages=2))
    assert seen == [start, f"{start}/broken-0"]
    assert len(result["pages"]) == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["url"] == seen[1]


@pytest.mark.parametrize("markdown, expected", [
    (types.SimpleNamespace(fit_markdown="", raw_markdown="Raw page content"), "Raw page content"),
    (types.SimpleNamespace(fit_markdown=" \n\t", raw_markdown="Raw page content"), "Raw page content"),
    (types.SimpleNamespace(fit_markdown="Fitted content", raw_markdown="Raw content"), "Fitted content"),
    ("Plain Markdown", "Plain Markdown"),
    (types.SimpleNamespace(fit_markdown="", raw_markdown=""), None),
])
def test_crawl_markdown_content(tmp_path, monkeypatch, markdown, expected):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    indexed = []
    monkeypatch.setattr(project_memory, "index_file", lambda project, path: indexed.append(path))

    class FakeCrawler:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def arun(self, url, config):
            return types.SimpleNamespace(success=True, markdown=markdown, links={}, error_message="")

    monkeypatch.setitem(sys.modules, "crawl4ai", types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    ))
    url = "https://example.com"
    result = asyncio.run(site_crawler.crawl_site(tmp_path, url, max_pages=1))
    path = site_crawler.page_path(tmp_path, url)
    if expected is None:
        assert result["pages"] == []
        assert result["errors"] == [{"url": url, "error": "No readable page content"}]
        assert indexed == []
        assert not path.exists()
    else:
        assert result["errors"] == []
        assert len(result["pages"]) == 1
        assert result["pages"][0]["characters"] == len(expected)
        body = path.read_text(encoding="utf-8")
        assert body.split("\n\n", 2)[2] == expected + "\n"
        assert indexed == [path]
