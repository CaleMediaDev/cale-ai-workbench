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
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + DOCUMENT_CHUNK_CHARS, len(text))
        boundary = end
        # FTS indexes each chunk independently. Avoid turning an ordinary
        # word into two unsearchable fragments at a fixed character boundary.
        while (boundary > start and boundary < len(text)
               and text[boundary - 1].isalnum() and text[boundary].isalnum()):
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


def verification_source_state(folder):
    """Stream a bounded, complete source manifest or label it explicitly limited.

    Hash every included byte on each capture: size/mtime alone are not content
    identity. Before/open/after file identity and directory rechecks detect
    ordinary edits/replacements/listing races. This is not a filesystem snapshot
    or a sandbox against deliberately coordinated same-user tampering.
    """
    import json
    import stat
    root = _artifact_project_root(folder)
    started=time.monotonic()
    entries=[]; pending=[root]; directories=[]; total=consumed=visited=path_bytes=0
    omitted=0; reasons=set(); exhausted=False
    excluded={'.git','__pycache__'}
    runtime={'.venv','venv','node_modules','Toolbox'}
    def limited(reason):
        nonlocal omitted
        omitted+=1;reasons.add(reason)
    def expired():
        return time.monotonic()-started>=_VERIFICATION_MAX_SECONDS
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
                            if item.name in runtime:limited('excluded_runtime');continue
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
    if pending:omitted+=len(pending);reasons.add('unvisited_directories')
    encoded=json.dumps(sorted(entries),separators=(',',':')).encode()
    return {'sha256':hashlib.sha256(encoded).hexdigest(),'files':len(entries),'bytes':total,
            'omitted':omitted,'scope':'limited_dependency_manifest' if omitted else 'full_project_source',
            'limitations':sorted(reasons)}


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


def current_check_receipts(folder, project, *, checker_file):
    """Revalidate local cache integrity/freshness; this does not execute any check."""
    import json
    import task_context
    root=_artifact_project_root(folder);basis=verification_basis(root,project)
    current=verification_source_state(root)
    runner=hashlib.sha256(Path(checker_file).read_bytes()).hexdigest()
    output=[]
    with connection(root) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='verification_receipts'").fetchone():return []
        rows=conn.execute('SELECT id,substr(payload,1,32001) AS payload,length(CAST(payload AS BLOB)) AS bytes FROM verification_receipts ORDER BY created DESC LIMIT 64').fetchall()
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
                or value.get('planning_basis')!=basis or value.get('request_hash')!=task_context.request_identity(root)
                or _verification_digest(value.get('source_before'))!=_verification_digest(current)
                or _verification_digest(value.get('source_after'))!=_verification_digest(current)
                or value.get('checker_sha256')!=runner):continue
        output.append(value)
    return output


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
