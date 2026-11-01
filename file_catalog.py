"""Background, filename-only catalog of accessible files on local drives.

The catalog never opens file contents. A scan keeps prior results available
until a full pass finishes, then removes paths that disappeared on scanned
drives. Interrupted scans restart cleanly on the next Workbench launch.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DB_NAME = "computer-files.sqlite3"
STATUS_NAME = "computer-files-status.json"
BATCH = 1000
MIN_FREE_BYTES = 5 * 1024**3


def enough_space(state: str | Path) -> bool:
    """Leave room for Windows and the person's own files during a large scan."""
    return shutil.disk_usage(state).free >= MIN_FREE_BYTES


def folder_priority(folder: Path, home: Path | None = None) -> int:
    """Visit personal files before large system trees during a full-drive scan."""
    home = home or Path.home()
    if folder == home or home.is_relative_to(folder):
        return 3
    if folder.is_relative_to(home):
        first = folder.relative_to(home).parts[0].casefold()
        if first in {"documents", "desktop", "downloads", "pictures", "videos", "music"}:
            return 5
        return 4
    return 0


def local_roots() -> list[Path]:
    """List fixed and removable Windows drives; never crawl network shares."""
    if os.name != "nt":
        return [Path(Path.home().anchor or "/")]
    kernel = ctypes.windll.kernel32
    buffer = ctypes.create_unicode_buffer(256)
    length = kernel.GetLogicalDriveStringsW(len(buffer), buffer)
    if length >= len(buffer):
        buffer = ctypes.create_unicode_buffer(length + 1)
        length = kernel.GetLogicalDriveStringsW(len(buffer), buffer)
    drives = [Path(value) for value in buffer[:length].split("\0") if value]
    return [drive for drive in drives if kernel.GetDriveTypeW(str(drive)) in (2, 3)]


