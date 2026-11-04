"""Local project memory: durable SQLite records plus a fast full-text file index.

Project files remain authoritative. The database is a rebuildable search index
and a small store for explicit decisions, lessons, components, and skill recipes.
No service, account, embeddings download, or cloud request is required.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

KINDS = frozenset({"requirement", "decision", "lesson", "skill", "component", "app", "note", "file", "document"})
TEXT_SUFFIXES = frozenset({".md", ".txt", ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".json", ".html", ".css", ".yaml", ".yml", ".toml", ".ps1", ".sql"})
DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx", ".pptx"})
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
DOCUMENT_CHUNK_CHARS = 16_000
# Only project_documents' derived pre-snapshot records are retired. Explicit
# notes/skills and caller-defined extractor revisions retain their identities.
LEGACY_DOCUMENT_EXTRACTORS = tuple(
    f"extractor:{version}-vision{vision}"
    for version in ("v1", "v2") for vision in (0, 1)
)
SKIP_DIRS = frozenset({".git", ".local-ai", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next", ".cache"})
DEFAULT_FILES = (
    "AGENTS.md", "README.md", "pyproject.toml", "package.json",
    ".local-ai/PLAN.md", ".local-ai/PROGRESS.md", ".local-ai/TEST-RESULTS.md",
)
SECRET_NAME = re.compile(r"(?:^\.env(?:\.|$)|secret|credential|token|password|\.pem$|\.pfx$|\.p12$|\.key$)", re.I)
WORD = re.compile(r"[^\W_]+", re.UNICODE)
SEARCH_STOPWORDS = frozenset({
    "a", "an", "and", "are", "for", "from", "in", "is", "my", "of",
    "on", "or", "please", "show", "the", "this", "to", "what", "where", "with",
})


def database_path(folder: str | Path) -> Path:
    """Keep the index with its project so it survives app source updates."""
    root = Path(folder).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Project folder is not a directory.")
    state = root / ".local-ai"
    state.mkdir(exist_ok=True)
    return state / "memory.sqlite3"


@contextmanager
def connection(folder: str | Path):
    """Initialize schema on first use; WAL permits concurrent readers and writers."""
    conn = sqlite3.connect(database_path(folder), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
        body TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '', source_path TEXT NOT NULL DEFAULT '',
        source_hash TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS memories_kind_updated ON memories(kind, updated DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS memories_document_source ON memories(source_path,tags) WHERE kind='document'")
    conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        id UNINDEXED, title, body, tags, tokenize='unicode61'
    )""")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize(folder: str | Path) -> str:
    """Create the database automatically; SQLite ships in the app's Python."""
    with connection(folder):
        pass
    return str(database_path(folder))


def _put(conn, identifier, kind, title, body, tags="", source_path="", source_hash=""):
    now = time.time()
    conn.execute("""INSERT INTO memories
        (id, kind, title, body, tags, source_path, source_hash, created, updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, title=excluded.title,
        body=excluded.body, tags=excluded.tags, source_path=excluded.source_path,
        source_hash=excluded.source_hash, updated=excluded.updated""",
        (identifier, kind, title, body, tags, source_path, source_hash, now, now))
    conn.execute("DELETE FROM memory_fts WHERE id=?", (identifier,))
    conn.execute("INSERT INTO memory_fts(id,title,body,tags) VALUES (?,?,?,?)",
                 (identifier, title, body, tags))


def _source(folder: Path, raw: str) -> str:
    path = (folder / raw).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(folder):
        raise ValueError("Source must be a file inside this project.")
    return path.relative_to(folder).as_posix()


def save(folder: str | Path, kind: str, title: str, body: str,
         tags: str = "", source_path: str = "", memory_id: str = "") -> dict:
    """Save or update an explicit, typed memory linked to its source file."""
    root = Path(folder).resolve(strict=True)
    if kind not in KINDS - {"file", "document"}:
        raise ValueError("Choose requirement, decision, lesson, skill, component, app, or note.")
    title, body = title.strip(), body.strip()
    if not title or not body or len(title) > 200 or len(body) > 16000:
        raise ValueError("Memory needs a title and 1–16,000 characters of content.")
    tags = ", ".join(dict.fromkeys(WORD.findall(tags.lower())[:12]))
    source = _source(root, source_path) if source_path else ""
    with connection(root) as conn:
        identifier = memory_id or uuid.uuid4().hex
        if memory_id:
            existing = conn.execute("SELECT kind FROM memories WHERE id=?", (identifier,)).fetchone()
            if existing is None or existing["kind"] in {"file", "document"}:
                raise ValueError("Only an existing saved memory can be updated.")
        _put(conn, identifier, kind, title, body, tags, source)
    return {"id": identifier, "kind": kind, "title": title, "source_path": source}


