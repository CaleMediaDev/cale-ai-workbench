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
from typing import Callable

DB_NAME = "computer-files.sqlite3"
STATUS_NAME = "computer-files-status.json"
FIRST_SCAN_NAME = "computer-files-first-scan.done"
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


def first_scan_pending(state: str | Path, snapshot: dict | None = None) -> bool:
    """Hold first-run tasks until one complete scan; never gate daily refreshes.

    Older installations already have a completed status but no marker. Treat
    that status as proof so an update does not interrupt their task composer.
    An indexing error releases the composer with a visible warning instead of
    trapping someone in an unusable first-run screen.
    """
    snapshot = status(state) if snapshot is None else snapshot
    marker = Path(state) / FIRST_SCAN_NAME
    if marker.is_file():
        return False
    if snapshot.get("status") == "complete":
        # An older release has a complete catalog but no marker. Migrate it
        # now so its next daily refresh cannot re-lock the composer.
        marker.write_text("complete", encoding="ascii")
        return False
    return snapshot.get("status") in {"not_started", "working"}


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


def scan_computer(state: str | Path, roots: list[str | Path] | None = None,
                  defer_if_busy: Callable[[], bool] | None = None) -> dict:
    """Catalog names and paths; defer routine rescans when user work begins."""
    roots = [Path(root).absolute() for root in (local_roots() if roots is None else roots)]
    roots = [root for root in roots if root.is_dir() and not root.is_symlink()]
    scan_id = uuid.uuid4().hex
    files = folders = unreadable = processed = 0
    _status(state, status="working", files=0, folders=0, roots=[str(r) for r in roots],
            current="Starting the local file scan")
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
            completed_roots = []
            for root in roots:
                unreadable_before = unreadable
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
                                        if (defer_if_busy is not None
                                                and (Path(state) / FIRST_SCAN_NAME).is_file()
                                                and defer_if_busy()):
                                            result = {
                                                "status": "deferred", "files": files,
                                                "folders": folders, "unreadable": unreadable,
                                                "roots": [str(r) for r in roots],
                                                "current": "File scan paused while Cale works",
                                            }
                                            _status(state, **result)
                                            return result
                                        _status(
                                            state, status="working", files=files,
                                            folders=folders, unreadable=unreadable,
                                            roots=[str(r) for r in roots],
                                            current=str(folder),
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
                if unreadable == unreadable_before:
                    completed_roots.append(root)
            # Prune only roots traversed without errors. Unreadable entries or
            # directories may still contain paths from the prior catalog.
            for root in completed_roots:
                # Include the separator so sibling roots with a shared name
                # prefix are not mistaken for descendants of this root.
                prefix = os.path.join(str(root), "")
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
        (Path(state) / FIRST_SCAN_NAME).write_text("complete", encoding="ascii")
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
            found = []
            while len(found) < candidate_limit:
                rows = conn.execute(
                    """SELECT entries.path,entries.name,entries.kind
                    FROM entry_fts JOIN entries ON entries.id=entry_fts.rowid
                    WHERE entry_fts MATCH ? ORDER BY bm25(entry_fts,8.0,1.0)
                    LIMIT ? OFFSET ?""",
                    (expression, candidate_limit - len(found), len(found)),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    if Path(row["path"]).exists():
                        found.append(dict(row))
                    else:
                        conn.execute("DELETE FROM entries WHERE path=?", (row["path"],))
                # Deleted rows no longer occupy the ranked candidate window;
                # skip only the live matches already collected.
            return found

        def score(row: dict) -> tuple[int, int]:
            name = row["name"].casefold()
            path = row["path"].casefold()
            return (
                sum(word.casefold() in path for word in terms),
                sum(word.casefold() in name for word in terms),
            )

        minimum_coverage = 2 if len(terms) >= 3 else 1
        # Parent-folder terms occur in millions of rows on a whole-PC index.
        # Search names first so a broad request such as "documents" does not
        # rank every file under Documents before returning the folder itself.
        exact_name = matches(" AND ".join(f'name:"{word}"' for word in terms), count * 3)
        if exact_name:
            return exact_name[:count]
        # A name in one folder plus a term in its parent is still an exact
        # request; do this before accepting partial filename matches.
        exact = matches(" AND ".join(f'"{word}"' for word in terms), count * 3)
        if exact:
            return exact[:count]
        loose_name = matches(" OR ".join(f'name:"{word}"' for word in terms), count * 20)
        relevant_name = [row for row in loose_name if score(row)[0] >= minimum_coverage]
        if relevant_name:
            return sorted(relevant_name, key=score, reverse=True)[:count]
        # A path-only request still falls back to the full filename/parent index.
        loose = matches(" OR ".join(f'"{word}"' for word in terms), count * 20)
        relevant = [row for row in loose if score(row)[0] >= minimum_coverage]
        return sorted(relevant, key=score, reverse=True)[:count]
