"""Fast, rebuildable SQLite catalog of Cale's local skills and tools.

The files and Python registrations remain authoritative. This index contains
only descriptions, schemas, and searchable text, so a damaged database can be
recreated without losing a person's skills or project data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

MAX_SKILLS = 1000
MAX_SKILL_BYTES = 128_000
WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def database_path(root: str | Path) -> Path:
    return Path(root).resolve() / "LongTasks" / "capabilities.sqlite3"


def _connect(root: str | Path) -> sqlite3.Connection:
    path = database_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("""CREATE TABLE IF NOT EXISTS capabilities (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
        description TEXT NOT NULL, path TEXT NOT NULL DEFAULT '',
        schema_json TEXT NOT NULL DEFAULT '{}', content_hash TEXT NOT NULL,
        updated REAL NOT NULL
    )""")
        conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS capabilities_fts USING fts5(
        id UNINDEXED, name, description, body, tokenize='unicode61'
    )""")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def _database(root: str | Path):
    with closing(_connect(root)) as conn, conn:
        yield conn


def _description(body: str, name: str) -> str:
    """Use front matter when present; otherwise use the first real sentence."""
    match = re.search(r"(?m)^description:[^\S\r\n]*(\S[^\r\n]*)$", body[:4000])
    if match:
        return match.group(1).strip(" '\"\r")[:500]
    lines = [line.strip("# ") for line in body.splitlines()
             if line.strip() and not line.startswith(("---", "name:", "description:", "#"))]
    return (lines[0] if lines else name)[:500]


def _skill_files(root: Path):
    """Prefer installed, possibly user-edited skills; use bundled skills before setup."""
    installed = root / "data/skills"
    bundled = root / "vendor/agent-skills"
    sources = (installed, bundled) if installed.is_dir() else (bundled,)
    seen = set()
    for source in sources:
        if not source.is_dir():
            continue
        for path in sorted(source.rglob("SKILL.md")):
            if len(seen) >= MAX_SKILLS:
                return
            try:
                resolved = path.resolve(strict=True)
                if (path.is_symlink() or not resolved.is_file()
                        or not resolved.is_relative_to(source.resolve())
                        or path.stat().st_size > MAX_SKILL_BYTES):
                    continue
            except OSError:
                continue
            name = path.parent.name
            if name in seen:
                continue
            seen.add(name)
            yield path


def specialist_snapshot_path(root: str | Path) -> Path:
    return Path(root).resolve() / "LongTasks" / "specialists.json"


def _registered_tools(root: Path) -> list[dict]:
    """Discover direct local tools without launching specialist backends."""
    import native_agent
    import toolbox_mcp

    rows = []
    for item in native_agent.TOOLS:
        function = item["function"]
        rows.append({"name": function["name"],
                     "description": function["description"],
                     "schema": function["parameters"]})
    for tool in asyncio.run(toolbox_mcp.server.list_tools()):
        if tool.name.startswith("toolbox_"):
            continue
        rows.append({"name": tool.name, "description": tool.description or "",
                     "schema": tool.input_schema})
    for name, spec in toolbox_mcp.LOCAL_CATALOG.items():
        rows.append({"name": name, "description": spec["description"],
                     "schema": spec["inputSchema"]})
    try:
        snapshot = json.loads(specialist_snapshot_path(root).read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict):
            return rows
        for spec in snapshot.get("tools", []):
            if (isinstance(spec, dict) and isinstance(spec.get("name"), str)
                    and spec["name"].startswith(("rea.", "devtools."))
                    and isinstance(spec.get("description"), str)
                    and isinstance(spec.get("schema"), dict)):
                rows.append(spec)
    except (OSError, ValueError, TypeError):
        pass
    return rows


def _upsert(conn: sqlite3.Connection, row: dict) -> bool:
    prior = conn.execute("SELECT content_hash,path FROM capabilities WHERE id=?", (row["id"],)).fetchone()
    if (prior and prior["content_hash"] == row["hash"] and prior["path"] == row["path"]
            and conn.execute("SELECT 1 FROM capabilities_fts WHERE id=? LIMIT 1",
                             (row["id"],)).fetchone()):
        return False
    conn.execute("""INSERT INTO capabilities
        (id,kind,name,description,path,schema_json,content_hash,updated)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        kind=excluded.kind,name=excluded.name,description=excluded.description,
        path=excluded.path,schema_json=excluded.schema_json,
        content_hash=excluded.content_hash,updated=excluded.updated""",
        (row["id"], row["kind"], row["name"], row["description"],
         row["path"], row["schema"], row["hash"], time.time()))
    conn.execute("DELETE FROM capabilities_fts WHERE id=?", (row["id"],))
    conn.execute("INSERT INTO capabilities_fts(id,name,description,body) VALUES (?,?,?,?)",
                 (row["id"], row["name"], row["description"], row["body"]))
    return True


