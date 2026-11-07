"""Local project memory: durable SQLite records plus a fast full-text file index.

Project files remain authoritative. The database is a rebuildable search index
and a small store for explicit decisions, lessons, components, and skill recipes.
No service, account, embeddings download, or cloud request is required.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import stat
import time
import unicodedata
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


def _artifact_project_root(folder: str | Path) -> Path:
    """Keep cached project artifacts contained, including Windows reparse points."""
    root=Path(folder).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Cached artifacts need their own project directory.')
    notes=root/'.local-ai';database=notes/'memory.sqlite3'
    for path in (notes,database):
        if path.is_symlink() or (path.exists() and getattr(path.lstat(),'st_file_attributes',0)&0x400):
            raise ValueError('Cached artifact state must stay in its own project; links are not allowed.')
        if not path.resolve().is_relative_to(root):
            raise ValueError('Cached artifact state must stay in its own project.')
    return root


def database_path(folder: str | Path) -> Path:
    """Keep the index with its project so it survives app source updates."""
    root = _artifact_project_root(folder)
    state = root / ".local-ai"
    state.mkdir(exist_ok=True)
    database = state / "memory.sqlite3"
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            info = database.with_name(database.name + suffix).lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("SQLite state must stay in its own project; links are not allowed.")
    return database


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
        raise ValueError("Memory needs a title and 1Ã¢â‚¬â€œ16,000 characters of content.")
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
        info = resolved.stat()
        return (resolved.is_file() and not any(SECRET_NAME.search(part) for part in relative.parts[:-1])
                and info.st_nlink == 1 and info.st_size <= 256_000)
    except (OSError, RuntimeError):
        return False


def _indexed_file_identity_matches(root: Path, relative: str) -> bool:
    """Keep a derived identity only while its current eligible path is provable."""
    path = root / relative
    try:
        return (_eligible(root, path)
                and path.resolve(strict=True).relative_to(root).as_posix() == relative)
    except (OSError, RuntimeError, ValueError):
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
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + DOCUMENT_CHUNK_CHARS, len(text))
        boundary = end
        # FTS indexes each chunk independently. Avoid turning an ordinary
        # word into two unsearchable fragments at a fixed character boundary.
        while (boundary > start and boundary < len(text)
               and (text[boundary - 1].isalnum()
                    or unicodedata.category(text[boundary - 1]) == "Co"
                    or unicodedata.category(text[boundary - 1]).startswith("M"))
               and (text[boundary].isalnum()
                    or unicodedata.category(text[boundary]) == "Co"
                    or unicodedata.category(text[boundary]).startswith("M"))):
            boundary -= 1
        # A token longer than the chunk budget still requires a hard split.
        if boundary > start:
            end = boundary
        chunks.append(text[start:end])
        start = end
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
                             and not SECRET_NAME.search(d) and not (Path(base) / d).is_symlink())
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
        existing_files = {row[0] for row in conn.execute("SELECT id FROM memories WHERE kind='file'")}
        # Even a complete walk excludes SKIP_DIRS. Explicitly indexed files in
        # those directories must not disappear merely because they were unvisited.
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
        stale_ids = existing_files & (inspected - seen)
        if include_code and not truncated:
            # Retire vanished or ineligible sources outside the inspected set,
            # without discarding valid records from excluded directories.
            stale_ids.update(identifier for identifier in existing_files - seen
                             if not _indexed_file_identity_matches(root, identifier[len("file:"):]))
        stale = list(stale_ids)
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


def _query_words(query: str) -> list[str]:
    """Keep a combining mark with its base word before SQLite tokenization.

    Splitting cafe followed by an accent into two AND terms cannot match the
    single unicode61 token stored in the index. Private-use characters are also
    token characters in unicode61. Quotes/operators remain data.
    """
    words, current = [], []
    for character in query:
        category = unicodedata.category(character)
        if character.isalnum() or category == "Co" or current and category.startswith("M"):
            current.append(character)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return words


def search(folder: str | Path, query: str, kind: str = "", limit: int = 8) -> list[dict]:
    """Return ranked snippets, not entire files, from the local FTS5 index."""
    if kind and kind not in KINDS:
        raise ValueError("Unknown memory kind.")
    root = Path(folder).resolve(strict=True)
    index(root)
    # unicode61 does not expand characters such as ÃƒÅ¸ into multiple letters.
    # Full casefolding would turn an exact source term into a different token.
    words = list(dict.fromkeys(
        word.lower() for word in _query_words(query)
        if word.lower() not in SEARCH_STOPWORDS
    ))[:12]
    limit = max(1, min(int(limit), 20))
    def query_rows():
        with connection(root) as conn:
            if words:
                def matches(expression):
                    return conn.execute("""SELECT m.id,m.kind,m.title,m.body,m.tags,m.source_path,
                    m.source_hash,m.updated,
                    snippet(memory_fts, 2, '[', ']', ' Ã¢â‚¬Â¦ ', 24) AS excerpt
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


# Scoped host verification evidence; no model-declared success authority.
_VERIFICATION_RECEIPT_BYTES = 32000
from contextvars import ContextVar
_VERIFICATION_PROJECT = ContextVar('verification_project', default=None)


@contextmanager
def verification_context(project):
    """Host-owned task context; never an argument exposed to the model."""
    token=_VERIFICATION_PROJECT.set(project)
    try:yield
    finally:_VERIFICATION_PROJECT.reset(token)


# Verification is an explicit disk operation, never a stat-only digest cache.
_VERIFICATION_MAX_BYTES = 2 * 1024**3
_VERIFICATION_MAX_FILES = 16384
_VERIFICATION_MAX_VISITED = 65536
_VERIFICATION_MAX_PATH_BYTES = 4 * 1024**2
_VERIFICATION_MAX_SECONDS = 60.0
_VERIFICATION_READ_BYTES = 1024**2