@contextmanager
def database(state: str | Path):
    root = Path(state)
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        parent TEXT NOT NULL, kind TEXT NOT NULL, scan_id TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS entries_scan ON entries(scan_id)")
    conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS entry_fts USING fts5(
        name, parent, content='entries', content_rowid='id', tokenize='unicode61'
    )""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS entries_insert AFTER INSERT ON entries BEGIN
        INSERT INTO entry_fts(rowid,name,parent) VALUES(new.id,new.name,new.parent);
    END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS entries_delete AFTER DELETE ON entries BEGIN
        INSERT INTO entry_fts(entry_fts,rowid,name,parent)
        VALUES('delete',old.id,old.name,old.parent);
    END""")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _status(state: str | Path, **fields) -> None:
    path = Path(state) / STATUS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({**fields, "updated": time.time()}), encoding="utf-8")
    os.replace(temporary, path)


def status(state: str | Path) -> dict:
    try:
        return json.loads((Path(state) / STATUS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "not_started", "files": 0, "folders": 0}


def refresh_delay(snapshot: dict, refresh_seconds: int = 24 * 60 * 60,
                  now: float | None = None) -> float:
    """Reuse a recent completed catalog across app restarts.

    The status is written only after the whole scan and stale-path pruning
    finish. Interrupted scans therefore start again instead of being mistaken
    for a complete index.
    """
    if snapshot.get("status") != "complete":
        return 0.0
    try:
        completed_at = float(snapshot["updated"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    now = time.time() if now is None else now
    age = max(0.0, now - completed_at)
    return max(0.0, refresh_seconds - age)


def scan_computer(state: str | Path, roots: list[str | Path] | None = None) -> dict:
    """Catalog accessible names/paths without reading document contents."""
    roots = [Path(root) for root in (local_roots() if roots is None else roots)]
    roots = [root for root in roots if root.is_dir() and not root.is_symlink()]
    scan_id = uuid.uuid4().hex
    files = folders = unreadable = processed = 0
    _status(state, status="working", files=0, folders=0, roots=[str(r) for r in roots])
    try:
        with database(state) as conn:
            if not enough_space(state):
                result = {
                    "status": "waiting_for_space", "files": 0, "folders": 0,
                    "roots": [str(r) for r in roots],
                    "message": "Free at least 5 GB on the drive holding Cale's index to continue.",
                }
                _status(state, **result)
                return result
            for root in roots:
                pending = [root]
                while pending:
                    folder = pending.pop()
                    child_folders = []
                    try:
                        with os.scandir(folder) as entries:
                            for entry in entries:
                                try:
                                    if entry.is_symlink() or entry.is_junction():
                                        continue
                                    directory = entry.is_dir(follow_symlinks=False)
                                    regular = entry.is_file(follow_symlinks=False)
                                    if not (directory or regular):
                                        continue
                                    path = str(Path(entry.path))
                                    conn.execute(
                                        """INSERT INTO entries(path,name,parent,kind,scan_id)
                                        VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                                        kind=excluded.kind, scan_id=excluded.scan_id""",
                                        (path, entry.name, str(folder),
                                         "folder" if directory else "file", scan_id),
                                    )
                                    if directory:
                                        folders += 1
                                        child_folders.append(Path(entry.path))
                                    else:
                                        files += 1
                                    processed += 1
                                    if processed % BATCH == 0:
                                        conn.commit()
                                        _status(
                                            state, status="working", files=files,
                                            folders=folders, unreadable=unreadable,
                                            roots=[str(r) for r in roots],
                                        )
                                    if processed % (BATCH * 10) == 0 and not enough_space(state):
                                        result = {
                                            "status": "waiting_for_space", "files": files,
                                            "folders": folders, "unreadable": unreadable,
                                            "roots": [str(r) for r in roots],
                                            "message": "Free at least 5 GB on the drive holding Cale's index to continue.",
                                        }
                                        _status(state, **result)
                                        return result
                                except OSError:
                                    unreadable += 1
                    except OSError:
                        unreadable += 1
                    # A stack pops its last entry first. Sort each directory's
                    # children so the user's profile wins over Windows trees.
                    pending.extend(sorted(child_folders, key=folder_priority))
            # Prune only after a complete pass. An interrupted process leaves
            # the prior catalog intact for the next launch to recover.
            for root in roots:
                prefix = str(root)
                conn.execute(
                    "DELETE FROM entries WHERE substr(path,1,?)=? AND scan_id<>?",
                    (len(prefix), prefix, scan_id),
                )
            conn.commit()
        result = {
            "status": "complete", "files": files, "folders": folders,
            "unreadable": unreadable, "roots": [str(r) for r in roots],
        }
        _status(state, **result)
        return result
    except Exception as exc:
        _status(
            state, status="error", files=files, folders=folders,
            unreadable=unreadable, roots=[str(r) for r in roots],
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


def search(state: str | Path, query: str, limit: int = 10) -> list[dict]:
    """Find filename and path matches from the local index, newest scan first."""
    words = re.findall(r"[^\W_]+", query, re.UNICODE)[:10]
    if not words:
        return []
    filler = {"my", "the", "a", "an", "file", "folder", "document", "from",
              "in", "on", "for", "of", "please", "called", "named"}
    # Remove whole conversational words before splitting filenames. The regex
    # treats an underscore as part of a name, so "file_catalog.py" stays intact.
    cleaned = re.sub(
        r"(?i)\b(?:" + "|".join(sorted(filler)) + r")\b", " ", query,
    )
    terms = re.findall(r"[^\W_]+", cleaned, re.UNICODE)[:10] or words
    count = max(1, min(limit, 30))
    with database(state) as conn:
        def matches(expression: str, candidate_limit: int) -> list[dict]:
            rows = conn.execute(
                """SELECT entries.path,entries.name,entries.kind
                FROM entry_fts JOIN entries ON entries.id=entry_fts.rowid
                WHERE entry_fts MATCH ? ORDER BY bm25(entry_fts,8.0,1.0)
                LIMIT ?""",
                (expression, candidate_limit),
            ).fetchall()
            found = []
            for row in rows:
                if Path(row["path"]).exists():
                    found.append(dict(row))
                else:
                    conn.execute("DELETE FROM entries WHERE path=?", (row["path"],))
            return found

        exact = matches(" AND ".join(f'"{word}"' for word in terms), count * 3)
        if exact:
            return exact[:count]
        # Natural requests often contain a word absent from the real filename.
        # Use the most distinctive remaining words, then rank by term coverage.
        loose = matches(" OR ".join(f'"{word}"' for word in terms), count * 20)

        def score(row: dict) -> tuple[int, int]:
            name = row["name"].casefold()
            path = row["path"].casefold()
            return (
                sum(word.casefold() in path for word in terms),
                sum(word.casefold() in name for word in terms),
            )

        minimum_coverage = 2 if len(terms) >= 3 else 1
        relevant = [row for row in loose if score(row)[0] >= minimum_coverage]
        return sorted(relevant, key=score, reverse=True)[:count]
