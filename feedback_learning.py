"""Learn local task preferences from explicit thumbs-up and thumbs-down reviews.

This small, offline instance-based learner ranks past feedback by TF-IDF
similarity. It does not upload examples, rewrite the person's current request,
or pretend that a thumbs-up identifies which detail of a result was good.
"""

from __future__ import annotations

import math
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
            conn.execute("ALTER TABLE feedback RENAME TO feedback_legacy")
            conn.execute(schema)
            conn.execute("""INSERT INTO feedback
                (id,task_id,generation,kind,rating,title,brief,note,created)
                SELECT id,task_id,generation,kind,rating,title,brief,note,created
                FROM feedback_legacy""")
            conn.execute("DROP TABLE feedback_legacy")
            conn.execute("CREATE INDEX feedback_kind ON feedback(kind, created DESC)")
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
    if re.search(r"\bdesign\b", text):
        return "app_design"
    if re.search(r"\b(?:video|animation|film)\b", text):
        return "video"
    return "general"


def record(root: str | Path, task: dict, rating: int, note: str = "") -> int:
    """Save the current explicit review per task generation, idempotently."""
    if rating not in (-1, 1):
        raise ValueError("A review must be thumbs up or thumbs down.")
    note = " ".join(str(note).split())[:2000]
    if rating < 0 and not note:
        raise ValueError("Tell Cale what to improve before giving thumbs down.")
    with _connect(root) as conn:
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
    with _connect(root) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT id,generation,kind,rating,note,created FROM feedback "
            "WHERE task_id=? ORDER BY created DESC LIMIT 12", (task_id,)
        )]


def forget(root: str | Path, feedback_id: int) -> bool:
    """Let the owner retract a learned example without changing task files."""
    with _connect(root) as conn:
        return conn.execute("DELETE FROM feedback WHERE id=?", (feedback_id,)).rowcount > 0


def _terms(text: str) -> Counter:
    return Counter(word for word in WORD.findall(text.lower()) if word not in STOP)


def _similarity(left: Counter, right: Counter, idf: dict) -> float:
    common = left.keys() & right.keys()
    numerator = sum(left[word] * right[word] * idf[word] ** 2 for word in common)
    left_norm = math.sqrt(sum(count ** 2 * idf[word] ** 2 for word, count in left.items()))
    right_norm = math.sqrt(sum(count ** 2 * idf[word] ** 2 for word, count in right.items()))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def guidance(root: str | Path, kind: str, request: str,
             exclude_task_id: str = "", limit: int = 3) -> str:
    """Retrieve relevant lessons using a locally learned similarity ranking."""
    if not request.strip() or limit <= 0:
        return ""
    with _connect(root) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT task_id,rating,title,brief,note,created FROM feedback AS candidate "
            "WHERE kind=? AND task_id!=? AND NOT EXISTS ("
            "SELECT 1 FROM feedback AS newer "
            "WHERE newer.task_id=candidate.task_id "
            "AND newer.generation>candidate.generation) "
            "ORDER BY created DESC LIMIT 300",
            (kind, exclude_task_id),
        )]
    if not rows:
        return ""
    query = _terms(request)
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
        if row["rating"] < 0 and row["note"]:
            lessons.append("Improve: " + row["note"][:240])
        elif row["rating"] > 0 and score >= 0.30:
            lessons.append("Similar task the user liked: " + row["title"][:140])
        if len(lessons) >= min(3, limit):
            break
    if not lessons:
        return ""
    return ("Past user feedback for similar work (use only when relevant; "
            "the current request takes priority):\n- " + "\n- ".join(lessons))
