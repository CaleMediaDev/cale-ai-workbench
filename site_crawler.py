"""Bounded, local website research with Crawl4AI and project memory."""

import hashlib
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

import project_memory
from web_tools import check_url


def normalized_url(url: str, host: str = "") -> str:
    """Keep a crawl on one public hostname and reject unsafe links."""
    clean = urldefrag(url.strip())[0]
    parsed, _ = check_url(clean)
    if host and parsed.hostname.lower() != host:
        raise ValueError("A website crawl stays on its starting hostname.")
    return clean


def page_path(project: Path, url: str) -> Path:
    """Use a stable filename, including a hash to avoid slug collisions."""
    parsed = urlsplit(url)
    label = re.sub(r"[^a-z0-9]+", "-", (parsed.path or "home").lower()).strip("-")[-60:] or "home"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return project / "Research" / "Web" / f"{label}-{digest}.md"


async def crawl_site(project: Path, start_url: str, max_pages: int = 5) -> dict:
    """Attempt at most 20 same-host pages, save Markdown, and index it locally.

    Crawl4AI runs in its own local Chromium. The crawl respects robots.txt and
    never signs in or uses a cloud extraction service. Pages are untrusted data.
    Failed and unreadable pages consume the same visit budget as saved pages.
    """
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

    project = Path(project).resolve(strict=True)
    start = normalized_url(start_url)
    host = urlsplit(start).hostname.lower()
    limit = max(1, min(int(max_pages), 20))
    queue, queued, visited = deque([start]), {start}, set()
    pages, errors = [], []
    settings = CrawlerRunConfig(
        check_robots_txt=True, page_timeout=30000, wait_until="domcontentloaded",
        exclude_external_links=True, verbose=False,
    )
    async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False)) as crawler:
        while queue and len(visited) < limit:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            try:
                normalized_url(url, host)
                result = await crawler.arun(url=url, config=settings)
                if not result.success:
                    raise RuntimeError(str(result.error_message or "Crawl failed")[:300])
                normalized_url(getattr(result, "redirected_url", None) or url, host)
                markdown_result = result.markdown
                if isinstance(markdown_result, str):
                    markdown = markdown_result
                else:
                    markdown = getattr(markdown_result, "fit_markdown", "") or ""
                    if not str(markdown).strip():
                        markdown = getattr(markdown_result, "raw_markdown", "") or ""
                markdown = str(markdown)[:100_000]
                if not markdown.strip():
                    raise RuntimeError("No readable page content")
                path = page_path(project, url)
                path.parent.mkdir(parents=True, exist_ok=True)
                body = f"# Source: {url}\n\nCrawled: {datetime.now(timezone.utc).isoformat()}\n\n{markdown}\n"
                path.write_text(body, encoding="utf-8")
                project_memory.index_file(project, path)
                pages.append({"url": url, "file": str(path), "characters": len(markdown)})
                for link in (result.links or {}).get("internal", []):
                    if not isinstance(link, dict):
                        continue
                    candidate = link.get("href", "")
                    if not isinstance(candidate, str):
                        continue
                    try:
                        base_url = getattr(result, "redirected_url", None) or url
                        candidate = normalized_url(urljoin(base_url, candidate), host)
                    except (OSError, ValueError):
                        continue
                    if candidate not in queued and len(queued) < 100:
                        queue.append(candidate)
                        queued.add(candidate)
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append({"url": url, "error": str(exc)[:300]})
    return {"start_url": start, "pages": pages, "errors": errors,
            "limit": limit, "project_folder": str(project)}
