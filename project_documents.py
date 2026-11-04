"""Read a task's local documents with bounded, reusable extracted text.

Downloaded PDFs and spreadsheets are project files. Their source bytes remain
authoritative; the cache is keyed by a content hash so a changed file cannot
silently reuse old text. Nothing in this module calls a cloud provider.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path

import file_chat
import project_memory

MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_EXTRACTED_CHARS = 5_000_000
MAX_EXCERPT_CHARS = 10_000
CACHE_VERSION = "v3"  # Re-extract pre-snapshot caches; v1 also translated line endings.
SUPPORTED = file_chat.IMAGES | file_chat.PLAIN_TEXT | file_chat.DOCUMENTS


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_snapshot(source: Path, expected_digest: str, question: str = "") -> str:
    """Extract only bytes verified to belong to the requested cache key."""
    with tempfile.TemporaryDirectory(prefix="document-extract-") as directory:
        snapshot = Path(directory) / source.name
        digest = hashlib.sha256()
        total = 0
        with source.open("rb") as original, snapshot.open("wb") as output:
            while chunk := original.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise ValueError("This document exceeds the 50 MB local reader limit")
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected_digest:
            raise RuntimeError("The document changed while it was being read; try again")
        if source.suffix.casefold() in file_chat.IMAGES:
            return file_chat.image_description(snapshot, question)
        if source.suffix.casefold() in file_chat.PLAIN_TEXT:
            return snapshot.read_text(encoding="utf-8-sig", errors="replace")
        return file_chat.document_text(snapshot, max_scanned_pages=20)


def inspect(folder: str | Path, path: str, question: str = "",
            start_char: int = 0, max_chars: int = MAX_EXCERPT_CHARS) -> dict:
    """Return a local excerpt; later calls reuse extraction if bytes match."""
    root = Path(folder).resolve(strict=True)
    source = (root / path).resolve(strict=True)
    if not source.is_file() or not source.is_relative_to(root):
        raise ValueError("Choose a file inside this task folder")
    if source.suffix.casefold() not in SUPPORTED:
        raise ValueError("This reader supports images, text, PDF, DOCX, XLSX, and PPTX files")
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("This document exceeds the 50 MB local reader limit")
    if len(question) > 1000:
        raise ValueError("Keep the document question under 1,000 characters")
    limit = max(1, min(int(max_chars), MAX_EXCERPT_CHARS))
    relative = source.relative_to(root).as_posix()
    if source.suffix.casefold() in file_chat.IMAGES:
        digest = _digest(source)
        description = _extract_snapshot(source, digest, question)[:limit]
        if _digest(source) != digest:
            raise RuntimeError("The document changed while it was being read; try again")
        return {"source": relative, "text": description, "kind": "image",
                "total_characters": len(description), "next_start_char": None,
                "cached": False}

    digest = _digest(source)
    # Extraction depends on both file type and whether scanned-PDF vision is
    # installed. A later vision install must not reuse an earlier text-only PDF.
    vision = source.suffix.casefold() == ".pdf" and (
        file_chat.ROOT / "Media/python/Scripts/python.exe"
    ).is_file()
    cache = (root / ".local-ai" / "document-cache" /
             f"{CACHE_VERSION}-{digest}-{source.suffix.lower().lstrip('.')}-{int(vision)}.txt")
    cached = cache.is_file()
    if cached:
        with cache.open("r", encoding="utf-8", newline="") as cached_text:
            text = cached_text.read()
        if _digest(source) != digest:
            raise RuntimeError("The document changed while it was being read; try again")
    else:
        text = _extract_snapshot(source, digest)
        if _digest(source) != digest:
            raise RuntimeError("The document changed while it was being read; try again")
        if len(text) > MAX_EXTRACTED_CHARS:
            raise ValueError("The extracted text is too large for this task reader")
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=cache.parent,
            prefix=cache.name + ".", suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            try:
                output.write(text)
            except BaseException:
                output.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, cache)
        finally:
            temporary.unlink(missing_ok=True)
        if _digest(source) != digest:
            raise RuntimeError("The document changed while it was being read; try again")
    if len(text) > MAX_EXTRACTED_CHARS:
        raise ValueError("The extracted text is too large for this task reader")
    indexed = False
    try:
        if source.suffix.casefold() in file_chat.DOCUMENTS:
            project_memory.index_document_text(
                root, source, text, digest, f"{CACHE_VERSION}-vision{int(vision)}",
            )
            indexed = True
        elif source.stat().st_size <= 256_000:
            project_memory.index_file(root, source)
            indexed = True
    except (OSError, sqlite3.Error, ValueError):
        # A locked index or sensitive filename must not prevent an explicitly
        # requested document read. A later inspect can rebuild the FTS entry.
        pass
    # Indexing may take long enough for the source to change after the earlier
    # validation, including when extraction was served from the cache.
    if _digest(source) != digest:
        raise RuntimeError("The document changed while it was being read; try again")
    if question.strip():
        excerpt = file_chat.select_excerpt(text, question, limit=limit,
                                            subject="document")
        next_char = None
    else:
        start = max(0, min(int(start_char), len(text)))
        excerpt = text[start:start + limit]
        next_char = start + len(excerpt) if start + len(excerpt) < len(text) else None
    # Question-based excerpt selection can outlast the earlier validation.
    if _digest(source) != digest:
        raise RuntimeError("The document changed while it was being read; try again")
    return {"source": relative, "text": excerpt, "kind": "document",
            "total_characters": len(text), "next_start_char": next_char,
            "cached": cached, "indexed": indexed}
