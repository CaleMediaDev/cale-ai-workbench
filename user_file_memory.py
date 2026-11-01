"""Private, rebuildable full-text index for files a person gives Cale.

The source file stays authoritative. Only explicitly selected paths are read;
search refreshes indexed files so edits and deletions do not return stale text.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path

import file_chat
import project_memory

IMAGES = file_chat.IMAGES
AUDIO = frozenset({".wav", ".mp3", ".m4a", ".flac", ".ogg", ".wma"})
VIDEO = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm"})
SUPPORTED = file_chat.PLAIN_TEXT | file_chat.DOCUMENTS | IMAGES | AUDIO | VIDEO
MAX_FILES = 200
MAX_BYTES = 15 * 1024 * 1024
MAX_MEDIA_BYTES = 100 * 1024 * 1024
MAX_TEXT = 256_000


def _file_limit(path: Path) -> int:
    return MAX_MEDIA_BYTES if path.suffix.casefold() in AUDIO | VIDEO else MAX_BYTES


def _files(paths: list[str]):
    """Walk only user-selected roots, without following directory links."""
    count = 0
    for raw in paths[:20]:
        try:
            root = Path(raw).resolve(strict=True)
        except OSError:
            continue
        if not root.is_file() and not root.is_dir():
            continue
        if root.is_file():
            candidates = iter((root,))
        else:
            def walk():
                for base, dirs, names in os.walk(root, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if d not in project_memory.SKIP_DIRS
                                     and not (Path(base) / d).is_symlink())
                    for name in sorted(names):
                        yield Path(base) / name

            candidates = walk()
        for path in candidates:
            if count >= MAX_FILES:
                return
            if path.is_symlink() or project_memory.SECRET_NAME.search(path.name):
                continue
            if path.suffix.casefold() not in SUPPORTED:
                continue
            if not path.is_file() or path.stat().st_size > _file_limit(path):
                continue
            count += 1
            yield path.resolve()


def _media_call(action: str, path: Path, **options) -> dict:
    """Use bundled offline media models; never send selected content online."""
    python = file_chat.ROOT / "Media/python/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("The local media reader is still installing.")
    result = subprocess.run(
        [str(python), str(file_chat.ROOT / "media_worker.py")],
        input=json.dumps({"action": action, "path": str(path), **options}),
        text=True, capture_output=True, encoding="utf-8", errors="replace",
        cwd=file_chat.ROOT, timeout=900, creationflags=file_chat.HIDE,
    )
    try:
        payload = json.loads(result.stdout.splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("The local media reader did not respond.") from exc
    if result.returncode or "error" in payload:
        raise RuntimeError(str(payload.get("error", "Could not read the media.")))
    return payload


def _text(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in IMAGES:
        description = file_chat.image_description(
            path, "Describe this image accurately and transcribe readable text."
        ).strip()
        return ("[AI-generated local image description; verify against the original image.]\n" + description) if description else ""
    if suffix in AUDIO:
        result = _media_call("transcribe_audio", path)
        transcript = result["text"].strip()
        return ("[Offline audio transcript; verify against the original recording.]\n" + transcript) if transcript else ""
    if suffix in VIDEO:
        result = _media_call(
            "analyze_video", path, max_frames=4, include_audio=False,
            question="Describe the visible events and transcribe readable text.",
        )
        observations = "\n".join(
            f"{frame['timestamp_seconds']}s: {frame['description']}"
            for frame in result["sampled_frames"]
        )
        try:
            audio = _media_call("transcribe_audio", path).get("text", "")
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired):
            # Silent videos and unsupported audio tracks still retain visual memory.
            audio = ""
        if not observations and not audio:
            return ""
        return (
            "[AI-generated description of up to four sampled video frames; events between frames may be missed.]\n"
            + observations + "\n[Offline audio transcript]\n" + audio
        )
    if path.suffix.casefold() in file_chat.PLAIN_TEXT:
        data = path.read_bytes()
        if b"\x00" in data:
            return ""
        return data[:MAX_TEXT].decode("utf-8-sig", "replace")
    from markitdown import MarkItDown

    return MarkItDown(enable_plugins=False).convert(str(path)).text_content[:MAX_TEXT]


@contextmanager
def _connection(state: str | Path):
    root = Path(state)
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "user-files.sqlite3", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY, size INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
        content TEXT NOT NULL, indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(
        path UNINDEXED, filename, content, tokenize='unicode61'
    )""")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def ingest(state: str | Path, paths: list[str]) -> dict:
    """Incrementally index supported files and documents from selected roots."""
    indexed = unchanged = skipped = 0
    with _connection(state) as conn:
        for path in _files(paths):
            source = str(path)
            try:
                stat = path.stat()
                previous = conn.execute("SELECT size,modified_ns FROM files WHERE path=?", (source,)).fetchone()
                if previous and (previous["size"], previous["modified_ns"]) == (stat.st_size, stat.st_mtime_ns):
                    unchanged += 1
                    continue
                content = _text(path).strip()
                if not content:
                    if previous:
                        conn.execute("DELETE FROM files WHERE path=?", (source,))
                        conn.execute("DELETE FROM file_fts WHERE path=?", (source,))
                    skipped += 1
                    continue
                conn.execute("""INSERT INTO files(path,size,modified_ns,content) VALUES(?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET size=excluded.size,
                    modified_ns=excluded.modified_ns, content=excluded.content,
                    indexed_at=CURRENT_TIMESTAMP""", (source, stat.st_size, stat.st_mtime_ns, content))
                conn.execute("DELETE FROM file_fts WHERE path=?", (source,))
                conn.execute("INSERT INTO file_fts(path,filename,content) VALUES(?,?,?)",
                             (source, path.name, content))
                indexed += 1
            except (OSError, ValueError, RuntimeError, KeyError, ImportError, subprocess.TimeoutExpired):
                conn.execute("DELETE FROM files WHERE path=?", (source,))
                conn.execute("DELETE FROM file_fts WHERE path=?", (source,))
                skipped += 1
        conn.commit()
    return {"indexed": indexed, "unchanged": unchanged, "skipped": skipped}