def _sync_once(root: str | Path) -> dict:
    """Incrementally refresh skills and tools; remove stale index rows."""
    root = Path(root).resolve(strict=True)
    current = set()
    updated = 0
    skills = tools = 0
    with _database(root) as conn:
        for path in _skill_files(root):
            data = path.read_bytes()
            body = data.decode("utf-8-sig", "replace")
            name = path.parent.name
            identifier = "skill:" + name
            current.add(identifier)
            skills += 1
            updated += _upsert(conn, {"id": identifier, "kind": "skill", "name": name,
                "description": _description(body, name), "path": path.relative_to(root).as_posix(),
                "schema": "{}", "body": body, "hash": hashlib.sha256(data).hexdigest()})
        for tool in _registered_tools(root):
            identifier = "tool:" + tool["name"]
            current.add(identifier)
            tools += 1
            schema = json.dumps(tool["schema"], sort_keys=True, ensure_ascii=False)
            body = tool["description"] + "\n" + schema
            updated += _upsert(conn, {"id": identifier, "kind": "tool",
                "name": tool["name"], "description": tool["description"],
                "path": "", "schema": schema, "body": body,
                "hash": hashlib.sha256(body.encode()).hexdigest()})
        stale = [row[0] for row in conn.execute("SELECT id FROM capabilities")
                 if row[0] not in current]
        for identifier in stale:
            conn.execute("DELETE FROM capabilities WHERE id=?", (identifier,))
            conn.execute("DELETE FROM capabilities_fts WHERE id=?", (identifier,))
    return {"skills": skills, "tools": tools, "updated": updated,
            "removed": len(stale), "database": str(database_path(root))}


def sync(root: str | Path) -> dict:
    """Refresh the index, preserving a damaged copy before rebuilding it."""
    try:
        return _sync_once(root)
    except sqlite3.DatabaseError as exc:
        if (getattr(exc, "sqlite_errorcode", 0) & 0xff) not in (
                sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            raise
        path = database_path(root)
        if not path.is_file():
            raise
        descriptor, backup_name = tempfile.mkstemp(
            prefix=path.name + f".corrupt-{int(time.time())}-", dir=path.parent)
        os.close(descriptor)
        backup = Path(backup_name)
        os.replace(path, backup)
        for suffix in ("-wal", "-shm"):
            companion = Path(str(path) + suffix)
            if companion.exists():
                os.replace(companion, Path(str(backup) + suffix))
        return _sync_once(root)


def sync_specialists(root: str | Path) -> dict:
    """Refresh the specialist schema cache once installed runtimes are ready."""
    import native_specialists

    root = Path(root).resolve()
    catalog = native_specialists.runtime().catalog()
    if not catalog["tools"]:
        raise RuntimeError("No local specialist tools started: " + str(catalog["errors"]))
    target = specialist_snapshot_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)
    return sync(root)


def health(root: str | Path) -> dict:
    """Read-only proof that both catalogs and the FTS index answer a query."""
    path = database_path(root)
    if not path.is_file():
        return {"ok": False, "skills": 0, "tools": 0}
    try:
        uri = path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
            skills = conn.execute("SELECT count(*) FROM capabilities WHERE kind='skill'").fetchone()[0]
            tools = conn.execute("SELECT count(*) FROM capabilities WHERE kind='tool'").fetchone()[0]
            probe = conn.execute("SELECT id FROM capabilities_fts LIMIT 1").fetchone()
        return {"ok": bool(skills and tools and probe), "skills": skills, "tools": tools}
    except (OSError, sqlite3.Error):
        return {"ok": False, "skills": 0, "tools": 0}


def search(root: str | Path, query: str, limit: int = 8, kind: str = "") -> list[dict]:
    """Use indexed full-text search; return concise, actionable catalog rows."""
    if kind not in ("", "skill", "tool"):
        raise ValueError("kind must be skill or tool")
    terms = WORDS.findall(str(query).lower())[:12]
    limit = max(1, min(int(limit), 20))
    if not database_path(root).is_file():
        sync(root)
    with _database(root) as conn:
        if terms:
            expression = " OR ".join('"' + term.replace('"', '') + '"' for term in terms)
            rows = conn.execute("""SELECT c.id,c.kind,c.name,c.description,c.path,c.schema_json
                FROM capabilities_fts f JOIN capabilities c ON c.id=f.id
                WHERE capabilities_fts MATCH ? AND (?='' OR c.kind=?)
                ORDER BY bm25(capabilities_fts) LIMIT ?""",
                (expression, kind, kind, limit)).fetchall()
        else:
            rows = conn.execute("""SELECT id,kind,name,description,path,schema_json
                FROM capabilities WHERE (?='' OR kind=?) ORDER BY name LIMIT ?""",
                (kind, kind, limit)).fetchall()
    return [{"id": row["id"], "kind": row["kind"], "name": row["name"],
             "description": row["description"], "path": row["path"],
             "schema": json.loads(row["schema_json"]) if row["kind"] == "tool" else {}}
            for row in rows]


def read_skill(root: str | Path, identifier: str) -> dict:
    """Read the live skill file, so edits are visible even before the next sync."""
    root = Path(root).resolve(strict=True)
    with _database(root) as conn:
        row = conn.execute("SELECT name,path FROM capabilities WHERE id=? AND kind='skill'",
                           (identifier,)).fetchone()
    if not row:
        raise ValueError("Find an installed skill with search_capabilities first")
    path = (root / row["path"]).resolve(strict=True)
    allowed = (root / "data/skills", root / "vendor/agent-skills")
    if not any(path.is_relative_to(folder.resolve()) for folder in allowed):
        raise ValueError("Skill path leaves the installed catalog")
    if path.stat().st_size > MAX_SKILL_BYTES:
        raise ValueError("Skill is too large to load")
    return {"id": identifier, "name": row["name"], "path": str(path),
            "content": path.read_text(encoding="utf-8-sig", errors="replace")}


if __name__ == "__main__":
    import sys

    root = Path(__file__).resolve().parent
    print(json.dumps(sync_specialists(root) if "--with-specialists" in sys.argv else sync(root)))