def _eligible(root: Path, path: Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES or SECRET_NAME.search(path.name):
        return False
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or SECRET_NAME.search(resolved.name):
            return False
        relative = resolved.relative_to(root)
        return (resolved.is_file() and not any(SECRET_NAME.search(part) for part in relative.parts[:-1])
                and resolved.stat().st_size <= 256_000)
    except (OSError, RuntimeError):
        return False


def index_file(folder: str | Path, path: str | Path) -> dict:
    """Index one generated research file without scanning a whole codebase."""
    root = Path(folder).resolve(strict=True)
    target = Path(path).resolve(strict=True)
    if not _eligible(root, target):
        raise ValueError("Choose a small text file inside this project.")
    data = target.read_bytes()
    if b"\x00" in data:
        raise ValueError("Binary files cannot be indexed as text.")
    relative = target.relative_to(root).as_posix()
    identifier = "file:" + relative
    with connection(root) as conn:
        _put(conn, identifier, "file", relative, data.decode("utf-8-sig", "replace"),
             source_path=relative, source_hash=hashlib.sha256(data).hexdigest())
    return {"id": identifier, "source_path": relative}


def _document_source(root: Path, path: str | Path) -> Path:
    source = Path(path).resolve(strict=True)
    if (not source.is_file() or not source.is_relative_to(root)
            or source.suffix.lower() not in DOCUMENT_SUFFIXES
            or source.stat().st_size > MAX_DOCUMENT_BYTES
            or any(SECRET_NAME.search(part) for part in source.relative_to(root).parts)):
        raise ValueError("Choose a supported document inside this project")
    return source


def index_document_text(folder: str | Path, path: str | Path, text: str,
                        source_hash: str, revision: str = "v1") -> dict:
    """Index inspected document text in small source-checked FTS records."""
    root = Path(folder).resolve(strict=True)
    source = _document_source(root, path)
    relative = source.relative_to(root).as_posix()
    if not isinstance(text, str) or len(text) > 5_000_000:
        raise ValueError("Document text is too large to index")
    if not re.fullmatch(r"[a-f0-9]{64}", source_hash):
        raise ValueError("A verified source hash is required")
    chunks = [text[start:start + DOCUMENT_CHUNK_CHARS]
              for start in range(0, len(text), DOCUMENT_CHUNK_CHARS)]
    with connection(root) as conn:
        existing = conn.execute(
            "SELECT COUNT(*),MIN(source_hash),MAX(source_hash),MIN(tags),MAX(tags) FROM memories "
            "WHERE kind='document' AND source_path=?", (relative,),
        ).fetchone()
        if (existing[0] == len(chunks) and existing[1] == existing[2] == source_hash
                and existing[3] == existing[4] == "extractor:" + revision):
            stored_chunks = dict(conn.execute(
                "SELECT id,body FROM memories WHERE kind='document' AND source_path=?",
                (relative,),
            ))
            expected_chunks = {
                f"document:{relative}:{number}": body
                for number, body in enumerate(chunks, 1)
            }
            if stored_chunks == expected_chunks:
                return {"source_path": relative, "parts": len(chunks), "updated": False}
        old = [row[0] for row in conn.execute(
            "SELECT id FROM memories WHERE kind='document' AND source_path=?", (relative,)
        )]
        for identifier in old:
            conn.execute("DELETE FROM memories WHERE id=?", (identifier,))
            conn.execute("DELETE FROM memory_fts WHERE id=?", (identifier,))
        for number, body in enumerate(chunks, 1):
            identifier = f"document:{relative}:{number}"
            _put(conn, identifier, "document", f"{relative} (part {number}/{len(chunks)})",
                 body, "extractor:" + revision, relative, source_hash)
    return {"source_path": relative, "parts": len(chunks), "updated": True}


def index(folder: str | Path, include_code: bool = False, max_files: int = 800) -> dict:
    """Incrementally index project documents and optionally source files.

    A full code scan is explicit and bounded. Ordinary searches refresh only the
    small plan/progress/README set, keeping query latency low.
    """
    root = Path(folder).resolve(strict=True)
    max_files = max(1, min(int(max_files), 2000))
    paths = [root / relative for relative in DEFAULT_FILES]
    truncated = False
    if include_code:
        count = 0
        for base, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                             and not SECRET_NAME.search(d) and not (Path(base) / d).is_symlink()
                             and not (Path(base) / d).is_junction())
            for name in sorted(names):
                path = Path(base) / name
                if path not in paths:
                    paths.append(path)
                count += 1
                if count >= max_files:
                    truncated = True
                    break
            if truncated:
                break
    changed = unchanged = skipped = 0
    seen = set()
    with connection(root) as conn:
        for path in paths:
            if not _eligible(root, path):
                continue
            relative = path.resolve().relative_to(root).as_posix()
            identifier = "file:" + relative
            try:
                data = path.read_bytes()
            except OSError:
                # An editor may replace or lock one file mid-scan. Remove its
                # stale text and continue indexing the rest of the project.
                conn.execute("DELETE FROM memories WHERE id=?", (identifier,))
                conn.execute("DELETE FROM memory_fts WHERE id=?", (identifier,))
                skipped += 1
                continue
            if b"\x00" in data:
                continue
            digest = hashlib.sha256(data).hexdigest()
            seen.add(identifier)
            previous = conn.execute("SELECT source_hash FROM memories WHERE id=?", (identifier,)).fetchone()
            if previous and previous["source_hash"] == digest:
                unchanged += 1
                continue
            text = data.decode("utf-8-sig", errors="replace")
            _put(conn, identifier, "file", relative, text, "", relative, digest)
            changed += 1
        # Remove vanished index entries only when the whole source walk was seen.
        existing_files = {row[0] for row in conn.execute("SELECT id FROM memories WHERE kind='file'")}
        if include_code and not truncated:
            stale = list(existing_files - seen)
        else:
            # Inspected paths that failed eligibility or text validation must
            # lose their old records too. Preserve files outside this scan.
            inspected = set()
            for path in paths:
                try:
                    # Indexing stores resolved source identities, so invalid
                    # aliases must evict that same canonical record too.
                    resolved = path.resolve()
                    if resolved.is_relative_to(root):
                        inspected.add("file:" + resolved.relative_to(root).as_posix())
                except (OSError, RuntimeError, ValueError):
                    continue  # Unresolvable or escaped aliases provide no safe identity.
            stale = list(existing_files & (inspected - seen))
        for identifier in stale:
            conn.execute("DELETE FROM memories WHERE id=?", (identifier,))
            conn.execute("DELETE FROM memory_fts WHERE id=?", (identifier,))
    return {"database": str(database_path(root)), "indexed": changed,
            "unchanged": unchanged, "skipped": skipped,
            "removed": len(stale), "truncated": truncated}


