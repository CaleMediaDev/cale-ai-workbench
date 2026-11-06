"""Reproduce bounded recall on synthetic data in an automatically removed TEMP DB.

Run from any directory: python benchmarks/feedback_recall_scale.py --rows 100000
Optional --report FILE exclusively creates FILE; existing files are never replaced.
The 900-second budget is cooperative, not a hard timeout on SQLite/filesystem I/O.
Use an external process timeout when a hard wall-clock limit is required.
No user-data directory, network, model, or background maintenance worker is used.
"""

from __future__ import annotations

import argparse
from contextlib import closing, nullcontext
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time


MAX_ROWS = 100_000
MAX_BYTES = 200 * 1024 * 1024
MAX_SECONDS = 900.0
REPOSITORY = Path(__file__).resolve().parents[1]


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def load_feedback():
    # Import only the repository's module; do not resolve a same-named CWD module.
    sys.dont_write_bytecode = True
    path = REPOSITORY / "feedback_learning.py"
    source_hash = digest(path)
    spec = importlib.util.spec_from_file_location("benchmark_feedback_learning", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == path.resolve()
    assert module._maintenance_slot is None
    return module, path, source_hash


def database_bytes(root):
    return sum(path.stat().st_size for path in root.glob("feedback-learning.sqlite3*")
               if path.is_file())


def raw_counts(path, module):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        counts = {"reviews": db.execute("SELECT count(*) FROM feedback").fetchone()[0],
                  "generation_floors": db.execute("SELECT count(*) FROM feedback_generations").fetchone()[0]}
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='feedback_recall_meta_v1'").fetchone():
            return counts, None
        meta = dict(db.execute("SELECT * FROM feedback_recall_meta_v1 WHERE id=1").fetchone())
        counts.update(
            indexed_docs=db.execute("SELECT count(*) FROM feedback_recall_docs_v1 WHERE epoch=?", (meta["epoch"],)).fetchone()[0],
            postings=db.execute("SELECT count(*) FROM feedback_recall_terms_v1 WHERE epoch=?", (meta["epoch"],)).fetchone()[0],
            pending_changes=db.execute("SELECT count(*) FROM feedback_recall_changes_v1").fetchone()[0],
            document_frequencies=[list(row) for row in db.execute(
                "SELECT term,docs FROM feedback_recall_df_v1 WHERE epoch=? ORDER BY term", (meta["epoch"],))],
        )
        assert meta["version"] == module._recall_version()
        return counts, meta


def benchmark(rows, seconds):
    module, source, source_hash = load_feedback()
    started = time.monotonic()
    deadline = started + seconds
    result = {"format": 1, "data": "owned temporary synthetic reviews only",
              "requested_rows": rows, "soft_budget_seconds": seconds,
              "source_sha256": source_hash, "token_version": module._recall_version(),
              "python_version": sys.version.split()[0], "sqlite_version": sqlite3.sqlite_version,
              "provenance": "Same source performs raw seeding, actual maintenance, and read-only queries; no derived index rows are seeded.",
              "limits": "Cooperative budgets cannot preempt native SQLite/filesystem calls. Lexical capped recall is advisory, not exhaustive semantic matching or whole-app performance."}
    with tempfile.TemporaryDirectory(prefix="cale-feedback-recall-benchmark-") as temporary:
        root = Path(temporary)
        path = root / "feedback-learning.sqlite3"
        with module._connect(root) as db:
            db.execute("INSERT INTO feedback_generations VALUES('rare-old',0)")
            db.execute("INSERT INTO feedback(task_id,generation,kind,rating,title,brief,note,created) VALUES('rare-old',0,'music',-1,'ambernebula','ambernebula','Remember the old rare lesson',0)")
            db.executemany("INSERT INTO feedback_generations VALUES(?,0)",
                           (("scale-" + str(i),) for i in range(rows - 1)))
            db.executemany("INSERT INTO feedback(task_id,generation,kind,rating,title,brief,note,created) VALUES(?,0,'music',-1,'birch','birch','Common lesson',?)",
                           (("scale-" + str(i), float(i + 1)) for i in range(rows - 1)))
        counts, meta = raw_counts(path, module)
        assert counts["reviews"] == counts["generation_floors"] == rows and meta is None
        result["seed_seconds"] = time.monotonic() - started
        batches = unavailable = consecutive_unavailable = 0
        maximum = {"rows": 0, "text_bytes": 0, "posting_mutations": 0}
        terminal = "budget_partial"
        while time.monotonic() < deadline:
            if database_bytes(root) >= MAX_BYTES:
                terminal = "size_budget_partial"
                break
            batch = module.maintain(root)
            batches += 1
            if batch["status"] == "unavailable":
                unavailable += 1
                consecutive_unavailable += 1
            else:
                consecutive_unavailable = 0
                for key in maximum:
                    maximum[key] = max(maximum[key], batch.get(key, 0))
            assert maximum["rows"] <= module.RECALL_BATCH_ROWS
            assert maximum["text_bytes"] <= module.RECALL_BATCH_BYTES
            assert maximum["posting_mutations"] <= module.RECALL_BATCH_MUTATIONS
            if batch["status"] == "ready":
                terminal = "ready"
                break
            if consecutive_unavailable >= 10:
                terminal = "unavailable_partial"
                break
        counts, meta = raw_counts(path, module)
        result.update(terminal=terminal, maintenance_seconds=time.monotonic() - started - result["seed_seconds"],
                      batches=batches, unavailable_batches=unavailable, maximum_batch=maximum,
                      database_bytes=database_bytes(root), counts=counts, metadata=meta)
        assert result["database_bytes"] < MAX_BYTES
        if terminal == "ready":
            assert meta["complete"] == 1 and meta["omitted"] == 0
            assert counts["indexed_docs"] == counts["postings"] == rows
            assert counts["pending_changes"] == 0
            assert sum(count for _, count in counts["document_frequencies"]) == rows
        before = digest(path)
        queries = {}
        for name, request in (("rare", "ambernebula"), ("common", "birch"),
                              ("stopwords", "the and please with your"), ("no_match", "unseenzirconium")):
            tick = time.perf_counter()
            response = module.guidance_details(root, "music", request)
            queries[name] = {"seconds": time.perf_counter() - tick,
                             "status": response["status"], "lesson_present": bool(response["text"])}
            assert response["status"]["metadata_rows"] <= module.RECALL_METADATA_ROWS
            assert response["status"]["text_rows"] <= module.RECALL_CANDIDATES
            assert response["status"]["text_bytes"] <= module.RECALL_TEXT_BYTES
            if terminal == "ready" and name in ("rare", "stopwords", "no_match"):
                assert not response["status"]["fallback_used"]
                assert response["status"]["text_rows"] == int(name == "rare")
                if name == "rare":
                    assert "Remember the old rare lesson" in response["text"]
        after = digest(path)
        assert before == after
        result.update(queries=queries, database_sha256_before_queries=before,
                      database_sha256_after_queries=after, queries_left_database_unchanged=True)
    assert digest(source) == source_hash
    assert module._maintenance_slot is None
    result.update(source_unchanged=True, temporary_database_removed=True,
                  total_seconds=time.monotonic() - started)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=MAX_ROWS, help="Synthetic reviews, 1..100000")
    parser.add_argument("--seconds", type=float, default=MAX_SECONDS, help="Soft maintenance budget, >0..900")
    parser.add_argument("--report", type=Path, help="Exclusively create a JSON report; parent directory must exist")
    args = parser.parse_args()
    if not 1 <= args.rows <= MAX_ROWS or not 0 < args.seconds <= MAX_SECONDS:
        parser.error("rows must be 1..100000 and seconds must be >0..900")
    # Reserve before work; no clobber, directory creation, or output path used as DB input.
    with (args.report.open("x", encoding="utf-8") if args.report else nullcontext()) as report:
        result = benchmark(args.rows, args.seconds)
        encoded = json.dumps(result, indent=2, allow_nan=False)
        if report is not None:
            report.write(encoded + "\n")
        print(encoded)


if __name__ == "__main__":
    main()
