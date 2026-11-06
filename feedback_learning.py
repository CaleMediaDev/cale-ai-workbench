"""Learn local task preferences from explicit thumbs-up and thumbs-down reviews.

This small, offline instance-based learner ranks past feedback by TF-IDF
similarity. It does not upload examples, rewrite the person's current request,
or pretend that a thumbs-up identifies which detail of a result was good.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import marshal
import math
import threading
import uuid
import re
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

WORD = re.compile(r"[a-z0-9]{3,}", re.I)
STOP = frozenset({"and", "for", "the", "this", "that", "with", "you", "your",
                  "make", "create", "build", "want", "need", "please", "could"})


def _db(root: str | Path) -> Path:
    state = Path(root)
    state.mkdir(parents=True, exist_ok=True)
    return state / "feedback-learning.sqlite3"


@contextmanager
def _connect(root: str | Path):
    conn = sqlite3.connect(_db(root), timeout=20)
    try:
        conn.row_factory = sqlite3.Row
        schema = """CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, generation INTEGER NOT NULL,
            kind TEXT NOT NULL, rating INTEGER NOT NULL, title TEXT NOT NULL,
            brief TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
            UNIQUE(task_id, generation, rating)
        )"""
        conn.execute(schema)
        conn.execute("CREATE INDEX IF NOT EXISTS feedback_kind ON feedback(kind, created DESC)")
        stored_schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='feedback'"
        ).fetchone()[0]
        if "AUTOINCREMENT" not in stored_schema.upper():
            # Preserve existing review IDs, but never recycle them after retraction.
            conn.execute("BEGIN IMMEDIATE")
            # A concurrent opener may already have migrated while this connection
            # waited for the writer lock. Rebuilding again would erase the
            # AUTOINCREMENT high-water mark for IDs deleted since that migration.
            locked_schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='feedback'"
            ).fetchone()[0]
            if "AUTOINCREMENT" not in locked_schema.upper():
                conn.execute("ALTER TABLE feedback RENAME TO feedback_legacy")
                conn.execute(schema)
                conn.execute("""INSERT INTO feedback
                    (id,task_id,generation,kind,rating,title,brief,note,created)
                    SELECT id,task_id,generation,kind,rating,title,brief,note,created
                    FROM feedback_legacy""")
                conn.execute("DROP TABLE feedback_legacy")
                conn.execute("CREATE INDEX feedback_kind ON feedback(kind, created DESC)")
        # Retraction must not make superseded generations current again.
        # Keep only generation metadata here, never review text or preferences.
        generation_schema = "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feedback_generations'"
        recall_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='feedback_recall_meta_v1'").fetchone()
        old_mode = conn.execute("SELECT floor_mode FROM feedback_recall_meta_v1 WHERE id=1").fetchone() if recall_meta else None
        legacy_mode = old_mode is not None and old_mode[0] == 0
        if legacy_mode and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if not conn.execute(generation_schema).fetchone():
            # Backfill once, under the same writer lock as table creation.
            # Rechecking after admission avoids rescanning history when two
            # openers simultaneously encounter an older database.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(generation_schema).fetchone():
                # Retained reviews cannot reconstruct floors after retraction.
                # Match recall/maintenance: a known lost authoritative table
                # must not be treated as a legacy database awaiting backfill.
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='feedback_recall_meta_v1'").fetchone():
                    old_mode = conn.execute(
                        "SELECT floor_mode FROM feedback_recall_meta_v1 WHERE id=1"
                    ).fetchone()
                    if old_mode is None or old_mode[0] != 0:
                        raise sqlite3.DatabaseError("Authoritative generation floors disappeared")
                conn.execute("""CREATE TABLE feedback_generations (
                    task_id TEXT PRIMARY KEY, generation INTEGER NOT NULL
                )""")
                conn.execute("""INSERT INTO feedback_generations (task_id,generation)
                    SELECT task_id,MAX(generation) FROM feedback GROUP BY task_id""")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='feedback_recall_meta_v1'").fetchone():
            # Floors observed/created by this writer are now authoritative.
            # Missing floor triggers still force the normal bounded rebuild.
            conn.execute("UPDATE feedback_recall_meta_v1 SET floor_mode=1,complete=0 WHERE id=1 AND floor_mode=0")
        yield conn
        conn.commit()
    finally:
        conn.close()


def kind_for(task: dict) -> str:
    """Group comparable work without inferring a preference from unrelated jobs."""
    card = str(task.get("card", ""))
    text = (str(task.get("title", "")) + " " + str(task.get("brief", ""))).lower()
    if card.startswith("image-job:"):
        return "image"
    if card.startswith("music-job:"):
        return "music"
    if re.search(r"\b(?:website|webpage|landing page)\b", text):
        return "website"
    if re.search(r"\b(?:app|software|program|dashboard|ui)\b", text):
        return "app_design"
    if re.search(r"\b(?:image|picture|photo|illustration)\b", text):
        return "image"
    if re.search(r"\b(?:song|music|rap|beat|melody)\b", text):
        return "music"
    if re.search(r"\b(?:video|animation|film)\b", text):
        return "video"
    if re.search(r"\bdesign\b", text):
        return "app_design"
    return "general"


def record(root: str | Path, task: dict, rating: int, note: str = "") -> int:
    """Save the current explicit review per task generation, idempotently."""
    if rating not in (-1, 1):
        raise ValueError("A review must be thumbs up or thumbs down.")
    note = " ".join(str(note).split())[:2000]
    if rating < 0 and not note:
        raise ValueError("Tell Cale what to improve before giving thumbs down.")
    with _connect(root) as conn:
        conn.execute("""INSERT INTO feedback_generations (task_id,generation)
            VALUES (?,?) ON CONFLICT(task_id) DO UPDATE SET
                generation=MAX(feedback_generations.generation,excluded.generation)""",
            (str(task["id"]), int(task.get("generation", 0))),
        )
        conn.execute(
            "DELETE FROM feedback WHERE task_id=? AND generation=? AND rating!=?",
            (str(task["id"]), int(task.get("generation", 0)), rating),
        )
        conn.execute("""INSERT INTO feedback
            (task_id,generation,kind,rating,title,brief,note,created)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(task_id,generation,rating) DO UPDATE SET
                kind=excluded.kind, title=excluded.title, brief=excluded.brief,
                note=excluded.note, created=excluded.created""",
            (str(task["id"]), int(task.get("generation", 0)), kind_for(task), rating,
             str(task.get("title", ""))[:200], str(task.get("brief", ""))[:4000],
             note, time.time()),
        )
        return int(conn.execute("""SELECT id FROM feedback WHERE
            task_id=? AND generation=? AND rating=?""",
            (str(task["id"]), int(task.get("generation", 0)), rating)).fetchone()[0])


def for_task(root: str | Path, task_id: str) -> list[dict]:
    try:
        with _recall_read(root) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id,generation,kind,rating,substr(note,1,2000) AS note,created FROM feedback "
                "WHERE task_id=? ORDER BY created DESC LIMIT 12", (task_id,)
            )]
    except sqlite3.OperationalError as error:
        if (getattr(error, "sqlite_errorcode", 0) or 0) & 255 == sqlite3.SQLITE_CANTOPEN:
            return []
        raise
    except OSError:
        return []


def forget(root: str | Path, feedback_id: int) -> bool:
    """Retract a review without reviving superseded same-generation reviews."""
    with _connect(root) as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        review = conn.execute(
            "SELECT task_id,generation,created,id FROM feedback WHERE id=?",
            (feedback_id,),
        ).fetchone()
        if review is None:
            return False
        # Legacy databases may retain opposite ratings for one generation.
        # Remove superseded reviews too, but never a newer review or generation.
        conn.execute(
            "DELETE FROM feedback WHERE task_id=? AND generation=? "
            "AND (created<? OR (created=? AND id<=?))",
            (review["task_id"], review["generation"], review["created"],
             review["created"], review["id"]),
        )
        return True


def _terms(text: str) -> Counter:
    return Counter(word for word in WORD.findall(text.lower()) if word not in STOP)


def _similarity(left: Counter, right: Counter, idf: dict) -> float:
    common = left.keys() & right.keys()
    numerator = sum(left[word] * right[word] * idf[word] ** 2 for word in common)
    left_norm = math.sqrt(sum(count ** 2 * idf[word] ** 2 for word, count in left.items()))
    right_norm = math.sqrt(sum(count ** 2 * idf[word] ** 2 for word, count in right.items()))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


# The derived index proposes IDs, never authoritative feedback or completion.
RECALL_QUERY_CHARS = 8192
RECALL_QUERY_DISTINCT = 256
RECALL_QUERY_TERMS = 32
RECALL_METADATA_ROWS = 2048
RECALL_POSTINGS_PER_TERM = 64
RECALL_CANDIDATES = 300
RECALL_TEXT_BYTES = 8 * 1024 * 1024
RECALL_ROW_BYTES = 27000
RECALL_DOC_TERMS = 1536
RECALL_READ_SECONDS = .1
RECALL_BATCH_SECONDS = .05
RECALL_BATCH_ROWS = 64
RECALL_BATCH_TASKS = 16
RECALL_BATCH_BYTES = 256 * 1024
RECALL_BATCH_MUTATIONS = 4096

_RECALL_TABLES = ("feedback_recall_meta_v1", "feedback_recall_docs_v1",
                  "feedback_recall_terms_v1", "feedback_recall_df_v1", "feedback_recall_changes_v1")
_RECALL_INDEXES = ("feedback_recall_lookup_v1", "feedback_recall_changes_order_v1")


def _recall_version():
    def normalized(code):
        constants=tuple(normalized(value) if isinstance(value,type(code)) else value for value in code.co_consts)
        return code.replace(co_consts=constants,co_filename="<terms>",co_firstlineno=1,co_linetable=b"")
    code=normalized(_terms.__code__)
    return "v1:" + hashlib.sha256(marshal.dumps(code) +
        repr((WORD.pattern, WORD.flags, sorted(STOP))).encode()).hexdigest()


def _deadline(conn, seconds):
    until = time.monotonic() + seconds
    conn.set_progress_handler(lambda: int(time.monotonic() >= until), 1000)
    return until


@contextmanager
def _recall_read(root):
    path = (Path(root) / "feedback-learning.sqlite3").resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
    try:
        conn.row_factory = sqlite3.Row
        _deadline(conn, RECALL_READ_SECONDS)
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _triggers(floors):
    result = {}
    def enqueue(event, key):
        return ("UPDATE feedback_recall_meta_v1 SET change_seq=change_seq+1,complete=0 WHERE id=1; "
                "INSERT INTO feedback_recall_changes_v1(event,key,revision) VALUES('" + event + "'," + key +
                ",(SELECT change_seq FROM feedback_recall_meta_v1 WHERE id=1)) "
                "ON CONFLICT(event,key) DO UPDATE SET revision=excluded.revision;")
    for operation in ("INSERT", "UPDATE", "DELETE"):
        refs = ("old", "new") if operation == "UPDATE" else ("old",) if operation == "DELETE" else ("new",)
        name = "feedback_recall_doc_" + operation.lower() + "_v1"
        body = " ".join(enqueue("doc", "CAST(" + ref + ".id AS TEXT)") for ref in refs)
        result[name] = "CREATE TRIGGER " + name + " AFTER " + operation + " ON feedback BEGIN " + body + " END"
        if floors:
            name = "feedback_recall_floor_" + operation.lower() + "_v1"
            body = "UPDATE feedback_recall_meta_v1 SET eligibility_revision=eligibility_revision+1 WHERE id=1; "
            body += " ".join(enqueue("floor", ref + ".task_id") for ref in refs)
            result[name] = "CREATE TRIGGER " + name + " AFTER " + operation + " ON feedback_generations BEGIN " + body + " END"
    return result


def _floor_table(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='feedback_generations'").fetchone() is not None


def _trigger_match(conn, floors):
    for name, sql in _triggers(floors).items():
        row = conn.execute("SELECT substr(sql,1,4097),length(sql) FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if not row or row[1] > 4096 or " ".join(row[0].split()) != " ".join(sql.split()):
            return False
    return True


def _read_meta(conn):
    numeric=("epoch","frontier_id","target_id","change_seq","complete","indexed_docs",
             "indexed_terms","omitted","eligibility_revision","batch_no","floor_mode","started")
    sql="SELECT CASE WHEN typeof(version)='text' THEN substr(version,1,128) END AS version, length(CAST(version AS BLOB)) AS version_bytes,"
    sql+=",".join("CASE WHEN typeof("+name+")='integer' THEN "+name+" END AS "+name for name in numeric)
    row=conn.execute(sql+" FROM feedback_recall_meta_v1 WHERE id=1").fetchone()
    if row is None:return None
    value=dict(row)
    if (not isinstance(value["version"],str) or value["version_bytes"]>128
            or any(value[name] is None or (name not in {"frontier_id","target_id"} and value[name]<0) for name in numeric)
            or value["epoch"]<1 or value["complete"] not in (0,1) or value["floor_mode"] not in (0,1) or value["started"] not in (0,1)):
        raise sqlite3.DatabaseError("Invalid derived recall metadata")
    return value


def _index_meta(conn):
    row = _read_meta(conn)
    if not row or row["version"] != _recall_version() or row["floor_mode"] != int(_floor_table(conn)) or not _trigger_match(conn, _floor_table(conn)):
        return None
    return dict(row)


def _index_init(conn):
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name IN (" + ",".join("?" for _ in (*_RECALL_TABLES, *_RECALL_INDEXES)) + ")", (*_RECALL_TABLES, *_RECALL_INDEXES))}
    if existing and existing != set((*_RECALL_TABLES, *_RECALL_INDEXES)):
        raise sqlite3.DatabaseError("Derived recall schema is incomplete")
    if not existing:
        for sql in (
            "CREATE TABLE feedback_recall_meta_v1(id INTEGER PRIMARY KEY CHECK(id=1),version TEXT NOT NULL,epoch INTEGER NOT NULL,frontier_id INTEGER NOT NULL,target_id INTEGER NOT NULL,change_seq INTEGER NOT NULL,complete INTEGER NOT NULL,indexed_docs INTEGER NOT NULL,indexed_terms INTEGER NOT NULL,omitted INTEGER NOT NULL,eligibility_revision INTEGER NOT NULL,batch_no INTEGER NOT NULL,floor_mode INTEGER NOT NULL,started INTEGER NOT NULL)",
            "CREATE TABLE feedback_recall_docs_v1(epoch INTEGER NOT NULL,feedback_id INTEGER NOT NULL,source_hash TEXT NOT NULL,omitted INTEGER NOT NULL,PRIMARY KEY(epoch,feedback_id)) WITHOUT ROWID",
            "CREATE TABLE feedback_recall_terms_v1(epoch INTEGER NOT NULL,feedback_id INTEGER NOT NULL,term TEXT NOT NULL,kind TEXT NOT NULL,created REAL NOT NULL,tf INTEGER NOT NULL CHECK(tf>0),PRIMARY KEY(epoch,feedback_id,term)) WITHOUT ROWID",
            "CREATE INDEX feedback_recall_lookup_v1 ON feedback_recall_terms_v1(epoch,kind,term,created DESC,feedback_id DESC,tf)",
            "CREATE TABLE feedback_recall_df_v1(epoch INTEGER NOT NULL,kind TEXT NOT NULL,term TEXT NOT NULL,docs INTEGER NOT NULL CHECK(docs>0),PRIMARY KEY(epoch,kind,term)) WITHOUT ROWID",
            "CREATE TABLE feedback_recall_changes_v1(event TEXT NOT NULL,key TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(event,key)) WITHOUT ROWID",
            "CREATE INDEX feedback_recall_changes_order_v1 ON feedback_recall_changes_v1(revision,event,key)",
        ): conn.execute(sql)
    floors = _floor_table(conn)
    meta = _read_meta(conn)
    if meta and meta["floor_mode"] and not floors:
        raise sqlite3.DatabaseError("Authoritative generation floors disappeared")
    rebuild = meta is None or meta["version"] != _recall_version() or meta["floor_mode"] != int(floors) or not _trigger_match(conn, floors)
    if rebuild:
        for operation in ("insert", "update", "delete"):
            for group in ("doc", "floor"):
                conn.execute("DROP TRIGGER IF EXISTS feedback_recall_" + group + "_" + operation + "_v1")
        for sql in _triggers(floors).values(): conn.execute(sql)
        first = conn.execute("SELECT id FROM feedback ORDER BY id ASC LIMIT 1").fetchone()
        last = conn.execute("SELECT id FROM feedback ORDER BY id DESC LIMIT 1").fetchone()
        frontier = first[0] if first else 0
        target = last[0] if last else 0
        epoch = meta["epoch"] + 1 if meta else 1
        seq = meta["change_seq"] if meta else 0
        # Inclusive first seek supports zero, negative and MIN_INT rowids without
        # subtracting one or claiming the initial row was already processed.
        conn.execute("INSERT OR REPLACE INTO feedback_recall_meta_v1 VALUES(1,?,?,?,?,?,0,0,0,0,0,0,?,0)", (_recall_version(),epoch,frontier,target,seq,int(floors)))
    return _read_meta(conn)


def _doc(conn, ident, extra="", parameters=()):
    row = conn.execute("""SELECT id,
        CASE WHEN typeof(generation)='integer' THEN generation END AS generation,
        CASE WHEN typeof(rating)='integer' THEN rating END AS rating,
        CASE WHEN typeof(created) IN ('integer','real') THEN created END AS created,
        CASE WHEN typeof(task_id)='text' THEN substr(task_id,1,257) END AS task_id,
        CASE WHEN typeof(kind)='text' THEN substr(kind,1,65) END AS kind,
        CASE WHEN typeof(title)='text' THEN substr(title,1,201) END AS title,
        CASE WHEN typeof(brief)='text' THEN substr(brief,1,4001) END AS brief,
        CASE WHEN typeof(note)='text' THEN substr(note,1,2001) END AS note,
        length(CAST(task_id AS BLOB))+length(CAST(kind AS BLOB))+length(CAST(title AS BLOB))+
        length(CAST(brief AS BLOB))+length(CAST(note AS BLOB)) AS bytes
        FROM feedback f WHERE id=?""" + (" AND " + extra if extra else ""), (ident,*parameters)).fetchone()
    if row is None: return None, 0
    value = dict(row)
    valid = (value["generation"] is not None and value["rating"] in (-1,1)
             and isinstance(value["created"], (int,float)) and math.isfinite(value["created"])
             and all(isinstance(value[k], str) for k in ("task_id","kind","title","brief","note"))
             and len(value["task_id"]) <= 256 and len(value["kind"]) <= 64
             and len(value["title"]) <= 200 and len(value["brief"]) <= 4000 and len(value["note"]) <= 2000)
    # Transferred substrings remain bounded even for an oversized corrupt row.
    transferred = sum(len(str(value[k] or "").encode("utf-8", "replace")) for k in ("task_id","kind","title","brief","note"))
    return value if valid else {"id": ident, "invalid": True}, transferred


def _doc_hash(doc):
    return hashlib.sha256(json.dumps(doc,sort_keys=True,ensure_ascii=True,allow_nan=False).encode()).hexdigest()


def _eligible(floors, alias="f"):
    floor = (alias + ".generation=(SELECT generation FROM feedback_generations WHERE task_id=" + alias + ".task_id) AND ") if floors else ""
    return floor + "NOT EXISTS(SELECT 1 FROM feedback n WHERE n.task_id=" + alias + ".task_id AND (n.generation>" + alias + ".generation OR (n.generation=" + alias + ".generation AND (n.created>" + alias + ".created OR (n.created=" + alias + ".created AND n.id>" + alias + ".id)))))"


def _replace_index_doc(conn, epoch, ident, doc, mutation_budget):
    old = conn.execute("SELECT source_hash,omitted FROM feedback_recall_docs_v1 WHERE epoch=? AND feedback_id=?", (epoch,ident)).fetchone()
    hashed = _doc_hash(doc) if doc else ""
    if old and old[0] == hashed: return 0
    bounds=conn.execute("SELECT count(*),coalesce(sum(length(CAST(kind AS BLOB))+length(CAST(term AS BLOB))),0),coalesce(max(length(kind)),0),coalesce(max(length(term)),0) FROM (SELECT kind,term FROM feedback_recall_terms_v1 WHERE epoch=? AND feedback_id=? LIMIT ?)",(epoch,ident,RECALL_DOC_TERMS+1)).fetchone()
    if bounds[0]>RECALL_DOC_TERMS or bounds[1]>RECALL_BATCH_BYTES or bounds[2]>64 or bounds[3]>4201:
        raise sqlite3.DatabaseError("Derived document exceeds metadata bound")
    previous = conn.execute("SELECT kind,term FROM feedback_recall_terms_v1 WHERE epoch=? AND feedback_id=? LIMIT ?", (epoch,ident,RECALL_DOC_TERMS+1)).fetchall()
    if any(not isinstance(kind,str) or not isinstance(term,str) for kind,term in previous):
        raise sqlite3.DatabaseError("Invalid derived document terms")
    omitted = int(bool(doc and doc.get("invalid")))
    terms = _terms(doc["title"] + " " + doc["brief"]) if doc and not omitted else {}
    if len(terms)>RECALL_DOC_TERMS: raise sqlite3.DatabaseError("Document exceeds term bound")
    mutations = len(previous)+len(terms)
    if mutations > mutation_budget: return None
    for kind,term in previous:
        conn.execute("DELETE FROM feedback_recall_df_v1 WHERE epoch=? AND kind=? AND term=? AND docs=1", (epoch,kind,term))
        conn.execute("UPDATE feedback_recall_df_v1 SET docs=docs-1 WHERE epoch=? AND kind=? AND term=?", (epoch,kind,term))
    conn.execute("DELETE FROM feedback_recall_terms_v1 WHERE epoch=? AND feedback_id=?", (epoch,ident))
    conn.execute("DELETE FROM feedback_recall_docs_v1 WHERE epoch=? AND feedback_id=?", (epoch,ident))
    if doc:
        conn.execute("INSERT INTO feedback_recall_docs_v1 VALUES(?,?,?,?)", (epoch,ident,hashed,omitted))
        for term,tf in terms.items():
            conn.execute("INSERT INTO feedback_recall_terms_v1 VALUES(?,?,?,?,?,?)", (epoch,ident,term,doc["kind"],doc["created"],tf))
            conn.execute("INSERT INTO feedback_recall_df_v1 VALUES(?,?,?,1) ON CONFLICT(epoch,kind,term) DO UPDATE SET docs=docs+1", (epoch,doc["kind"],term))
    conn.execute("UPDATE feedback_recall_meta_v1 SET indexed_docs=indexed_docs+?,indexed_terms=indexed_terms+?,omitted=omitted+? WHERE id=1", (int(bool(doc))-int(bool(old)),len(terms)-len(previous),omitted-(old[1] if old else 0)))
    return mutations


def maintain(root, *, before_commit=None):
    """One bounded, resumable background batch. Never called by a read API."""
    conn = None
    try:
        path = (Path(root)/"feedback-learning.sqlite3").resolve()
        conn = sqlite3.connect(path.as_uri()+"?mode=rw",uri=True,timeout=0)
        conn.row_factory = sqlite3.Row
        until = _deadline(conn, RECALL_BATCH_SECONDS)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("SELECT id FROM feedback LIMIT 1")
        meta = _index_init(conn)
        rows = text_bytes = mutations = 0
        tasks = set()
        def apply(ident):
            nonlocal rows,text_bytes,mutations
            if (rows>=RECALL_BATCH_ROWS or text_bytes+RECALL_ROW_BYTES>RECALL_BATCH_BYTES
                    or len(tasks)>=RECALL_BATCH_TASKS or time.monotonic()>=until): return False
            doc,size = _doc(conn,ident)
            count = _replace_index_doc(conn,meta["epoch"],ident,doc,RECALL_BATCH_MUTATIONS-mutations)
            if count is None:return False
            rows+=1;text_bytes+=size;mutations+=count
            if doc and "task_id" in doc:tasks.add(doc["task_id"])
            return True
        comparison=">" if meta["started"] else ">="
        pending = conn.execute("SELECT id FROM feedback WHERE id"+comparison+"? AND id<=? ORDER BY id LIMIT ?", (meta["frontier_id"],meta["target_id"],RECALL_BATCH_ROWS)).fetchall()
        events = conn.execute("SELECT substr(event,1,9),CASE WHEN event='doc' THEN substr(key,1,21) ELSE '' END,CASE WHEN typeof(revision)='integer' THEN revision END FROM feedback_recall_changes_v1 ORDER BY revision,event,key LIMIT 16").fetchall()
        def backfill():
            for entry in pending[:8]:
                if not apply(entry[0]):break
                meta["frontier_id"]=entry[0];meta["started"]=1
            if not pending:meta["frontier_id"]=meta["target_id"];meta["started"]=1
        def dirty():
            for event,key,revision in events:
                if event not in {"doc","floor"} or revision is None:
                    raise sqlite3.DatabaseError("Invalid derived change metadata")
                if event=="doc":
                    try:ident=int(key)
                    except (ValueError,TypeError):raise sqlite3.DatabaseError("Invalid derived change key")
                    if not apply(ident):break
                conn.execute("DELETE FROM feedback_recall_changes_v1 WHERE event=? AND revision=?", (event,revision))
        # Alternating committed batches prevents either hot changes or old
        # history from monopolizing a shared time/posting-mutation budget.
        if meta["batch_no"] % 2:dirty();backfill()
        else:backfill();dirty()
        scanned = meta["started"] and meta["frontier_id"]>=meta["target_id"] and conn.execute("SELECT 1 FROM feedback_recall_changes_v1 LIMIT 1").fetchone() is None
        omitted=conn.execute("SELECT omitted FROM feedback_recall_meta_v1 WHERE id=1").fetchone()[0]
        complete = scanned and not omitted
        conn.execute("UPDATE feedback_recall_meta_v1 SET frontier_id=?,started=?,complete=?,batch_no=batch_no+1 WHERE id=1", (meta["frontier_id"],meta["started"],int(complete)))
        cleanup=False
        if time.monotonic()<until:
            allowance=min(128,max(0,(RECALL_BATCH_MUTATIONS-mutations)//3))
            for table,columns in (("feedback_recall_terms_v1","epoch,feedback_id,term"),
                                  ("feedback_recall_df_v1","epoch,kind,term"),
                                  ("feedback_recall_docs_v1","epoch,feedback_id")):
                cursor=conn.execute("DELETE FROM "+table+" WHERE ("+columns+") IN (SELECT "+columns+" FROM "+table+" WHERE epoch<? LIMIT ?)",(meta["epoch"],allowance))
                mutations+=cursor.rowcount
                cleanup=cleanup or conn.execute("SELECT 1 FROM "+table+" WHERE epoch<? LIMIT 1",(meta["epoch"],)).fetchone() is not None
        if before_commit is not None:before_commit()
        conn.commit()
        return {"status":"cleanup" if cleanup else "ready" if complete else "limited" if scanned and omitted else "partial","rows":rows,"text_bytes":text_bytes,"posting_mutations":mutations}
    except (OSError,sqlite3.Error,ValueError,TypeError,OverflowError):
        if conn is not None:conn.rollback()
        return {"status":"unavailable"}
    finally:
        if conn is not None:conn.close()


def _recall_rows(root, kind, request, exclude):
    status={"coverage":"unavailable","limited":len(request)>RECALL_QUERY_CHARS,"metadata_rows":0,"text_bytes":0,"candidate_rows":0,"text_rows":0,"fallback_used":False}
    rows=[];seen=set()
    query=_terms(request[:RECALL_QUERY_CHARS])
    if len(query)>RECALL_QUERY_DISTINCT:status["limited"]=True
    wanted=list(query)[:RECALL_QUERY_DISTINCT]
    try:
        with _recall_read(root) as conn:
            floors=_floor_table(conn)
            if not floors and conn.execute("SELECT 1 FROM sqlite_master WHERE name='feedback_recall_meta_v1'").fetchone():
                old_mode=conn.execute("SELECT floor_mode FROM feedback_recall_meta_v1 WHERE id=1").fetchone()
                if old_mode is None or old_mode[0]!=0:
                    return [],status
            try:meta=_index_meta(conn)
            except sqlite3.Error:meta=None
            status["coverage"]=("indexed_snapshot_ready" if meta and meta["complete"] and not meta["omitted"] else "partial") if floors else "legacy_partial"
            index_trusted=True
            def add(ident, expected_hash=None):
                nonlocal index_trusted
                if ident in seen:return False
                if (len(rows)>=RECALL_CANDIDATES or status["text_rows"]>=RECALL_CANDIDATES
                        or status["text_bytes"]+RECALL_ROW_BYTES>RECALL_TEXT_BYTES):
                    status["limited"]=True
                    return False
                doc,size=_doc(conn,ident,"kind=? AND task_id!=? AND "+_eligible(floors),(kind,exclude))
                status["text_bytes"]+=size
                if doc is not None:status["text_rows"]+=1
                if doc is None or doc.get("invalid"):
                    if expected_hash is not None:index_trusted=False
                    return False
                if expected_hash is not None and _doc_hash(doc)!=expected_hash:
                    index_trusted=False
                    return False
                seen.add(ident);rows.append(doc);return True
            if meta and wanted:
                stats=[]
                for start in range(0,len(wanted),128):
                    terms=wanted[start:start+128]
                    found=conn.execute("SELECT term,CASE WHEN typeof(docs)='integer' AND docs>0 THEN docs END FROM feedback_recall_df_v1 WHERE epoch=? AND kind=? AND term IN ("+",".join("?" for _ in terms)+")", (meta["epoch"],kind,*terms)).fetchall()
                    status["metadata_rows"]+=len(found)
                    if any(r[1] is None for r in found):raise sqlite3.DatabaseError("Invalid derived frequency")
                    stats.extend(found)
                stats=sorted(stats,key=lambda r:(r[1],r[0]))
                if len(stats)>RECALL_QUERY_TERMS:status["limited"]=True
                hints={}
                # Reserve metadata room for a recent fallback, but reserve TEXT
                # slots for indexed matches first. Recency never displaces them.
                for term,df in stats[:RECALL_QUERY_TERMS]:
                    capacity=min(RECALL_POSTINGS_PER_TERM,RECALL_METADATA_ROWS-RECALL_CANDIDATES-status["metadata_rows"])
                    if capacity<=0:status["limited"]=True;break
                    if df>capacity:status["limited"]=True
                    matches=conn.execute("SELECT p.feedback_id,CASE WHEN typeof(d.source_hash)='text' AND length(CAST(d.source_hash AS BLOB))=64 THEN d.source_hash END,CASE WHEN typeof(p.created) IN ('integer','real') THEN p.created END FROM feedback_recall_terms_v1 p INDEXED BY feedback_recall_lookup_v1 JOIN feedback_recall_docs_v1 d ON d.epoch=p.epoch AND d.feedback_id=p.feedback_id JOIN feedback f ON f.id=p.feedback_id WHERE p.epoch=? AND p.kind=? AND p.term=? AND f.kind=? AND f.task_id!=? AND "+_eligible(floors)+" ORDER BY p.created DESC,p.feedback_id DESC LIMIT ?", (meta["epoch"],kind,term,kind,exclude,capacity)).fetchall()
                    status["metadata_rows"]+=len(matches)
                    for ident,hashed,created in matches:
                        if not isinstance(hashed,str) or created is None or not math.isfinite(created):
                            raise sqlite3.DatabaseError("Invalid derived posting metadata")
                        old=hints.get(ident,(0,hashed,created))
                        hints[ident]=(old[0]+math.log1p(max(1,meta["indexed_docs"])/(1+df))*query[term],hashed,created)
                for ident,(score,hashed,created) in sorted(hints.items(),key=lambda item:(-item[1][0],-item[1][2],item[0])):
                    if len(rows)>=RECALL_CANDIDATES:
                        status["limited"]=True;break
                    add(ident,hashed)
            complete_index=bool(meta and meta["complete"] and not meta["omitted"] and floors)
            fallback_required=not complete_index or status["limited"] or not index_trusted
            if not index_trusted:status["coverage"]="partial"
            if len(rows)<RECALL_CANDIDATES and fallback_required:
                capacity=min(RECALL_CANDIDATES,RECALL_METADATA_ROWS-status["metadata_rows"])
                if capacity>0:
                    status["fallback_used"]=True
                    recent=conn.execute("SELECT f.id FROM feedback f WHERE kind=? AND task_id!=? AND "+_eligible(floors)+" ORDER BY created DESC,id ASC LIMIT ?", (kind,exclude,capacity)).fetchall()
                    status["metadata_rows"]+=len(recent)
                    if len(recent)==capacity:status["limited"]=True
                    for entry in recent:
                        if len(rows)>=RECALL_CANDIDATES:
                            status["limited"]=True;break
                        add(entry[0])
            status["candidate_rows"]=len(rows)
    except (OSError,sqlite3.Error,ValueError,TypeError,OverflowError):
        # Do not restart the VM budget or initialize a DB after a failed read.
        rows=[];status["coverage"]="unavailable";status["candidate_rows"]=0
    return rows,status


def guidance_details(root: str | Path, kind: str, request: str,
                     exclude_task_id: str = "", limit: int = 3) -> dict:
    if not request.strip() or limit<=0:return {"text":"","status":{"coverage":"empty_request"}}
    rows,status=_recall_rows(root,kind,request,exclude_task_id)
    return {"text":_format_guidance(rows,request,limit),"status":status}


def guidance(root: str | Path, kind: str, request: str,
             exclude_task_id: str = "", limit: int = 3) -> str:
    """Advisory bounded recall; use guidance_details for partial/limited coverage."""
    return guidance_details(root,kind,request,exclude_task_id,limit)["text"]


def _format_guidance(rows, request, limit):
    if not rows:
        return ""
    query = _terms(request[:RECALL_QUERY_CHARS])
    examples = [_terms(row["brief"] + " " + row["title"]) for row in rows]
    documents = len(examples) + 1
    frequency = Counter(word for example in [query, *examples] for word in example)
    idf = {word: math.log(1 + documents / (1 + count)) for word, count in frequency.items()}
    ranked = sorted(
        ((_similarity(query, example, idf), row) for row, example in zip(rows, examples)),
        key=lambda pair: (pair[0], pair[1]["created"]), reverse=True,
    )
    lessons = []
    for score, row in ranked:
        if score < 0.12:
            continue  # Unrelated feedback must not pollute a new task.
        lesson = ""
        if row["rating"] < 0 and row["note"]:
            lesson = "Improve: " + row["note"][:240]
        elif row["rating"] > 0 and score >= 0.30:
            lesson = "Similar task the user liked: " + row["title"][:140]
        if lesson and lesson not in lessons:
            lessons.append(lesson)
        if len(lessons) >= min(3, limit):
            break
    if not lessons:
        return ""
    return ("Past user feedback for similar work (use only when relevant; "
            "the current request takes priority):\n- " + "\n- ".join(lessons))


_maintenance_lock=threading.Lock()
_maintenance_slot=None
_maintenance_atexit=False


def start_maintenance(root):
    """Explicit application lifecycle only. No DB work on the calling thread."""
    global _maintenance_slot,_maintenance_atexit
    root=Path(root).absolute()
    with _maintenance_lock:
        if _maintenance_slot is not None:return None
        token=uuid.uuid4().hex;stop=threading.Event();permit=threading.Event();abandoned=threading.Event()
        def run():
            global _maintenance_slot
            permit.wait()
            if abandoned.is_set():return
            try:
                while not stop.is_set():
                    result=maintain(root)
                    if stop.wait(.25 if result["status"] in {"partial","cleanup"} else 5):break
            finally:
                with _maintenance_lock:
                    if _maintenance_slot is not None and _maintenance_slot["token"]==token:_maintenance_slot=None
        thread=threading.Thread(target=run,name="feedback-recall-maintenance",daemon=True)
        _maintenance_slot={"token":token,"root":root,"stop":stop,"thread":thread}
        try:thread.start()
        except BaseException:
            abandoned.set();permit.set();_maintenance_slot=None
            raise
        if not _maintenance_atexit:atexit.register(_shutdown_maintenance);_maintenance_atexit=True
    permit.set()
    return token


def stop_maintenance(token, timeout=.25):
    """Release only the creating call's birth; False means not stopped/adopted."""
    with _maintenance_lock:
        slot=_maintenance_slot
        if slot is None or token is None or slot["token"]!=token:return False
        slot["stop"].set()
    if slot["thread"] is not threading.current_thread():slot["thread"].join(timeout=max(0,min(.25,timeout)))
    with _maintenance_lock:
        return _maintenance_slot is None or _maintenance_slot["token"]!=token


def _shutdown_maintenance():
    with _maintenance_lock:
        token=_maintenance_slot["token"] if _maintenance_slot is not None else None
    if token is not None:stop_maintenance(token)