def _refresh_file_record(root: Path, row, checked_documents: dict | None = None) -> bool:
    """Recheck a recalled file before using indexed text as evidence.

    Code scans are explicit and bounded, but a search hit may outlive a later
    edit or deletion. Refresh only the touched hit rather than walking the
    whole project on every memory lookup.
    """
    if row["kind"] == "document":
        relative = row["source_path"]
        if row["tags"] in LEGACY_DOCUMENT_EXTRACTORS:
            # Hash equality cannot validate old extracted text after a transient
            # source edit. Retire only this recalled source's legacy chunks;
            # the next explicit document inspect rebuilds verified v3 text.
            key = ("legacy-extractor", relative)
            if checked_documents is None or key not in checked_documents:
                with connection(root) as conn:
                    placeholders = ",".join("?" for _ in LEGACY_DOCUMENT_EXTRACTORS)
                    predicate = "kind='document' AND source_path=? AND tags IN (" + placeholders + ")"
                    values = (relative, *LEGACY_DOCUMENT_EXTRACTORS)
                    ids = [item[0] for item in conn.execute("SELECT id FROM memories WHERE " + predicate, values)]
                    for identifier in ids:
                        deleted = conn.execute("DELETE FROM memories WHERE id=? AND " + predicate,
                                               (identifier, *values))
                        if deleted.rowcount:
                            conn.execute("DELETE FROM memory_fts WHERE id=?", (identifier,))
                if checked_documents is not None:
                    checked_documents[key] = True
            return True
        if checked_documents is not None and relative in checked_documents:
            return checked_documents[relative]
        try:
            source = _document_source(root, root / relative)
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            changed = digest.hexdigest() != row["source_hash"]
        except (OSError, RuntimeError, ValueError):
            changed = True
        if changed:
            # Extracted text belongs to exact source bytes. Never serve it
            # after an edit; the next inspect_project_file call re-extracts.
            with connection(root) as conn:
                ids = [item[0] for item in conn.execute(
                    "SELECT id FROM memories WHERE kind='document' AND source_path=?",
                    (relative,),
                )]
                for identifier in ids:
                    conn.execute("DELETE FROM memories WHERE id=?", (identifier,))
                    conn.execute("DELETE FROM memory_fts WHERE id=?", (identifier,))
        if checked_documents is not None:
            checked_documents[relative] = changed
        return changed
    if row["kind"] != "file":
        return False
    path = root / row["source_path"]
    if not _eligible(root, path):
        with connection(root) as conn:
            conn.execute("DELETE FROM memories WHERE id=?", (row["id"],))
            conn.execute("DELETE FROM memory_fts WHERE id=?", (row["id"],))
        return True
    try:
        # Stored file identities are canonical. A replacement symlink must not
        # refresh a different record while leaving this old record readable.
        if path.resolve(strict=True).relative_to(root).as_posix() != row["source_path"]:
            raise ValueError("Indexed source identity changed.")
        data = path.read_bytes()
    except (OSError, ValueError):
        with connection(root) as conn:
            conn.execute("DELETE FROM memories WHERE id=?", (row["id"],))
            conn.execute("DELETE FROM memory_fts WHERE id=?", (row["id"],))
        return True
    if b"\x00" in data:
        with connection(root) as conn:
            conn.execute("DELETE FROM memories WHERE id=?", (row["id"],))
            conn.execute("DELETE FROM memory_fts WHERE id=?", (row["id"],))
        return True
    digest = hashlib.sha256(data).hexdigest()
    if digest == row["source_hash"]:
        return False
    try:
        index_file(root, path)
    except (OSError, ValueError):
        # The source can change again between this read and the reindex read.
        # Hide the old hit until a later successful scan sees stable bytes.
        with connection(root) as conn:
            conn.execute("DELETE FROM memories WHERE id=?", (row["id"],))
            conn.execute("DELETE FROM memory_fts WHERE id=?", (row["id"],))
    return True


