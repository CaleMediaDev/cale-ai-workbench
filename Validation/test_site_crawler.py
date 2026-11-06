"""Local crawler boundaries and durable research results."""

import asyncio
import sys
import types
from urllib.parse import urlsplit

import pytest

import project_memory
import site_crawler


@pytest.mark.parametrize("bad_link", [
    {"href": 123}, {"href": b"/broken"}, None, "/broken", 123, [],
])
def test_malformed_link_target_does_not_abort_crawl(tmp_path, monkeypatch, bad_link):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    indexed = []
    monkeypatch.setattr(project_memory, "index_file", lambda project, path: indexed.append(path))
    start = "https://example.com"
    child = start + "/child"
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
                success=True, markdown="Readable content", error_message="",
                links={"internal": [bad_link, {"href": "/child"}]} if url == start else {},
            )

    monkeypatch.setitem(sys.modules, "crawl4ai", types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    ))
    result = asyncio.run(site_crawler.crawl_site(tmp_path, start, max_pages=2))
    assert seen == [start, child]
    assert result["errors"] == []
    assert [page["url"] for page in result["pages"]] == seen
    assert indexed == [site_crawler.page_path(tmp_path, url) for url in seen]
    assert all(path.exists() for path in indexed)


@pytest.mark.parametrize("malformed_links", [
    {"internal": None}, {"internal": 123}, {"internal": "broken"},
    ["broken"], "broken",
])
def test_malformed_link_collection_preserves_queued_pages(tmp_path, monkeypatch, malformed_links):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    indexed = []
    monkeypatch.setattr(project_memory, "index_file", lambda project, path: indexed.append(path))
    start = "https://example.com"
    malformed = start + "/malformed"
    sibling = start + "/sibling"
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
            if url == start:
                links = {"internal": [{"href": malformed}, {"href": sibling}]}
            elif url == malformed:
                links = malformed_links
            else:
                links = {}
            return types.SimpleNamespace(
                success=True, markdown="Readable content", error_message="", links=links,
            )

    monkeypatch.setitem(sys.modules, "crawl4ai", types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    ))
    result = asyncio.run(site_crawler.crawl_site(tmp_path, start, max_pages=3))
    assert seen == [start, malformed, sibling]
    assert result["errors"] == []
    assert [page["url"] for page in result["pages"]] == seen
    assert indexed == [site_crawler.page_path(tmp_path, url) for url in seen]
    assert all(path.exists() for path in indexed)


@pytest.mark.parametrize("empty_link", [{}, {"href": ""}, {"href": " \t\n"}])
def test_empty_link_target_does_not_consume_redirect_crawl_budget(tmp_path, monkeypatch, empty_link):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    indexed = []
    monkeypatch.setattr(project_memory, "index_file", lambda project, path: indexed.append(path))
    start = "https://example.com/old"
    destination = "https://example.com/docs/"
    child = destination + "child"
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
                success=True, markdown="Readable content", error_message="",
                redirected_url=destination if url == start else None,
                links={"internal": [empty_link, {"href": "child"}]} if url == start else {},
            )

    monkeypatch.setitem(sys.modules, "crawl4ai", types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    ))
    result = asyncio.run(site_crawler.crawl_site(tmp_path, start, max_pages=2))
    assert seen == [start, child]
    assert result["errors"] == []
    assert [page["url"] for page in result["pages"]] == [start, child]
    assert indexed == [site_crawler.page_path(tmp_path, url) for url in [start, child]]
    assert all(path.exists() for path in indexed)


@pytest.mark.parametrize("self_link", [
    "#overview", "https://example.com/docs/", "https://example.com/docs/#overview",
])
def test_redirect_self_links_do_not_consume_child_budget(tmp_path, monkeypatch, self_link):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    indexed = []
    monkeypatch.setattr(project_memory, "index_file", lambda project, path: indexed.append(path))
    start = "https://example.com/old"
    destination = "https://example.com/docs/"
    child = destination + "child"
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
                success=True, markdown="Readable content", error_message="",
                redirected_url=destination if url == start else None,
                links={"internal": [{"href": self_link}, {"href": "child"}]} if url == start else {},
            )

    monkeypatch.setitem(sys.modules, "crawl4ai", types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    ))
    result = asyncio.run(site_crawler.crawl_site(tmp_path, start, max_pages=2))
    assert seen == [start, child]
    assert result["errors"] == []
    assert [page["url"] for page in result["pages"]] == [start, child]
    assert indexed == [site_crawler.page_path(tmp_path, url) for url in [start, child]]
    assert all(path.exists() for path in indexed)