def search(state: str | Path, query: str, limit: int = 8) -> list[dict]:
    """Search selected files locally, refreshing changes before returning snippets."""
    with _connection(state) as conn:
        paths = [row[0] for row in conn.execute("SELECT path FROM files")]
    # A disappeared file must not remain available as apparent current evidence.
    existing = [
        path for path in paths
        if Path(path).is_file() and Path(path).suffix.casefold() in SUPPORTED
        and Path(path).stat().st_size <= _file_limit(Path(path))
    ]
    for start in range(0, len(existing), MAX_FILES):
        ingest(state, existing[start:start + MAX_FILES])
    with _connection(state) as conn:
        for path in set(paths) - set(existing):
            conn.execute("DELETE FROM files WHERE path=?", (path,))
            conn.execute("DELETE FROM file_fts WHERE path=?", (path,))
        words = project_memory.WORD.findall(query)[:12]
        if not words:
            rows = conn.execute("SELECT path,substr(content,1,700) AS excerpt FROM files ORDER BY indexed_at DESC LIMIT ?",
                                (max(1, min(limit, 20)),)).fetchall()
        else:
            expression = " OR ".join('"' + word + '"' for word in words)
            rows = conn.execute("""SELECT path,snippet(file_fts,2,'[',']',' … ',30) AS excerpt
                FROM file_fts WHERE file_fts MATCH ? ORDER BY bm25(file_fts,0.0,4.0,1.0)
                LIMIT ?""", (expression, max(1, min(limit, 20)))).fetchall()
        conn.commit()
    return [dict(row) for row in rows]


def get(state: str | Path, path: str) -> dict:
    """Return an indexed document only while its source still exists."""
    search(state, "", 1)
    if not Path(path).is_file():
        raise ValueError("That file is no longer in memory. Select it again.")
    with _connection(state) as conn:
        row = conn.execute("SELECT path,content,indexed_at FROM files WHERE path=?", (path,)).fetchone()
    if row is None:
        raise ValueError("That file is no longer in memory. Select it again.")
    return dict(row)