def search(folder: str | Path, query: str, kind: str = "", limit: int = 8) -> list[dict]:
    """Return ranked snippets, not entire files, from the local FTS5 index."""
    if kind and kind not in KINDS:
        raise ValueError("Unknown memory kind.")
    root = Path(folder).resolve(strict=True)
    index(root)
    # unicode61 does not expand characters such as ß into multiple letters.
    # Full casefolding would turn an exact source term into a different token.
    words = list(dict.fromkeys(
        word.lower() for word in WORD.findall(query)
        if word.lower() not in SEARCH_STOPWORDS
    ))[:12]
    limit = max(1, min(int(limit), 20))
    def query_rows():
        with connection(root) as conn:
            if words:
                def matches(expression):
                    return conn.execute("""SELECT m.id,m.kind,m.title,m.body,m.tags,m.source_path,
                    m.source_hash,m.updated,
                    snippet(memory_fts, 2, '[', ']', ' … ', 24) AS excerpt
                    FROM memory_fts JOIN memories AS m ON m.id=memory_fts.id
                    WHERE memory_fts MATCH ? AND (?='' OR m.kind=?)
                    ORDER BY bm25(memory_fts, 0.0, 4.0, 1.0, 2.0), m.updated DESC LIMIT ?""",
                    (expression, kind, kind, limit * 5)).fetchall()
                exact = matches(" AND ".join('"' + word + '"' for word in words))
                if exact:
                    return exact
                # Quoted/asterisk input may be a literal example of FTS syntax;
                # keep useful plain-text matches while never executing it raw.
                minimum = 1 if len(words) == 1 or re.search(r'["*]', query) else 2
                if minimum == 1:
                    expression = " OR ".join('"' + word + '"' for word in words)
                else:
                    # Require a pair before LIMIT is applied, so single-term
                    # hits cannot crowd valid fallback results out of the window.
                    expression = " OR ".join(
                        f'("{left}" AND "{right}")'
                        for number, left in enumerate(words)
                        for right in words[number + 1:]
                    )
                return matches(expression)
            return conn.execute("""SELECT id,kind,title,body,tags,source_path,
                source_hash,updated,substr(body,1,500) AS excerpt FROM memories
                WHERE (?='' OR kind=?) ORDER BY updated DESC LIMIT ?""",
                (kind, kind, limit * 5)).fetchall()

    checked_documents = {}
    for _ in range(3):
        rows = query_rows()
        changed = [_refresh_file_record(root, row, checked_documents) for row in rows]
        if not any(changed):
            break
    else:
        # A large number of stale hits or rapid edits must not surface old text.
        rows = [row for row in query_rows()
                if not _refresh_file_record(root, row, checked_documents)]
    return [{key: row[key] for key in ("id", "kind", "title", "tags", "source_path", "updated", "excerpt")}
            for row in rows[:limit]]


def get(folder: str | Path, memory_id: str) -> dict:
    """Fetch one complete record after search identifies it."""
    root = Path(folder).resolve(strict=True)
    with connection(root) as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    if row is None:
        raise ValueError("Memory was not found in this project.")
    legacy_document = row["kind"] == "document" and row["tags"] in LEGACY_DOCUMENT_EXTRACTORS
    if _refresh_file_record(root, row):
        with connection(root) as conn:
            row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            if legacy_document:
                raise ValueError("This document memory is outdated; use inspect_project_file on its source to rebuild it.")
            raise ValueError("Memory source file was removed or is no longer safe to read.")
    return dict(row)
