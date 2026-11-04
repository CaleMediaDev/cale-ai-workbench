"""Find no-key public API leads without installing a gateway or creating an account.

The upstream catalog checks URL reachability, not whether an API still works
or permits a particular use. Agents must read the provider's own documentation
before calling an endpoint or relying on its data.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache" / "public-apis-live.json"
SOURCE = "https://raw.githubusercontent.com/Manavarya09/public-apis-live/main/data/apis.json"
MAX_BYTES = 4_000_000
MAX_AGE = 86400


def _catalog() -> tuple[list[dict], bool]:
    """Refresh daily; keep the last good snapshot when the PC is offline."""
    fresh = CACHE.is_file() and time.time() - CACHE.stat().st_mtime < MAX_AGE
    if not fresh:
        try:
            request = Request(SOURCE, headers={"User-Agent": "Cale-AI-Workbench/1.0"})
            with urlopen(request, timeout=15) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("Public API catalog exceeded its download limit.")
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, list):
                raise ValueError("Unexpected public API catalog format.")
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            temp = CACHE.with_suffix(".tmp")
            temp.write_bytes(raw)
            temp.replace(CACHE)
            fresh = True
        except Exception:
            if not CACHE.is_file():
                raise RuntimeError("The public API catalog is unavailable while offline.") from None
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Unexpected public API catalog format.")
    return data, fresh


def search_public_apis(query: str, max_results: int = 10) -> dict:
    """Search no-key candidates; return source URLs and verification caveats."""
    terms = [word.casefold() for word in query.split() if len(word) > 1]
    if not terms or len(query) > 120:
        raise ValueError("Describe the public data or API you need in 2 to 120 characters.")
    if not isinstance(max_results, int) or not 1 <= max_results <= 20:
        raise ValueError("Result count must be between 1 and 20.")
    data, fresh = _catalog()
    matches = []
    for item in data:
        if not isinstance(item, dict) or item.get("status") != "up":
            continue
        if str(item.get("auth", "")).casefold() != "none":
            continue
        text = " ".join(str(item.get(k, "")) for k in ("name", "description", "category")).casefold()
        if not all(term in text for term in terms):
            continue
        score = sum(4 for term in terms if term in str(item.get("name", "")).casefold())
        score += sum(2 for term in terms if term in str(item.get("category", "")).casefold())
        score += sum(1 for term in terms if term in str(item.get("description", "")).casefold())
        matches.append((score, {key: item.get(key, "") for key in
                                ("name", "description", "category", "url", "lastChecked")}))
    matches.sort(key=lambda row: (-row[0], str(row[1]["name"]).casefold()))
    selected = [item for _, item in matches[:max_results]]
    return {"results": selected, "count": len(selected), "catalog_url": SOURCE,
            "cache_fresh": fresh,
            "note": "Catalog entries are research leads. 'Up' only means the listed URL responded; read the provider's official API documentation and verify current terms and data before using it."}
