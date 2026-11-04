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


def test_crawl_saves_pages_and_searchable_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
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
            links = [{"href": "https://example.com/second"}, {"href": "https://other.example/no"}]
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


@pytest.mark.parametrize("markdown, expected", [
    (types.SimpleNamespace(fit_markdown="", raw_markdown="Raw page content"), "Raw page content"),
    (types.SimpleNamespace(fit_markdown=" \n\t", raw_markdown="Raw page content"), "Raw page content"),
    (types.SimpleNamespace(fit_markdown=" \n\t", raw_markdown=" \n"), None),
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