def _verification_file_identity(value):
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def verification_source_state(folder, *, deadline=None):
    """Stream a bounded, complete source manifest or label it explicitly limited.

    Hash every included byte on each capture: size/mtime alone are not content
    identity. Before/open/after file identity and directory rechecks detect
    ordinary edits/replacements/listing races. This is not a filesystem snapshot
    or a sandbox against deliberately coordinated same-user tampering.
    """
    import json
    import stat
    if deadline is not None:
        try:
            valid_deadline = type(deadline) in (int, float) and math.isfinite(deadline)
        except (OverflowError, ValueError):
            valid_deadline = False
        if not valid_deadline:
            raise ValueError('Source deadline must be a finite monotonic timestamp')
    root = _artifact_project_root(folder)
    started=time.monotonic()
    entries=[]; pending=[root]; directories=[]; files=[]; total=consumed=visited=path_bytes=0
    omitted=0; reasons=set(); exhausted=False; excluded_roots=[]; excluded_path_bytes=0
    excluded={'.git','__pycache__'}
    runtime={'.venv','venv','node_modules','Toolbox'}
    def limited(reason):
        nonlocal omitted
        omitted+=1;reasons.add(reason)
    def expired():
        # Advisory callers may shorten this budget, never extend the scanner's
        # own limit. Default callers keep the original expiry calculation.
        return (time.monotonic()-started>=_VERIFICATION_MAX_SECONDS
                or (deadline is not None and time.monotonic()>=deadline))
    def unsafe(value):
        return stat.S_ISLNK(value.st_mode) or bool(getattr(value,'st_file_attributes',0)&0x400)
    while pending and not exhausted:
        if expired():limited('elapsed_budget');break
        directory=pending.pop()
        try:
            initial=directory.stat(follow_symlinks=False)
            if unsafe(initial) or not stat.S_ISDIR(initial.st_mode):
                limited('linked_or_changed_directory');continue
            with os.scandir(directory) as children:
                for item in children:
                    if expired():limited('elapsed_budget');exhausted=True;break
                    if visited>=_VERIFICATION_MAX_VISITED:
                        limited('visited_budget');exhausted=True;break
                    visited+=1
                    path=Path(item.path)
                    relative=path.relative_to(root).as_posix()
                    path_bytes+=len(relative.encode('utf-8'))
                    if path_bytes>_VERIFICATION_MAX_PATH_BYTES:
                        limited('path_budget');exhausted=True;break
                    try:
                        before=path.stat(follow_symlinks=False)
                        if unsafe(before):limited('linked_path');continue
                        if stat.S_ISDIR(before.st_mode):
                            if item.name in excluded:continue
                            if item.name in runtime:
                                limited('excluded_runtime')
                                excluded_path_bytes+=len(relative.encode('utf-8'))
                                if len(excluded_roots)>=128 or excluded_path_bytes>8192:
                                    limited('excluded_runtime_budget');exhausted=True;break
                                excluded_roots.append(relative);continue
                            if path==root/'.local-ai':
                                # Mutable task metadata is not app source. Code
                                # or nested/linked assets here prevent full scope.
                                from itertools import islice
                                with os.scandir(path) as state_children:
                                    state_items=list(islice(state_children,2049))
                                if (len(state_items)>2048 or any(child.is_dir(follow_symlinks=False)
                                        or child.is_symlink() or getattr(child.stat(follow_symlinks=False),'st_file_attributes',0)&0x400
                                        or Path(child.name).suffix.lower() in {'.js','.mjs','.html','.css','.py'}
                                        for child in state_items)):
                                    limited('task_state_assets')
                                continue
                            pending.append(path);continue
                        if not stat.S_ISREG(before.st_mode):limited('special_file');continue
                        if len(entries)>=_VERIFICATION_MAX_FILES:
                            limited('file_budget');exhausted=True;break
                        if before.st_size<0 or consumed+before.st_size>_VERIFICATION_MAX_BYTES:
                            limited('byte_budget');continue
                        digest=hashlib.sha256();size=0;complete=True
                        with path.open('rb') as stream:
                            opened=os.fstat(stream.fileno())
                            # Windows Path.stat and CRT fstat expose different ctime
                            # semantics. Compare all identity fields within each
                            # API; bridge path/descriptor by dev/ino/size/mtime.
                            if _verification_file_identity(before)[:4]!=_verification_file_identity(opened)[:4]:
                                limited('changed_file');continue
                            while True:
                                if expired():
                                    limited('elapsed_budget');exhausted=True;complete=False;break
                                chunk=stream.read(min(_VERIFICATION_READ_BYTES,
                                                      _VERIFICATION_MAX_BYTES-consumed+1))
                                if not chunk:break
                                consumed+=len(chunk);size+=len(chunk)
                                if consumed>_VERIFICATION_MAX_BYTES:
                                    limited('byte_budget');exhausted=True;complete=False;break
                                digest.update(chunk)
                            after_open=os.fstat(stream.fileno())
                        after=path.stat(follow_symlinks=False)
                        if (unsafe(after) or _verification_file_identity(opened)!=_verification_file_identity(after_open)
                                or _verification_file_identity(before)!=_verification_file_identity(after)
                                or size!=before.st_size):
                            limited('changed_file');continue
                        if complete:
                            total+=size;entries.append([relative,digest.hexdigest()])
                            files.append((path,_verification_file_identity(after)))
                    except (OSError,ValueError):
                        limited('unavailable_path')
            after_directory=directory.stat(follow_symlinks=False)
            if unsafe(after_directory) or _verification_file_identity(initial)!=_verification_file_identity(after_directory):
                limited('changed_directory')
            directories.append((directory,_verification_file_identity(after_directory)))
        except (OSError,ValueError):
            limited('unavailable_directory')
    # A new/deleted/renamed entry in an already scanned directory must not turn
    # an incomplete listing into a full-source claim while later folders hash.
    for directory,identity in directories:
        if expired():limited('elapsed_budget');break
        try:
            current=directory.stat(follow_symlinks=False)
            if unsafe(current) or _verification_file_identity(current)!=identity:
                limited('changed_directory')
        except (OSError,ValueError):limited('unavailable_directory')
    # Content edits do not change the parent directory's identity. Recheck each
    # included file after all reads, so an early file edited while a later large
    # asset streams cannot retain a complete-scope claim. This bounded metadata
    # pass does not read content again and shares the original elapsed budget.
    for path,identity in files:
        if expired():limited('elapsed_budget');break
        try:
            current=path.stat(follow_symlinks=False)
            if unsafe(current) or _verification_file_identity(current)!=identity:
                limited('changed_file')
        except (OSError,ValueError):limited('unavailable_path')
    if expired():limited('elapsed_budget')
    if pending:omitted+=len(pending);reasons.add('unvisited_directories')
    # Name intentional runtime exclusions in the content identity. This scope
    # can be used only with a complete actual local dependency observation;
    # served runtime assets remain unverified, even when this scan is complete.
    application_only=bool(excluded_roots) and reasons=={'excluded_runtime'}
    manifest={'files':sorted(entries),'excluded_runtime_roots':sorted(excluded_roots)} if excluded_roots else sorted(entries)
    encoded=json.dumps(manifest,separators=(',',':')).encode()
    return {'sha256':hashlib.sha256(encoded).hexdigest(),'files':len(entries),'bytes':total,
            'omitted':omitted,'scope':('application_source' if application_only else
                                     'limited_dependency_manifest' if omitted else 'full_project_source'),
            'limitations':sorted(reasons),'excluded_runtime_roots':sorted(excluded_roots)}