def test_later_backlink_to_redirect_destination_preserves_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    indexed = []
    monkeypatch.setattr(project_memory, "index_file", lambda project, path: indexed.append(path))
    start = "https://example.com/old"
    destination = "https://example.com/docs/"
    child = destination + "child"
    grandchild = destination + "grandchild"
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
            if url == start:
                links = [{"href": child}]
            elif url == child:
                links = [{"href": destination}, {"href": grandchild}]
            else:
                links = []
            return types.SimpleNamespace(
                success=True, markdown="Readable content", error_message="",
                redirected_url=destination if url == start else None,
                links={"internal": links},
            )

    monkeypatch.setitem(sys.modules, "crawl4ai", types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    ))
    result = asyncio.run(site_crawler.crawl_site(tmp_path, start, max_pages=3))
    assert seen == [start, child, grandchild]
    assert result["errors"] == []
    assert [page["url"] for page in result["pages"]] == seen
    assert indexed == [site_crawler.page_path(tmp_path, url) for url in seen]
    assert all(path.exists() for path in indexed)
    assert not site_crawler.page_path(tmp_path, destination).exists()


def test_converging_redirects_save_destination_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    indexed = []
    monkeypatch.setattr(project_memory, "index_file", lambda project, path: indexed.append(path))
    start = "https://example.com"
    first = start + "/first-alias"
    second = start + "/second-alias"
    sibling = start + "/sibling"
    destination = start + "/destination"
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
                success=True, markdown="Readable content", error_message="",
                redirected_url=destination if url in {first, second} else None,
                links={"internal": [{"href": first}, {"href": second}, {"href": sibling}]}
                if url == start else {},
            )

    monkeypatch.setitem(sys.modules, "crawl4ai", types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    ))
    result = asyncio.run(site_crawler.crawl_site(tmp_path, start, max_pages=4))
    assert seen == [start, first, second, sibling]
    assert result["errors"] == []
    assert [page["url"] for page in result["pages"]] == [start, first, sibling]
    assert indexed == [site_crawler.page_path(tmp_path, url) for url in [start, first, sibling]]
    assert all(path.exists() for path in indexed)
    assert not site_crawler.page_path(tmp_path, second).exists()


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


@pytest.mark.parametrize("redirected_url, expected_child", [
    ("https://example.com/docs/", "https://example.com/docs/child"),
    (None, "https://example.com/child"),
])
def test_relative_links_use_redirect_destination(tmp_path, monkeypatch, redirected_url, expected_child):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    monkeypatch.setattr(project_memory, "index_file", lambda *_: None)
    start = "https://example.com/old"
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
                success=True, markdown="Readable content", error_message="",
                redirected_url=redirected_url if url == start else None,
                links={"internal": [
                    {"href": "https://other.example/outside"},
                    {"href": "child"},
                ]} if url == start else {},
            )

    monkeypatch.setitem(sys.modules, "crawl4ai", types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    ))
    result = asyncio.run(site_crawler.crawl_site(tmp_path, start, max_pages=2))
    assert seen == [start, expected_child]
    assert result["errors"] == []
    assert [page["url"] for page in result["pages"]] == seen


def test_off_host_redirect_is_not_saved_or_indexed(tmp_path, monkeypatch):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    indexed = []
    monkeypatch.setattr(project_memory, "index_file", lambda project, path: indexed.append(path))
    start = "https://example.com/redirect"
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
                success=True, markdown="Content from another hostname", error_message="",
                redirected_url="https://other.example/landing",
                links={"internal": [{"href": "https://example.com/child"}]},
            )

    monkeypatch.setitem(sys.modules, "crawl4ai", types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    ))
    result = asyncio.run(site_crawler.crawl_site(tmp_path, start, max_pages=2))
    assert seen == [start]
    assert result["pages"] == []
    assert result["errors"] == [{
        "url": start, "error": "A website crawl stays on its starting hostname.",
    }]
    assert indexed == []
    assert not site_crawler.page_path(tmp_path, start).exists()


def test_crawler_library_exception_preserves_queued_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(site_crawler, "check_url", lambda url: (urlsplit(url), ["93.184.215.14"]))
    indexed = []
    monkeypatch.setattr(project_memory, "index_file", lambda project, path: indexed.append(path))
    start = "https://example.com"
    broken = start + "/broken"
    sibling = start + "/sibling"
    seen = []

    class BrowserError(Exception):
        pass

    class FakeCrawler:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def arun(self, url, config):
            seen.append(url)
            if url == broken:
                raise BrowserError("Browser page failed")
            return types.SimpleNamespace(
                success=True, markdown="Readable content", error_message="",
                links={"internal": [{"href": broken}, {"href": sibling}]} if url == start else {},
            )

    monkeypatch.setitem(sys.modules, "crawl4ai", types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
    ))
    result = asyncio.run(site_crawler.crawl_site(tmp_path, start, max_pages=3))
    assert seen == [start, broken, sibling]
    assert result["errors"] == [{"url": broken, "error": "Browser page failed"}]
    assert [page["url"] for page in result["pages"]] == [start, sibling]
    assert indexed == [site_crawler.page_path(tmp_path, url) for url in [start, sibling]]
    assert all(path.exists() for path in indexed)
    assert not site_crawler.page_path(tmp_path, broken).exists()


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
    (" " * 100_000 + "Readable content" + "x" * 100_000,
     ("Readable content" + "x" * 100_000)[:100_000]),
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
