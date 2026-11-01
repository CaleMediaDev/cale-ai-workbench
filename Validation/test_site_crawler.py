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