def verification_basis(folder, project=None):
    """Use full original instructions; never substitute an empty followup hash."""
    root=_artifact_project_root(folder)
    if project is None:
        project=_VERIFICATION_PROJECT.get()
    if project is None:
        # CLI verifiers have no in-process task context. Read an existing task
        # registry without creating/migrating it or blocking behind a writer.
        import long_tasks
        database=long_tasks.STATE/'projects.db'
        if database.is_file():
            try:
                from contextlib import closing
                with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=.2)) as conn:
                    row=conn.execute('SELECT brief,folder FROM projects WHERE folder=? LIMIT 1',(str(root),)).fetchone()
                if row:project={'brief':row[0],'folder':row[1]}
            except sqlite3.Error:pass
    if not project or Path(project['folder']).resolve()!=root:return None
    basis={'brief':hashlib.sha256(str(project.get('brief','')).encode()).hexdigest()}
    for name,path in (('rules',root/'AGENTS.md'),('followups',root/'.local-ai/USER-REQUESTS.md')):
        basis[name]=None
        if path.is_file():
            with path.open('rb') as stream:basis[name]=hashlib.file_digest(stream,'sha256').hexdigest()
    return basis


def _verification_digest(value):
    import json
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def record_host_check_receipt(folder, checker, arguments, report, source_before, *, checker_file, basis_before=None, loaded_checker_sha256=None, execution_permit=None):
    """Called only after an actual host check. Optional cache errors never alter verdicts."""
    import json
    import task_context
    if not _consume_host_check_permit(execution_permit,folder,checker,arguments,report,source_before,
            checker_file=checker_file,basis_before=basis_before,loaded_checker_sha256=loaded_checker_sha256):return None
    root=_artifact_project_root(folder)
    basis=verification_basis(root)
    after=verification_source_state(root)
    checker_hash=hashlib.sha256(Path(checker_file).read_bytes()).hexdigest()
    if loaded_checker_sha256 is None or loaded_checker_sha256!=checker_hash:return None
    if basis!=basis_before or source_before!=after or report.get('passed') is not True:return None
    encoded_report=json.dumps(report,sort_keys=True,allow_nan=False)
    if len(encoded_report.encode())>1024*1024:return None
    suite=report.get('suite_sha256')
    if suite is None:
        suite=_verification_digest(arguments)
    witnesses=report.get('persistence_witnesses',[])[:32]
    kind=report.get('check_tool',checker)
    structured=kind in {'check_python_function','check_javascript_function','check_webapp','check_webapp_saved','check_document_report'}
    level='acceptance_passed' if structured else 'process_exit_zero'
    if basis is None:level+='_unbound'
    value={'version':1,'observed_at':time.time(),'project':hashlib.sha256(str(root).encode()).hexdigest(),
           'planning_basis':basis,'request_hash':task_context.request_identity(root),
           'checker':checker,'check_sha256':_verification_digest(arguments),'bound_step_check_sha256':report.get('check_sha256'),'suite_sha256':suite,
           'checker_sha256':checker_hash,
           'source_before':source_before,'source_after':after,'report_sha256':hashlib.sha256(encoded_report.encode()).hexdigest(),
           'passed':True,'verification_level':level,'dependency_scope':report.get('dependency_scope','not_observed'),
           'training_eligible':False,'persistence_witnesses':witnesses}
    raw=json.dumps(value,sort_keys=True,allow_nan=False)
    if len(raw.encode())>_VERIFICATION_RECEIPT_BYTES:return None
    identifier=_verification_digest(value)
    with connection(root) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS verification_receipts (id TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO verification_receipts VALUES(?,?,?)',(identifier,raw,time.time()))
        conn.execute('DELETE FROM verification_receipts WHERE id NOT IN (SELECT id FROM verification_receipts ORDER BY created DESC LIMIT 64)')
    return identifier


def current_check_receipts(folder, project, *, checker_file, source_status=None):
    """Revalidate local cache integrity/freshness; this does not execute any check."""
    import json
    import task_context
    root=_artifact_project_root(folder);basis=verification_basis(root,project)
    current=verification_source_state(root)
    if isinstance(source_status,dict):
        source_status.update(scope=current['scope'],limitations=current.get('limitations',[])[:16])
    runner=hashlib.sha256(Path(checker_file).read_bytes()).hexdigest()
    with connection(root) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='verification_receipts'").fetchone():return []
        rows=conn.execute('SELECT id,substr(payload,1,32001) AS payload,length(CAST(payload AS BLOB)) AS bytes FROM verification_receipts ORDER BY created DESC LIMIT 64').fetchall()
    return _validated_check_receipts(rows, root, basis, current, runner)


# Opaque permits bind recording to a real checker return in this process. This
# prevents ordinary import/API fabrication; it is not a sandbox against the
# same user deliberately monkeypatching modules or editing the database.
_HOST_CHECK_PERMITS = {}
_HOST_CHECK_CODE = {}


def _host_report_binding(folder, checker, arguments, report, source_before, *, checker_file, basis_before, loaded_checker_sha256):
    import json
    raw=json.dumps(report,sort_keys=True,allow_nan=False)
    if len(raw.encode())>1024*1024:return None
    return _verification_digest([str(Path(folder).resolve()),checker,arguments,
        hashlib.sha256(raw.encode()).hexdigest(),source_before,str(Path(checker_file).resolve()),
        basis_before,loaded_checker_sha256])


def _issue_host_check_permit(folder, checker, arguments, report, source_before, *, checker_file, basis_before=None, loaded_checker_sha256=None):
    """Only exact owned execution code may bind its actual completed report.

    Names, filenames or supplied passed=True are insufficient. Check the live
    module globals and function identity, then compare its code object with the
    owned source compiled without execution. This also supports real __main__
    CLI aliases and rejects stale bytecode/hot-replaced source.
    """
    import sys
    import types
    frame=sys._getframe(1)
    name=frame.f_globals.get('__name__')
    module=sys.modules.get(name)
    if name not in {'native_agent','webapp_check','__main__'} or module is None or vars(module) is not frame.f_globals:return None
    path=Path(getattr(module,'__file__','')).resolve()
    allowed={'native_agent':{'execute_tool','_run_function_check_cli'},'webapp_check':{'run_saved_checks'}}
    if frame.f_code.co_name not in allowed.get(path.stem,set()):return None
    function=frame.f_globals.get(frame.f_code.co_name)
    if getattr(function,'__code__',None) is not frame.f_code:return None
    if Path(frame.f_code.co_filename).resolve()!=path or Path(checker_file).resolve()!=path:return None
    if loaded_checker_sha256!=frame.f_globals.get('_LOADED_CHECKER_SHA256'):return None
    with path.open('rb') as stream:source=stream.read(2*1024*1024+1)
    if len(source)>2*1024*1024 or hashlib.sha256(source).hexdigest()!=loaded_checker_sha256:return None
    key=(str(path),loaded_checker_sha256,frame.f_code.co_name,frame.f_code.co_filename,sys.implementation.cache_tag)
    expected=_HOST_CHECK_CODE.get(key)
    if expected is None:
        try:compiled=compile(source,frame.f_code.co_filename,'exec',dont_inherit=True,optimize=sys.flags.optimize)
        except (SyntaxError,ValueError,TypeError):return None
        code=next((value for value in compiled.co_consts if isinstance(value,types.CodeType) and value.co_name==frame.f_code.co_name),None)
        if code is None:return None
        expected=code
        if len(_HOST_CHECK_CODE)>=8:_HOST_CHECK_CODE.pop(next(iter(_HOST_CHECK_CODE)))
        _HOST_CHECK_CODE[key]=expected
    if frame.f_code!=expected:return None
    if not isinstance(report,dict) or report.get('passed') is not True:return None
    binding=_host_report_binding(folder,checker,arguments,report,source_before,checker_file=checker_file,
        basis_before=basis_before,loaded_checker_sha256=loaded_checker_sha256)
    if binding is None:return None
    now=time.monotonic()
    for token in list(_HOST_CHECK_PERMITS):
        if _HOST_CHECK_PERMITS[token]['expires']<now:del _HOST_CHECK_PERMITS[token]
    if len(_HOST_CHECK_PERMITS)>=64:_HOST_CHECK_PERMITS.pop(next(iter(_HOST_CHECK_PERMITS)))
    token=uuid.uuid4().hex
    _HOST_CHECK_PERMITS[token]={'binding':binding,'expires':now+120}
    return token


def _consume_host_check_permit(token, folder, checker, arguments, report, source_before, *, checker_file, basis_before, loaded_checker_sha256):
    value=(_HOST_CHECK_PERMITS.pop(token,None) if isinstance(token,str) and len(token)==32 and re.fullmatch(r'[a-f0-9]{32}',token) else None)
    if value is None or value['expires']<time.monotonic():return False
    binding=_host_report_binding(folder,checker,arguments,report,source_before,checker_file=checker_file,
        basis_before=basis_before,loaded_checker_sha256=loaded_checker_sha256)
    return binding is not None and binding==value['binding']



def _helper_request_identity(root):
    """Read the LIVE registered owner; a verification ContextVar is not authority."""
    import long_tasks
    from contextlib import closing
    root=_artifact_project_root(root)
    expected=_VERIFICATION_PROJECT.get()
    database=Path(long_tasks.STATE)/'projects.db'
    try:
        with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as conn:
            if expected is not None:
                if Path(expected['folder']).resolve()!=root:raise ValueError('Wrong helper project owner.')
                rows=conn.execute('SELECT id,folder,brief FROM projects WHERE id=? LIMIT 2',(expected['id'],)).fetchall()
            else:
                rows=conn.execute('SELECT id,folder,brief FROM projects WHERE folder=? COLLATE NOCASE LIMIT 2',(str(root),)).fetchall()
        if len(rows)!=1 or Path(rows[0][1]).resolve()!=root:raise ValueError('Helper reuse needs the current registered project instructions.')
        basis=verification_basis(root,{'id':rows[0][0],'folder':rows[0][1],'brief':rows[0][2]})
        return _verification_digest({'owner':rows[0][0],'basis':basis})
    except (sqlite3.Error,OSError,KeyError,TypeError) as error:
        raise ValueError('Helper reuse needs the current registered project instructions.') from error


_HELPER_RECORD_BYTES=100000
_HELPER_DEFINITION_BYTES=32000
_HELPER_CASE_BYTES=64000
_HELPER_RECORD_LIMIT=128
_HELPER_RECORD_FORMAT='cale-project-expression-record-v1'


def _helper_bytes(value):
    import json
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def _helper_digest(value):
    return hashlib.sha256(_helper_bytes(value)).hexdigest()


@contextmanager
def _helper_owned_state(folder):
    """Immutable files are authority; no SQLite main/WAL/SHM/journal is touched.

    The process mutex serializes the bounded collection check and publication.
    Windows held directory handles prevent reparse/rename escapes. Abandoned
    locks recover after a crash; partial staging files are never authoritative.
    """
    import ctypes,design_tools
    from contextlib import ExitStack
    root=_artifact_project_root(folder)
    _helper_request_identity(root)
    if os.name!='nt':raise RuntimeError('Project helper registries require Windows ownership guards.')
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p];api.CreateMutexW.restype=ctypes.c_void_p
    api.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32];api.WaitForSingleObject.restype=ctypes.c_uint32
    api.ReleaseMutex.argtypes=[ctypes.c_void_p];api.CloseHandle.argtypes=[ctypes.c_void_p]
    mutex=api.CreateMutexW(None,False,'Local\\CaleExpression-'+hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest())
    if not mutex:raise ctypes.WinError(ctypes.get_last_error())
    acquired=False
    try:
        result=api.WaitForSingleObject(mutex,10000)
        if result not in (0,0x80):raise ValueError('Helper registry is busy; retry without changing saved work.')
        acquired=True
        with ExitStack() as stack:
            stack.enter_context(design_tools._game_directory_guard(root))
            current=root
            for name in ('.local-ai','expression-helpers','v1'):
                following=current/name
                following.mkdir(exist_ok=True)
                stack.enter_context(design_tools._game_directory_guard(root,following))
                current=following
            for name in ('records','staging'):
                following=current/name;following.mkdir(exist_ok=True)
                stack.enter_context(design_tools._game_directory_guard(root,following))
            yield root
    finally:
        if acquired:api.ReleaseMutex(mutex)
        api.CloseHandle(mutex)


def _guarded_helper_operation(function):
    from functools import wraps
    @wraps(function)
    def guarded(folder,*args,**kwargs):
        with _helper_owned_state(folder) as root:return function(root,*args,**kwargs)
    return guarded


def _helper_paths(root):
    """Bound ALL directory entries, including unrecognized names; never delete them."""
    from itertools import islice
    directory=root/'.local-ai/expression-helpers/v1/records'
    with os.scandir(directory) as stream:entries=list(islice(stream,_HELPER_RECORD_LIMIT+1))
    if len(entries)>_HELPER_RECORD_LIMIT:raise ValueError('Helper record collection exceeds its bounded 128-entry scan.')
    return [Path(item.path) for item in entries]


def _helper_read(path):
    """Read a plain, single-link file through its checked, non-replaceable handle."""
    import ctypes,msvcrt,design_tools
    api=design_tools._game_win_api()
    handle=api.CreateFileW(design_tools._game_win_path(path),0x80000000,1,None,3,0x00200080,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes=(ctypes.c_uint32*2)()
        if not api.GetFileInformationByHandleEx(handle,9,ctypes.byref(attributes),ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes[0]&(0x400|0x10) or design_tools._game_win_final(api,handle)!=os.path.normcase(str(path)):
            raise ValueError('Helper records must be plain project-owned files.')
        fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY);handle=None
        with os.fdopen(fd,'rb') as stream:
            if os.fstat(stream.fileno()).st_nlink!=1:raise ValueError('Helper record hard links are not owned state.')
            raw=stream.read(_HELPER_RECORD_BYTES+1)
        if not 0<len(raw)<=_HELPER_RECORD_BYTES:raise ValueError('Stored helper record exceeds its byte budget.')
        return raw
    finally:
        if handle is not None:api.CloseHandle(handle)


def _helper_json_depth(raw):
    # Check nesting before JSON decoding/recursive canonicalization. Quoted
    # delimiters and escaped quotes do not contribute to structural depth.
    quoted=escaped=False;depth=0
    for byte in raw:
        if quoted:
            if escaped:escaped=False
            elif byte==92:escaped=True
            elif byte==34:quoted=False
        elif byte==34:quoted=True
        elif byte in (91,123):
            depth+=1
            if depth>16:raise ValueError('Stored helper record exceeds its nesting budget.')
        elif byte in (93,125):depth-=1


def _validated_expression_record(root,path):
    import json,quick_calculator
    raw=_helper_read(path);_helper_json_depth(raw)
    try:
        row=json.loads(raw)
        fields={'format','id','name','definition','cases','source_hash','cases_hash','runner_hash','request_hash','created'}
        if not isinstance(row,dict) or set(row)!=fields or row['format']!=_HELPER_RECORD_FORMAT:raise ValueError('Invalid record shape')
        if _helper_bytes(row)!=raw:raise ValueError('Noncanonical record')
        for key in ('id','source_hash','cases_hash','runner_hash','request_hash'):
            if not isinstance(row[key],str) or not re.fullmatch('[a-f0-9]{64}',row[key]):raise ValueError('Invalid identity')
        if path.name!=row['id']+'-'+hashlib.sha256(raw).hexdigest()+'.json':raise ValueError('Record filename mismatch')
        if type(row['created']) not in (int,float) or not math.isfinite(row['created']) or row['created']<=0:raise ValueError('Invalid date')
        definition=row['definition']
        if not isinstance(definition,dict) or set(definition)!={'name','parameters','expression','format'}:raise ValueError('Invalid definition')
        normalized=quick_calculator.normalize_helper(definition['name'],definition['parameters'],definition['expression'])
        if normalized!=definition or normalized['name']!=row['name']:raise ValueError('Invalid definition')
        cases=_helper_cases(normalized,row['cases'],verify=False)
        if len(_helper_bytes(definition))>_HELPER_DEFINITION_BYTES or len(_helper_bytes(cases))>_HELPER_CASE_BYTES:raise ValueError('Record budgets')
        if _helper_digest(definition)!=row['source_hash'] or _helper_digest(cases)!=row['cases_hash']:raise ValueError('Changed hashes')
        if _helper_digest([str(root),row['source_hash'],row['cases_hash'],row['runner_hash'],row['request_hash']])!=row['id']:raise ValueError('Changed identity')
        return row
    except (ValueError,TypeError,KeyError,RecursionError,OverflowError) as error:
        raise ValueError('Stored helper definition or verification cases changed; reverify before reuse.') from error


def _helper_cases(definition,cases,*,verify):
    import quick_calculator
    if not isinstance(cases,list) or not 1<=len(cases)<=24:raise ValueError('Verify 1 to 24 concrete inputs/expected output cases.')
    normalized=[]
    for case in cases:
        if not isinstance(case,dict) or set(case)!={'inputs','expected'}:raise ValueError('Each helper case needs exactly inputs and expected JSON.')
        inputs=quick_calculator.helper_inputs(definition,case['inputs']);expected=quick_calculator.helper_json(case['expected'])
        if verify and not quick_calculator.helper_values_match(quick_calculator.run_helper(definition,inputs),expected):
            raise ValueError('Computed output does not match the supplied case; no helper was qualified.')
        normalized.append({'inputs':inputs,'expected':expected})
    if len(_helper_bytes(normalized))>_HELPER_CASE_BYTES:raise ValueError('Combined helper cases must be under 64 KB.')
    return normalized


def _helper_current(root,row):
    """Recompute examples; a persisted flag or hash alone never certifies reuse."""
    import quick_calculator
    runner=quick_calculator.helper_interpreter_identity();request=_helper_request_identity(root)
    if row['runner_hash']!=runner or row['request_hash']!=request:
        raise ValueError('Helper instructions or interpreter changed; reverify before reuse.')
    _helper_cases(row['definition'],row['cases'],verify=True)
    if quick_calculator.helper_interpreter_identity()!=runner or _helper_request_identity(root)!=request:
        raise ValueError('Helper interpreter or project instructions changed during verification.')


def _helper_selected(root,identifier):
    if not isinstance(identifier,str) or not re.fullmatch('[a-f0-9]{64}',identifier):raise ValueError('Choose an exact helper ID from this project.')
    for path in _helper_paths(root):
        if path.name.startswith(identifier+'-'):
            try:return _validated_expression_record(root,path)
            except (OSError,ValueError):continue
    raise ValueError('Helper was not registered in this project or its record changed; reverify before reuse.')


def _helper_publish(root,row):
    """Flush complete bytes, then rename the still-held file without replacement."""
    import ctypes,msvcrt,design_tools
    raw=_helper_bytes(row)
    if len(raw)>_HELPER_RECORD_BYTES:raise ValueError('Stored helper record exceeds its byte budget.')
    store=root/'.local-ai/expression-helpers/v1'
    target=store/'records'/(row['id']+'-'+hashlib.sha256(raw).hexdigest()+'.json')
    from itertools import islice
    with os.scandir(store/'staging') as stream:
        pending=list(islice(stream,_HELPER_RECORD_LIMIT+1))
    if len(pending)>=_HELPER_RECORD_LIMIT:
        raise ValueError('Helper staging contains 128 retained entries; inspect or archive them before publishing more. No existing bytes were deleted.')
    temporary=store/'staging'/('pending-'+uuid.uuid4().hex)
    api=design_tools._game_win_api()
    handle=api.CreateFileW(design_tools._game_win_path(temporary),0x40000000|0x10000,1,None,1,0x00200080,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:
        if design_tools._game_win_final(api,handle)!=os.path.normcase(str(temporary)):raise ValueError('Helper staging escaped its project.')
        fd=msvcrt.open_osfhandle(handle,os.O_WRONLY|os.O_BINARY);handle=None
        with os.fdopen(fd,'wb') as output:
            output.write(raw);output.flush();os.fsync(output.fileno())
            # The source handle and both destination directories remain held.
            # No path-based close/rename window and no replacement of any file.
            import quick_calculator
            if (_helper_request_identity(root)!=row['request_hash'] or quick_calculator.helper_interpreter_identity()!=row['runner_hash']):
                raise ValueError('Helper interpreter or project instructions changed during publication.')
            design_tools._game_rename_handle(msvcrt.get_osfhandle(output.fileno()),target)
    finally:
        if handle is not None:api.CloseHandle(handle)
    return target


@_guarded_helper_operation
def register_expression_helper(folder,name,parameters,expression,cases):
    """Qualify supplied examples and publish an immutable project-local record."""
    import quick_calculator
    root=_artifact_project_root(folder);request=_helper_request_identity(root)
    runner=quick_calculator.helper_interpreter_identity()
    definition=quick_calculator.normalize_helper(name,parameters,expression)
    try:verified=_helper_cases(definition,cases,verify=True)
    except ValueError as error:
        if str(error).startswith('Computed output does not match'):
            return {'registered':False,'passed':False,'note':str(error)}
        raise
    if len(_helper_bytes(definition))>_HELPER_DEFINITION_BYTES:raise ValueError('Helper definition exceeds its size budget.')
    row={'format':_HELPER_RECORD_FORMAT,'name':name,'definition':definition,'cases':verified,
         'source_hash':_helper_digest(definition),'cases_hash':_helper_digest(verified),
         'runner_hash':runner,'request_hash':request,'created':time.time()}
    row['id']=_helper_digest([str(root),row['source_hash'],row['cases_hash'],runner,request])
    _helper_current(root,row)
    paths=_helper_paths(root)
    try:
        existing=_helper_selected(root,row['id']);_helper_current(root,existing)
    except ValueError:
        if len(paths)>=_HELPER_RECORD_LIMIT:raise ValueError('This project already has 128 records; no existing bytes were overwritten.')
        _helper_publish(root,row)
    if _helper_request_identity(root)!=request or quick_calculator.helper_interpreter_identity()!=runner:
        raise ValueError('Helper interpreter or project instructions changed during publication; discard the result.')
    return {'id':row['id'],'name':name,'registered':True,'passed':True,'verified_cases':len(verified),
            'source_sha256':row['source_hash'],'cases_sha256':row['cases_hash'],'runner_sha256':runner,
            'request_sha256':request,'effect':'restricted_json_expression',
            'note':'Only supplied examples passed. No task or SQL step completed; no general correctness claim.'}


@_guarded_helper_operation
def find_expression_helpers(folder,query='',limit=8):
    """Rebuild a bounded metadata index; never expose private expression/case text."""
    import quick_calculator
    if not isinstance(query,str) or len(query)>100 or type(limit) is not int:raise ValueError('Use a short query and integer limit.')
    runner=quick_calculator.helper_interpreter_identity();request=_helper_request_identity(folder);matches=[];seen=set()
    for path in _helper_paths(folder):
        try:row=_validated_expression_record(folder,path)
        except (OSError,ValueError):continue
        if row['id'] in seen or query.casefold() not in row['name'].casefold():continue
        seen.add(row['id']);current=row['request_hash']==request and row['runner_hash']==runner
        if current:
            try:_helper_current(folder,row)
            except ValueError:continue
        matches.append({'id':row['id'],'name':row['name'],'parameters':row['definition']['parameters'],
                        'source_sha256':row['source_hash'],'current_request':row['request_hash']==request,
                        'current_interpreter':row['runner_hash']==runner,'effect':'restricted_json_expression'})
        if len(matches)>=max(1,min(limit,20)):break
    if _helper_request_identity(folder)!=request or quick_calculator.helper_interpreter_identity()!=runner:
        raise ValueError('Helper interpreter or project instructions changed during discovery.')
    return {'helpers':matches,'note':'Project-local, examples-qualified candidates only. Bounded 128-entry rebuild; invocation revalidates.'}


@_guarded_helper_operation
def run_expression_helper(folder,helper_id,inputs):
    import quick_calculator
    row=_helper_selected(folder,helper_id);_helper_current(folder,row)
    value=quick_calculator.run_helper(row['definition'],inputs)
    current=_helper_selected(folder,helper_id)
    if current!=row:raise ValueError('Helper record changed during invocation; discard the result.')
    _helper_current(folder,current)
    return {'helper_id':helper_id,'value':value,'source_sha256':row['source_hash'],
            'verified_cases_sha256':row['cases_hash'],'inference_calls':0,
            'effect':'restricted_json_expression','task_completion':False}


@_guarded_helper_operation
def expression_helper_basis(folder,helper_id):
    """Current checked identity for a saved unit, not a completion verdict."""
    row=_helper_selected(folder,helper_id);_helper_current(folder,row)
    return {key:row[key] for key in ('id','source_hash','cases_hash','runner_hash','request_hash')}


def _validated_check_receipts(rows, root, basis, current, runner, *, request_identity=None):
    """Validate the public cache contract against explicitly captured inputs.

    The ordinary wrapper still reads its live basis; the TEMP preview supplies
    its pinned request hash. Neither path creates verification authority.
    """
    import json
    import task_context
    output=[]
    for row in rows:
        if row['bytes']>_VERIFICATION_RECEIPT_BYTES:continue
        try:value=json.loads(row['payload'])
        except (ValueError,TypeError):continue
        fields={'version','observed_at','dependency_scope','project','planning_basis','request_hash','checker','check_sha256','suite_sha256',
                'checker_sha256','bound_step_check_sha256','source_before','source_after','report_sha256','passed','verification_level',
                'training_eligible','persistence_witnesses'}
        if not isinstance(value,dict) or set(value)!=fields:continue
        try:
            if _verification_digest(value)!=row['id']:continue
        except (ValueError,TypeError):continue
        if any(not isinstance(value.get(key),str) or not re.fullmatch(r'[a-f0-9]{64}',value[key])
               for key in ('project','check_sha256','suite_sha256','checker_sha256','report_sha256')):continue
        bound=value.get('bound_step_check_sha256')
        if bound is not None and (not isinstance(bound,str) or not re.fullmatch(r'[a-f0-9]{64}',bound)):continue
        if (type(value.get('observed_at')) not in (int,float) or not 0<value['observed_at']<time.time()+1
                or type(value.get('version')) is not int or value['version']!=1
                or value.get('passed') is not True or value.get('training_eligible') is not False
                or value.get('verification_level')!='acceptance_passed'
                or value.get('project')!=hashlib.sha256(str(root).encode()).hexdigest()
                or value.get('planning_basis')!=basis or value.get('request_hash')!=(request_identity() if request_identity is not None else task_context.request_identity(root))
                or _verification_digest(value.get('source_before'))!=_verification_digest(current)
                or _verification_digest(value.get('source_after'))!=_verification_digest(current)
                or value.get('checker_sha256')!=runner):continue
        output.append(value)
    return output


_RECEIPT_RO_DB_BYTES = 16 * 1024 * 1024


_RECEIPT_RO_WAL_BYTES = 8 * 1024 * 1024


_RECEIPT_RO_COPY_SECONDS = 2.0


_RECEIPT_RO_SQL_SECONDS = .1


def _receipt_file_identity(path):
    value = path.lstat()
    if (not path.is_file() or path.is_symlink()
            or getattr(value, 'st_file_attributes', 0) & 0x400):
        raise ValueError('unsafe_path')
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _receipt_source_paths(database):
    paths = [database]
    wal = database.with_name(database.name + '-wal')
    journal = database.with_name(database.name + '-journal')
    if journal.exists() or journal.is_symlink():
        raise ValueError('journal_ambiguous')
    if wal.exists() or wal.is_symlink():
        paths.append(wal)
    return paths


def _receipt_stream(path, identity, deadline, destination=None):
    """Hash every copied byte; metadata is a race check, never the content proof."""
    result = hashlib.sha256()
    total = 0
    with path.open('rb') as stream:
        value = os.fstat(stream.fileno())
        actual = (value.st_dev, value.st_ino, value.st_size,
                  value.st_mtime_ns, value.st_ctime_ns)
        # Windows path-stat and opened-handle stat can report different ctime
        # values for the same file. Compare shared identity across APIs; guard each API's full
        # ctime-inclusive tuple against its own before/after observation.
        if actual[:4] != identity[:4]:
            raise ValueError('source_changed')
        while True:
            if time.monotonic() >= deadline:
                raise ValueError('copy_budget')
            chunk = stream.read(min(1024 * 1024, identity[2] - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > identity[2]:
                raise ValueError('source_changed')
            result.update(chunk)
            if destination is not None:
                destination.write(chunk)
        value = os.fstat(stream.fileno())
        if (value.st_dev, value.st_ino, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns) != actual:
            raise ValueError('source_changed')
    if total != identity[2] or _receipt_file_identity(path) != identity:
        raise ValueError('source_changed')
    return result.hexdigest()


def _receipt_instruction_snapshot(root, project, deadline):
    """Bound actual streamed instruction bytes, not just a pre-read stat size."""
    if (not isinstance(project, dict) or not isinstance(project.get('folder'), (str, Path))
            or not isinstance(project.get('brief', ''), str)
            or len(project.get('brief', '')) > 64000
            or Path(project['folder']).resolve(strict=True) != root):
        raise ValueError('basis_unavailable')
    brief = project.get('brief', '').encode('utf-8')
    if len(brief) > 64000:
        raise ValueError('basis_unavailable')
    basis = {'brief': hashlib.sha256(brief).hexdigest()}
    observed = {}
    for name, relative in (('rules', 'AGENTS.md'), ('followups', '.local-ai/USER-REQUESTS.md')):
        path = root / relative
        try:
            path.lstat()  # Broken links are present-but-unsafe, never absent.
        except FileNotFoundError:
            basis[name] = None
            observed[name] = None
            continue
        identity = _receipt_file_identity(path)
        if identity[2] > 64000:
            raise ValueError('basis_unavailable')
        digest = _receipt_stream(path, identity, deadline)
        basis[name] = digest
        observed[name] = (identity, digest)
    request = basis['followups'] or hashlib.sha256(b'').hexdigest()
    return basis, request, observed


def _receipt_temp_parent(root):
    """Prove an existing outside-project parent without tempfile probe writes."""
    import tempfile
    configured = tempfile.tempdir
    if configured is None:
        configured = next((os.environ[name] for name in ('TMPDIR', 'TEMP', 'TMP')
                           if os.environ.get(name)), None)
    if configured is None:
        configured = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Temp') if os.name == 'nt' else '/tmp'
    if not isinstance(configured, (str, bytes, os.PathLike)):
        raise ValueError('temp_unavailable')
    parent = Path(os.fsdecode(configured)).resolve(strict=True)
    if not parent.is_dir() or parent == root or parent.is_relative_to(root):
        raise ValueError('temp_unavailable')
    return parent


def readonly_check_receipts(folder, project, *, checker_file, include_plan=False):
    """Bounded evidence preview; UNKNOWN never means an obligation is fulfilled.

    Never opens the project SQLite database: mode=ro alone can create SHM for
    WAL databases. Copy stable bounded DB+WAL bytes into owned TEMP, query and
    validate there, then recheck the source bytes and original task basis.
    Final acceptance keeps its existing wrapper and obligation persistence.
    This is observational freshness, not same-user write linearizability.
    """
    import tempfile
    from contextlib import closing
    unknown = {'status': 'unknown', 'reason': 'unavailable', 'receipts': []}
    try:
        root = _artifact_project_root(folder)
        deadline = time.monotonic() + _RECEIPT_RO_COPY_SECONDS
        basis, request, instructions = _receipt_instruction_snapshot(root, project, deadline)
        database = root / '.local-ai/memory.sqlite3'
        if not database.is_file():
            return {**unknown, 'reason': 'database_missing'}
        paths = _receipt_source_paths(database)
        identities = {path: _receipt_file_identity(path) for path in paths}
        if (identities[database][2] > _RECEIPT_RO_DB_BYTES
                or any(identity[2] > _RECEIPT_RO_WAL_BYTES for path, identity in identities.items() if path != database)):
            return {**unknown, 'reason': 'snapshot_oversize'}
        runner_path = Path(checker_file)
        runner_identity = _receipt_file_identity(runner_path)
        if runner_identity[2] > 4 * 1024 * 1024:
            return {**unknown, 'reason': 'checker_unavailable'}
        runner = _receipt_stream(runner_path, runner_identity, deadline)
        current = verification_source_state(root, deadline=deadline)
        if (not isinstance(current, dict) or current.get('scope') not in {'full_project_source', 'application_source'}
                or type(current.get('omitted')) is not int
                or (current['scope'] == 'full_project_source' and current['omitted'] != 0)
                or (current['scope'] == 'application_source' and
                    (current['omitted'] <= 0 or current.get('limitations') != ['excluded_runtime']))):
            return {**unknown, 'reason': 'source_scope_limited'}
        temp_parent = _receipt_temp_parent(root)
        with tempfile.TemporaryDirectory(prefix='cale-receipt-preview-', dir=temp_parent) as temporary:
            snapshot = Path(temporary) / 'memory.sqlite3'
            hashes = {}
            for path, identity in identities.items():
                with (snapshot.parent / path.name).open('xb') as output:
                    hashes[path] = _receipt_stream(path, identity, deadline, output)
            if _receipt_source_paths(database) != paths:
                return {**unknown, 'reason': 'source_changed'}
            for path, identity in identities.items():
                if _receipt_stream(path, identity, deadline) != hashes[path]:
                    return {**unknown, 'reason': 'source_changed'}
            with closing(sqlite3.connect(snapshot, timeout=0)) as conn:
                conn.row_factory = sqlite3.Row
                sql_deadline = time.monotonic() + _RECEIPT_RO_SQL_SECONDS
                conn.set_progress_handler(lambda: time.monotonic() >= sql_deadline, 1000)
                conn.execute('BEGIN')
                if time.monotonic() >= sql_deadline:
                    return {**unknown, 'reason': 'sql_budget'}
                if conn.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
                    return {**unknown, 'reason': 'database_invalid'}
                # SQL byte/type checks precede payload materialization in Python.
                rows = conn.execute("SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB))=64 THEN id END AS id,"
                    "CASE WHEN typeof(payload)='text' AND length(CAST(payload AS BLOB))<=32000 THEN payload END AS payload,"
                    "length(CAST(payload AS BLOB)) AS bytes FROM verification_receipts ORDER BY created DESC LIMIT 64").fetchall()
                if any(row['id'] is None or row['payload'] is None for row in rows):
                    return {**unknown, 'reason': 'receipt_bounds'}
                if time.monotonic() >= sql_deadline:
                    return {**unknown, 'reason': 'sql_budget'}
                receipts = _validated_check_receipts(rows, root, basis, current, runner,
                    request_identity=lambda: request)
                plan = None
                if include_plan:
                    sizes = conn.execute("SELECT COUNT(*),COALESCE(SUM(status='done'),0),"
                        "COALESCE(SUM(status IN ('active','pending')),0),"
                        "SUM(CASE WHEN check_tool='check_webapp' THEN length(CAST(check_arguments AS BLOB)) ELSE 0 END),"
                        "MAX(CASE WHEN check_tool='check_webapp' THEN length(CAST(check_arguments AS BLOB)) ELSE 0 END),"
                        "SUM(CASE WHEN check_tool='check_webapp' AND typeof(check_arguments)!='text' THEN 1 ELSE 0 END),"
                        "SUM(CASE WHEN typeof(status)!='text' OR status NOT IN ('active','pending','done') THEN 1 ELSE 0 END) "
                        "FROM task_steps WHERE request_hash=? AND (status IS NULL OR status!='split')", (request,)).fetchone()
                    if (sizes[0] > 128 or (sizes[3] or 0) > 256000 or (sizes[4] or 0) > 64000 or sizes[5] or sizes[6]):
                        return {**unknown, 'reason': 'plan_bounds'}
                    import json
                    specs = [json.loads(row[0]) for row in conn.execute(
                        "SELECT check_arguments FROM task_steps WHERE request_hash=? AND (status IS NULL OR status!='split') "
                        "AND check_tool='check_webapp' ORDER BY position,id LIMIT 128", (request,))]
                    from webapp_check import normalize_spec, validate_spec_data
                    for specification in specs:
                        if (not isinstance(specification, dict) or not isinstance(specification.get('path'), str)
                                or not 1 <= len(specification['path']) <= 240):
                            return {**unknown, 'reason': 'plan_invalid'}
                        validate_spec_data(normalize_spec(specification))
                    plan = {'total': sizes[0], 'done': sizes[1], 'remaining': sizes[2],
                            'browser_specifications': specs}
                if time.monotonic() >= sql_deadline:
                    return {**unknown, 'reason': 'sql_budget'}
            if (_receipt_source_paths(database) != paths
                    or _receipt_instruction_snapshot(root, project, deadline) != (basis, request, instructions)
                    or verification_source_state(root, deadline=deadline) != current
                    or _receipt_stream(runner_path, runner_identity, deadline) != runner):
                return {**unknown, 'reason': 'basis_changed'}
            for path, identity in identities.items():
                if _receipt_stream(path, identity, deadline) != hashes[path]:
                    return {**unknown, 'reason': 'source_changed'}
        result = {'status': 'available', 'reason': None, 'receipts': receipts,
                  'planning_basis': basis, 'request_hash': request, 'source': current,
                  'checker_sha256': runner}
        if include_plan:
            result['plan'] = plan
            result['_plan_snapshot'] = {path.name: (identities[path], hashes[path]) for path in paths}
        return result
    except (OSError, ValueError, TypeError, RecursionError, sqlite3.Error):
        return unknown


