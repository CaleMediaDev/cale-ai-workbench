"""Explicit reviews teach relevant local preferences and can be forgotten."""

import feedback_learning


def task(identifier, brief, title="Make a song", generation=0):
    return {"id": identifier, "title": title, "brief": brief,
            "card": "music-job:" + identifier, "generation": generation}


def test_down_feedback_guides_similar_song_and_ignores_unrelated_request(tmp_path):
    bad = task("song-1", "Make a techno song with synth bass and female singing")
    feedback_id = feedback_learning.record(
        tmp_path, bad, -1, "Use electronic drums; guitars ruined the techno sound"
    )
    learned = feedback_learning.guidance(
        tmp_path, "music", "Make another techno song with synth bass"
    )
    assert "electronic drums" in learned
    assert not feedback_learning.guidance(
        tmp_path, "music", "Make a classical piano sonata"
    )
    assert not feedback_learning.guidance(
        tmp_path, "image", "Make a techno poster"
    )
    assert feedback_learning.for_task(tmp_path, "song-1")[0]["id"] == feedback_id
    assert feedback_learning.forget(tmp_path, feedback_id)
    assert not feedback_learning.guidance(
        tmp_path, "music", "Make another techno song with synth bass"
    )


def test_up_rating_is_idempotent_and_becomes_similar_success_example(tmp_path):
    liked = task("song-2", "Make a beach pop song with a female lead", title="Beach pop")
    first = feedback_learning.record(tmp_path, liked, 1)
    assert feedback_learning.record(tmp_path, liked, 1) == first
    suggestion = feedback_learning.guidance(
        tmp_path, "music", "Make another beach pop song with a female lead"
    )
    assert "Beach pop" in suggestion
    assert len(feedback_learning.for_task(tmp_path, "song-2")) == 1


def test_repeated_review_refreshes_task_snapshot_for_matching(tmp_path):
    original = task("edited-song", "Techno synth bass", title="Techno synth bass")
    first = feedback_learning.record(tmp_path, original, -1, "Use electronic drums")
    assert "Use electronic drums" in feedback_learning.guidance(
        tmp_path, "music", "Techno synth bass"
    )

    revised = task(
        "edited-song", "Classical piano sonata", title="Classical piano sonata"
    )
    assert feedback_learning.record(
        tmp_path, revised, -1, "Use softer piano dynamics"
    ) == first
    assert len(feedback_learning.for_task(tmp_path, "edited-song")) == 1
    assert not feedback_learning.guidance(tmp_path, "music", "Techno synth bass")
    learned = feedback_learning.guidance(tmp_path, "music", "Classical piano sonata")
    assert "Use softer piano dynamics" in learned
    assert "Use electronic drums" not in learned


def test_updated_review_refreshes_recency_for_guidance(tmp_path, monkeypatch):
    timestamps = iter([10.0, 20.0, 30.0])
    monkeypatch.setattr(feedback_learning.time, "time", lambda: next(timestamps))
    original = task("refreshed-song", "Techno synth bass", title="Techno synth bass")
    competing = task("other-song", "Techno synth bass", title="Techno synth bass")

    first = feedback_learning.record(tmp_path, original, -1, "Use electronic drums")
    feedback_learning.record(tmp_path, competing, -1, "Use a slower tempo")
    assert "Use a slower tempo" in feedback_learning.guidance(
        tmp_path, "music", "Techno synth bass", limit=1
    )

    assert feedback_learning.record(
        tmp_path, original, -1, "Use softer electronic drums"
    ) == first
    reviews = feedback_learning.for_task(tmp_path, "refreshed-song")
    assert len(reviews) == 1
    assert reviews[0]["created"] == 30.0
    learned = feedback_learning.guidance(
        tmp_path, "music", "Techno synth bass", limit=1
    )
    assert "Use softer electronic drums" in learned
    assert "Use a slower tempo" not in learned


def test_schema_initialization_failure_closes_connection(tmp_path, monkeypatch):
    import sqlite3
    from unittest.mock import Mock

    import pytest

    connection = Mock()
    connection.execute.side_effect = [
        None,
        sqlite3.OperationalError("index initialization failed"),
    ]
    monkeypatch.setattr(feedback_learning.sqlite3, "connect", Mock(return_value=connection))

    with pytest.raises(sqlite3.OperationalError, match="index initialization failed"):
        feedback_learning.for_task(tmp_path, "song-1")

    assert connection.execute.call_count == 2
    connection.close.assert_called_once_with()
    connection.commit.assert_not_called()


def test_rating_change_replaces_only_same_generation_review(tmp_path):
    reviewed = task("changed-vote", "Techno synth bass", title="Techno synth bass")
    feedback_learning.record(tmp_path, reviewed, 1)
    feedback_learning.record(tmp_path, reviewed, -1, "Use electronic drums")
    reviews = feedback_learning.for_task(tmp_path, "changed-vote")
    assert len(reviews) == 1
    assert reviews[0]["rating"] == -1
    assert "Use electronic drums" in feedback_learning.guidance(
        tmp_path, "music", "Techno synth bass"
    )

    current_id = feedback_learning.record(tmp_path, reviewed, 1)
    assert feedback_learning.record(tmp_path, reviewed, 1) == current_id
    reviews = feedback_learning.for_task(tmp_path, "changed-vote")
    assert len(reviews) == 1
    assert reviews[0]["rating"] == 1
    assert reviews[0]["note"] == ""
    learned = feedback_learning.guidance(tmp_path, "music", "Techno synth bass")
    assert "Similar task the user liked: Techno synth bass" in learned
    assert "Use electronic drums" not in learned

    next_generation = dict(reviewed, generation=1)
    feedback_learning.record(tmp_path, next_generation, -1, "Use a slower tempo")
    reviews = feedback_learning.for_task(tmp_path, "changed-vote")
    assert {(row["generation"], row["rating"]) for row in reviews} == {
        (0, 1), (1, -1)
    }


def test_guidance_uses_latest_reviewed_generation_not_latest_review_time(tmp_path, monkeypatch):
    timestamps = iter([10.0, 20.0, 30.0])
    monkeypatch.setattr(feedback_learning.time, "time", lambda: next(timestamps))
    original = task("revised-song", "Techno synth bass", title="Techno synth bass")
    feedback_learning.record(tmp_path, original, -1, "Use electronic drums")
    feedback_learning.record(tmp_path, dict(original, generation=1), 1)

    learned = feedback_learning.guidance(tmp_path, "music", "Techno synth bass")
    assert "Similar task the user liked: Techno synth bass" in learned
    assert "Use electronic drums" not in learned

    # Reviewing an older output again must not supersede the newer generation.
    feedback_learning.record(tmp_path, original, -1, "Use softer electronic drums")
    reviews = feedback_learning.for_task(tmp_path, "revised-song")
    assert {(row["generation"], row["rating"]) for row in reviews} == {
        (0, -1), (1, 1)
    }
    learned = feedback_learning.guidance(tmp_path, "music", "Techno synth bass")
    assert "Similar task the user liked: Techno synth bass" in learned
    assert "Improve:" not in learned


def test_forgotten_feedback_id_cannot_delete_a_later_review(tmp_path):
    import sqlite3

    for legacy in (False, True):
        root = tmp_path / ("legacy" if legacy else "fresh")
        root.mkdir()
        if legacy:
            with sqlite3.connect(root / "feedback-learning.sqlite3") as conn:
                conn.execute("""CREATE TABLE feedback (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
                    generation INTEGER NOT NULL, kind TEXT NOT NULL,
                    rating INTEGER NOT NULL, title TEXT NOT NULL,
                    brief TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, UNIQUE(task_id, generation, rating)
                )""")
                conn.execute("""INSERT INTO feedback VALUES
                    (7, 'old-song', 0, 'music', -1, 'Techno synth bass',
                     'Techno synth bass', 'Use electronic drums', 10.0)""")
            reviews = feedback_learning.for_task(root, "old-song")
            assert len(reviews) == 1
            assert reviews[0]["id"] == 7
            assert reviews[0]["note"] == "Use electronic drums"
            old_id = reviews[0]["id"]
        else:
            old_id = feedback_learning.record(
                root, task("old-song", "Techno synth bass"),
                -1, "Use electronic drums"
            )

        assert feedback_learning.forget(root, old_id)
        new_id = feedback_learning.record(
            root, task("new-song", "Classical piano sonata"),
            -1, "Use softer piano dynamics"
        )
        assert new_id > old_id
        assert not feedback_learning.forget(root, old_id)
        reviews = feedback_learning.for_task(root, "new-song")
        assert len(reviews) == 1
        assert reviews[0]["id"] == new_id
        assert "Use softer piano dynamics" in feedback_learning.guidance(
            root, "music", "Classical piano sonata"
        )


def test_duplicate_lessons_do_not_displace_distinct_feedback(tmp_path, monkeypatch):
    timestamps = iter([10.0, 20.0, 30.0])
    monkeypatch.setattr(feedback_learning.time, "time", lambda: next(timestamps))
    for identifier, note in (
        ("distinct-song", "Use a slower tempo"),
        ("duplicate-song-1", "Use electronic drums"),
        ("duplicate-song-2", "Use electronic drums"),
    ):
        feedback_learning.record(
            tmp_path,
            task(identifier, "Techno synth bass", title="Techno synth bass"),
            -1, note,
        )

    learned = feedback_learning.guidance(
        tmp_path, "music", "Techno synth bass", limit=2
    )
    assert learned.count("Improve: Use electronic drums") == 1
    assert "Improve: Use a slower tempo" in learned
    assert learned.count("\n- ") == 2
    assert learned.index("Use electronic drums") < learned.index("Use a slower tempo")
    # Deduplication affects retrieval only; each task retains its own review.
    for identifier in ("distinct-song", "duplicate-song-1", "duplicate-song-2"):
        assert len(feedback_learning.for_task(tmp_path, identifier)) == 1


def test_existing_opposite_ratings_use_only_latest_review(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(feedback_learning.time, "time", lambda: 10.0)
    for latest_time in (20.0, 10.0):
        root = tmp_path / str(latest_time)
        reviewed = task("legacy-votes", "Techno synth bass", title="Techno synth bass")
        feedback_learning.record(root, reviewed, -1, "Use electronic drums")
        # Existing databases can contain both ratings under the unique constraint.
        # An equal timestamp must still prefer the later inserted review.
        with sqlite3.connect(root / "feedback-learning.sqlite3") as conn:
            conn.execute("""INSERT INTO feedback
                (task_id,generation,kind,rating,title,brief,note,created)
                SELECT task_id,generation,kind,1,title,brief,'',?
                FROM feedback WHERE task_id=?""", (latest_time, "legacy-votes"))

        assert len(feedback_learning.for_task(root, "legacy-votes")) == 2
        learned = feedback_learning.guidance(root, "music", "Techno synth bass")
        assert "Similar task the user liked: Techno synth bass" in learned
        assert "Use electronic drums" not in learned
        assert "Improve:" not in learned


def test_retracting_latest_legacy_vote_does_not_revive_opposite_rating(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(feedback_learning.time, "time", lambda: 10.0)
    for latest_time in (20.0, 10.0):
        root = tmp_path / str(latest_time)
        reviewed = task("legacy-retraction", "Techno synth bass", title="Techno synth bass")
        historical_id = feedback_learning.record(
            root, reviewed, -1, "Historical preference"
        )
        feedback_learning.record(
            root, dict(reviewed, generation=1), -1, "Use electronic drums"
        )
        with sqlite3.connect(root / "feedback-learning.sqlite3") as conn:
            latest_id = conn.execute("""INSERT INTO feedback
                (task_id,generation,kind,rating,title,brief,note,created)
                SELECT task_id,generation,kind,1,title,brief,'',?
                FROM feedback WHERE task_id=? AND generation=1""",
                (latest_time, reviewed["id"])).lastrowid

        assert "Similar task the user liked:" in feedback_learning.guidance(
            root, "music", "Techno synth bass"
        )
        assert feedback_learning.forget(root, latest_id)
        assert not feedback_learning.guidance(root, "music", "Techno synth bass")
        assert not feedback_learning.forget(root, latest_id)
        # Only superseded reviews from the retracted generation are removed.
        remaining = feedback_learning.for_task(root, reviewed["id"])
        assert [row["id"] for row in remaining] == [historical_id]


def test_generation_history_backfill_runs_once_across_connections(tmp_path, monkeypatch):
    import sqlite3

    reviewed = task("legacy", "Techno synth bass", title="Techno synth bass")
    feedback_learning.record(tmp_path, dict(reviewed, generation=2), 1)
    database = tmp_path / "feedback-learning.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE feedback_generations")
    original_connect = sqlite3.connect
    statements = []

    def traced(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced)
    for _ in range(2):
        with feedback_learning._connect(tmp_path) as connection:
            assert connection.execute(
                "SELECT generation FROM feedback_generations WHERE task_id='legacy'"
            ).fetchone()[0] == 2
    assert sum("SELECT task_id,MAX(generation) FROM feedback" in sql for sql in statements) == 1


def test_retracting_latest_generation_does_not_revive_stale_feedback(tmp_path):
    original = task("retracted-song", "Techno synth bass", title="Techno synth bass")
    old_id = feedback_learning.record(
        tmp_path, original, -1, "Use electronic drums"
    )
    latest = dict(original, generation=1)
    latest_id = feedback_learning.record(tmp_path, latest, 1)
    assert "Similar task the user liked:" in feedback_learning.guidance(
        tmp_path, "music", "Techno synth bass"
    )

    assert feedback_learning.forget(tmp_path, latest_id)
    # Historical reviews remain available for explicit retraction, but opening
    # new connections must not reactivate their superseded preferences.
    assert feedback_learning.for_task(tmp_path, original["id"])[0]["id"] == old_id
    assert not feedback_learning.guidance(tmp_path, "music", "Techno synth bass")
    assert feedback_learning.record(
        tmp_path, original, -1, "Use softer electronic drums"
    ) == old_id
    assert not feedback_learning.guidance(tmp_path, "music", "Techno synth bass")

    # An explicit new review of the current generation remains eligible.
    feedback_learning.record(tmp_path, latest, -1, "Use a slower tempo")
    learned = feedback_learning.guidance(tmp_path, "music", "Techno synth bass")
    assert "Use a slower tempo" in learned
    assert "electronic drums" not in learned


def test_video_design_feedback_is_not_grouped_with_app_design(tmp_path):
    for medium in ("video", "animation", "film"):
        assert feedback_learning.kind_for({"title": "Design a " + medium}) == "video"
    assert feedback_learning.kind_for({"title": "Design a dashboard"}) == "app_design"
    assert feedback_learning.kind_for({"title": "Design a logo"}) == "app_design"

    reviewed = {
        "id": "video-design",
        "title": "Design a video",
        "brief": "Neon geometric transitions",
    }
    feedback_learning.record(tmp_path, reviewed, -1, "Use slower transitions")
    assert "Use slower transitions" in feedback_learning.guidance(
        tmp_path, "video", "Neon geometric transitions"
    )
    assert not feedback_learning.guidance(
        tmp_path, "app_design", "Neon geometric transitions"
    )


def test_review_requires_comment_for_thumbs_down(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="what to improve"):
        feedback_learning.record(tmp_path, task("song-3", "Music"), -1, "")


def test_concurrent_legacy_migration_preserves_deleted_id_high_water(tmp_path, monkeypatch):
    """A stale connection must not rebuild the table after another migration."""
    import sqlite3
    import threading
    from concurrent.futures import ThreadPoolExecutor

    db = tmp_path / "feedback-learning.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE feedback (
            id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, generation INTEGER NOT NULL,
            kind TEXT NOT NULL, rating INTEGER NOT NULL, title TEXT NOT NULL,
            brief TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
            UNIQUE(task_id, generation, rating))""")
        conn.execute("INSERT INTO feedback VALUES (7,'legacy',0,'music',1,'Old','Old','',1)")

    waiting = threading.Event()
    migrated = threading.Event()
    real_connect = sqlite3.connect

    class WaitingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "BEGIN IMMEDIATE":
                waiting.set()
                assert migrated.wait(5), "first writer did not finish migration"
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):
        if threading.current_thread().name.startswith("stale-feedback"):
            kwargs["factory"] = WaitingConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(feedback_learning.sqlite3, "connect", connect)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale-feedback") as pool:
        future = pool.submit(feedback_learning.record, tmp_path, task("second", "Music"), 1)
        try:
            assert waiting.wait(5), "second connection did not reach migration lock"
            deleted_id = feedback_learning.record(tmp_path, task("first", "Music"), 1)
            assert deleted_id > 7
            assert feedback_learning.forget(tmp_path, deleted_id)
        finally:
            migrated.set()
        later_id = future.result(timeout=5)

    assert later_id > deleted_id, "stale migration recycled a retracted review ID"
    assert not feedback_learning.forget(tmp_path, deleted_id)
    assert feedback_learning.for_task(tmp_path, "legacy")[0]["id"] == 7
    assert feedback_learning.for_task(tmp_path, "second")[0]["id"] == later_id
    with real_connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='feedback_legacy'").fetchone() is None


# Derived recall indexing is driven explicitly in owned fixtures; read APIs
# and record/forget never start maintenance threads.
def _finish_recall(root, steps=100):
    for _ in range(steps):
        result = feedback_learning.maintain(root)
        assert result["status"] != "unavailable", result
        if result["status"] == "ready": return result
    raise AssertionError("bounded backfill did not finish")


def test_indexed_old_lesson_keeps_priority_before_recent_fallback(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(feedback_learning.time, "time", lambda: clock[0])
    feedback_learning.record(tmp_path, task("old", "Techno synth bass", title="Techno synth bass"), -1, "Use electronic drums")
    for index in range(300):
        clock[0] = index + 1.0
        feedback_learning.record(tmp_path, task(str(index), "Classical piano sonata", title="Classical piano sonata"), -1, "Use softer piano dynamics")
    assert not feedback_learning.guidance(tmp_path, "music", "Techno synth bass")
    _finish_recall(tmp_path)
    result = feedback_learning.guidance_details(tmp_path, "music", "Techno synth bass", limit=1)
    assert "Use electronic drums" in result["text"]
    assert "Use softer piano dynamics" not in result["text"]
    assert result["status"]["candidate_rows"] <= 300
    assert result["status"]["metadata_rows"] <= 2048
    assert result["status"]["text_bytes"] <= 8 * 1024 * 1024


def test_recall_read_missing_database_creates_nothing(tmp_path, monkeypatch):
    root = tmp_path / "absent"
    monkeypatch.setattr(feedback_learning, "_connect", lambda *a: (_ for _ in ()).throw(AssertionError("read migration")))
    assert feedback_learning.guidance(root, "music", "Techno synth bass") == ""
    assert feedback_learning.for_task(root, "task") == []
    assert not root.exists()


def test_old_writer_changes_are_captured_and_stale_text_is_not_recalled(tmp_path):
    import sqlite3
    ident = feedback_learning.record(tmp_path, task("old-writer", "Techno synth bass"), -1, "Old note")
    _finish_recall(tmp_path)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        db.execute("UPDATE feedback SET note='New note' WHERE id=?", (ident,))
        assert db.execute("SELECT 1 FROM feedback_recall_changes_v1 WHERE event='doc'").fetchone()
    assert "Old note" not in feedback_learning.guidance(tmp_path, "music", "Techno synth bass")
    assert "New note" in feedback_learning.guidance(tmp_path, "music", "Techno synth bass")
    _finish_recall(tmp_path)
    assert feedback_learning.forget(tmp_path, ident)
    assert not feedback_learning.guidance(tmp_path, "music", "Techno synth bass")


def test_generation_floor_changes_are_observed_before_maintenance(tmp_path):
    import sqlite3
    feedback_learning.record(tmp_path, task("floor", "Techno synth bass"), -1, "Use electronic drums")
    _finish_recall(tmp_path)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        db.execute("UPDATE feedback_generations SET generation=2 WHERE task_id='floor'")
        assert db.execute("SELECT 1 FROM feedback_recall_changes_v1 WHERE event='floor'").fetchone()
    assert not feedback_learning.guidance(tmp_path, "music", "Techno synth bass")


def test_index_checkpoint_and_queue_roll_back_together(tmp_path):
    import sqlite3
    feedback_learning.record(tmp_path, task("crash", "Techno synth bass"), -1, "Use electronic drums")
    feedback_learning.maintain(tmp_path)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        db.execute("UPDATE feedback SET note='Changed note'")
        before = (db.execute("SELECT * FROM feedback_recall_meta_v1").fetchall(), db.execute("SELECT * FROM feedback_recall_changes_v1").fetchall())
    def fail():raise OSError("simulated interrupted commit")
    assert feedback_learning.maintain(tmp_path, before_commit=fail)["status"] == "unavailable"
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        assert before == (db.execute("SELECT * FROM feedback_recall_meta_v1").fetchall(), db.execute("SELECT * FROM feedback_recall_changes_v1").fetchall())
    _finish_recall(tmp_path)



def test_recall_omitted_rows_are_idempotent_and_recoverable(tmp_path):
    import sqlite3
    ident = feedback_learning.record(tmp_path, task("bad", "Techno synth bass"), -1, "Use electronic drums")
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        db.execute("UPDATE feedback SET brief=? WHERE id=?", ("x" * 4001, ident))
    for _ in range(3):
        result = feedback_learning.maintain(tmp_path)
        assert result["status"] in {"partial", "limited"}
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        assert db.execute("SELECT omitted FROM feedback_recall_meta_v1").fetchone()[0] == 1
        db.execute("UPDATE feedback SET brief='Techno synth bass' WHERE id=?", (ident,))
    _finish_recall(tmp_path)
    assert "Use electronic drums" in feedback_learning.guidance(tmp_path, "music", "Techno synth bass")
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        assert db.execute("SELECT omitted FROM feedback_recall_meta_v1").fetchone()[0] == 0


def test_lost_generation_floor_table_never_revives_old_lessons(tmp_path):
    import sqlite3
    feedback_learning.record(tmp_path, task("lost-floor", "Techno synth bass"), -1, "Old lesson")
    newer = feedback_learning.record(tmp_path, task("lost-floor", "Techno synth bass", generation=1), 1)
    feedback_learning.forget(tmp_path, newer)
    _finish_recall(tmp_path)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        db.execute("DROP TABLE feedback_generations")
    assert not feedback_learning.guidance(tmp_path, "music", "Techno synth bass")
    assert feedback_learning.maintain(tmp_path)["status"] == "unavailable"


def test_record_does_not_rebuild_lost_floors_from_superseded_reviews(tmp_path):
    import sqlite3
    import pytest

    original = task("lost-floor-write", "Techno synth bass", title="Techno synth bass")
    old_id = feedback_learning.record(tmp_path, original, -1, "Stale preference")
    latest_id = feedback_learning.record(tmp_path, dict(original, generation=1), 1)
    assert feedback_learning.forget(tmp_path, latest_id)
    _finish_recall(tmp_path)

    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        db.execute("DROP TABLE feedback_generations")

    # An unrelated write must not backfill generation zero from the retained
    # historical review and thereby make that preference eligible again.
    with pytest.raises(sqlite3.DatabaseError, match="generation floors disappeared"):
        feedback_learning.record(tmp_path, task("unrelated", "Classical piano sonata"), 1)

    assert not feedback_learning.guidance(tmp_path, "music", "Techno synth bass")
    assert feedback_learning.for_task(tmp_path, original["id"])[0]["id"] == old_id
    assert feedback_learning.for_task(tmp_path, "unrelated") == []
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='feedback_generations'"
        ).fetchone()


def test_recall_lifecycle_is_singleton_and_starts_no_database_work_on_caller(tmp_path, monkeypatch):
    from unittest.mock import Mock
    threads = []
    class FakeThread:
        def __init__(self, target, **kwargs): self.target = target; threads.append(self)
        def start(self): pass
        def join(self, timeout): assert 0 <= timeout <= .25
    monkeypatch.setattr(feedback_learning, "_maintenance_slot", None)
    monkeypatch.setattr(feedback_learning, "_maintenance_atexit", False)
    monkeypatch.setattr(feedback_learning.threading, "Thread", FakeThread)
    monkeypatch.setattr(feedback_learning.atexit, "register", Mock())
    probe = Mock(return_value={"status": "ready"})
    monkeypatch.setattr(feedback_learning, "maintain", probe)
    first = feedback_learning.start_maintenance(tmp_path)
    assert feedback_learning.start_maintenance(tmp_path) is None
    assert feedback_learning.start_maintenance(tmp_path / "other") is None
    assert len(threads) == 1 and not probe.called
    feedback_learning.stop_maintenance("not-the-owner")
    assert not feedback_learning._maintenance_slot["stop"].is_set()
    feedback_learning.stop_maintenance(first)
    threads[0].target()
    assert feedback_learning._maintenance_slot is None and not probe.called


def test_failed_lifecycle_start_abandons_late_target_without_clearing_new_owner(tmp_path, monkeypatch):
    import pytest
    from unittest.mock import Mock
    targets = []
    class FakeThread:
        def __init__(self, target, **kwargs): self.target = target; targets.append(target)
        def start(self):
            if len(targets) == 1: raise RuntimeError("after native launch")
        def join(self, timeout): pass
    monkeypatch.setattr(feedback_learning, "_maintenance_slot", None)
    monkeypatch.setattr(feedback_learning, "_maintenance_atexit", False)
    monkeypatch.setattr(feedback_learning.threading, "Thread", FakeThread)
    monkeypatch.setattr(feedback_learning.atexit, "register", Mock())
    probe = Mock(return_value={"status": "ready"})
    monkeypatch.setattr(feedback_learning, "maintain", probe)
    with pytest.raises(RuntimeError): feedback_learning.start_maintenance(tmp_path)
    token = feedback_learning.start_maintenance(tmp_path)
    targets[0]()
    assert feedback_learning._maintenance_slot["token"] == token and not probe.called
    feedback_learning.stop_maintenance(token); targets[1]()
    assert feedback_learning._maintenance_slot is None



def _bulk_feedback(root, amount, brief="Techno synth bass"):
    import sqlite3
    feedback_learning.record(root, task("bulk-first", brief, title=brief), -1, "Preference first")
    with sqlite3.connect(root / "feedback-learning.sqlite3") as db:
        db.executemany("INSERT INTO feedback(task_id,generation,kind,rating,title,brief,note,created) VALUES(?,0,'music',-1,?,?,?,?)",
                       (("bulk-"+str(i),brief,brief,"Preference "+str(i),float(i+1)) for i in range(amount-1)))
        db.executemany("INSERT INTO feedback_generations VALUES(?,0)", (("bulk-"+str(i),) for i in range(amount-1)))


def test_common_term_queries_keep_all_python_row_and_text_caps(tmp_path, monkeypatch):
    _bulk_feedback(tmp_path, 1000)
    _finish_recall(tmp_path, steps=200)
    original=feedback_learning._doc
    calls=[]
    def observed(*args, **kwargs):
        result=original(*args, **kwargs)
        if result[0] is not None:calls.append(1)
        return result
    monkeypatch.setattr(feedback_learning, "_doc", observed)
    result=feedback_learning.guidance_details(tmp_path,"music","Techno synth bass")
    assert result["text"] and result["status"]["limited"] is True
    assert len(calls)==result["status"]["text_rows"]<=300
    assert result["status"]["metadata_rows"]<=2048
    assert result["status"]["text_bytes"]<=feedback_learning.RECALL_TEXT_BYTES
    long_request=" ".join("term"+str(i) for i in range(2000))
    limited=feedback_learning.guidance_details(tmp_path,"music",long_request)
    assert limited["status"]["limited"] is True
    assert limited["status"]["metadata_rows"]<=2048


def test_old_writer_tied_rating_and_floor_deletion_never_recall_superseded_note(tmp_path):
    import sqlite3
    feedback_learning.record(tmp_path,task("ties","Techno synth bass",title="Techno synth bass"),-1,"Superseded note")
    _finish_recall(tmp_path)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        db.execute("INSERT INTO feedback(task_id,generation,kind,rating,title,brief,note,created) SELECT task_id,generation,kind,1,title,brief,'',created FROM feedback WHERE task_id='ties'")
    result=feedback_learning.guidance(tmp_path,"music","Techno synth bass")
    assert "Similar task the user liked:" in result and "Superseded" not in result
    _finish_recall(tmp_path)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        db.execute("DELETE FROM feedback_generations WHERE task_id='ties'")
    assert not feedback_learning.guidance(tmp_path,"music","Techno synth bass")


def test_index_rebuild_is_incremental_readonly_and_cleans_old_epochs_in_batches(tmp_path,monkeypatch):
    import sqlite3
    from unittest.mock import patch
    _bulk_feedback(tmp_path,20)
    _finish_recall(tmp_path)
    connect=sqlite3.connect
    statements=[]
    def observed(*args,**kwargs):
        db=connect(*args,**kwargs);db.set_trace_callback(statements.append);return db
    monkeypatch.setattr(feedback_learning,"STOP",feedback_learning.STOP | {"unused-rebuild-token"})
    with patch.object(feedback_learning.sqlite3,"connect",side_effect=observed):
        assert feedback_learning.guidance(tmp_path,"music","Techno synth bass")
    assert not any(sql.lstrip().upper().startswith(("CREATE","DROP","ALTER","DELETE","INSERT","UPDATE")) for sql in statements)
    first=feedback_learning.maintain(tmp_path)
    assert first["status"] in {"partial","cleanup"}
    with connect(tmp_path / "feedback-learning.sqlite3") as db:
        epoch,frontier=db.execute("SELECT epoch,frontier_id FROM feedback_recall_meta_v1").fetchone()
        assert epoch==2 and frontier<20
    _finish_recall(tmp_path)
    with connect(tmp_path / "feedback-learning.sqlite3") as db:
        assert not db.execute("SELECT 1 FROM feedback_recall_terms_v1 WHERE epoch<2 LIMIT 1").fetchone()
        assert db.execute("SELECT indexed_docs FROM feedback_recall_meta_v1").fetchone()[0]==20


def test_wal_writer_read_snapshot_is_consistent_and_does_not_count_uncommitted_feedback(tmp_path):
    import sqlite3,time
    feedback_learning.record(tmp_path,task("wal","Techno synth bass"),-1,"Committed note")
    _finish_recall(tmp_path)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as setup:
        setup.execute("PRAGMA journal_mode=WAL")
    writer=sqlite3.connect(tmp_path / "feedback-learning.sqlite3")
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE feedback SET note='Uncommitted note'")
        started=time.monotonic();result=feedback_learning.guidance(tmp_path,"music","Techno synth bass");elapsed=time.monotonic()-started
        assert "Committed note" in result and "Uncommitted note" not in result
        assert elapsed<.3
    finally:writer.rollback();writer.close()


def test_read_legacy_floorless_store_never_runs_global_migration(tmp_path,monkeypatch):
    import sqlite3
    feedback_learning.record(tmp_path,task("legacy-ro","Techno synth bass"),-1,"Use electronic drums")
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:db.execute("DROP TABLE feedback_generations")
    monkeypatch.setattr(feedback_learning,"_connect",lambda *a:(_ for _ in ()).throw(AssertionError("global migration")))
    result=feedback_learning.guidance_details(tmp_path,"music","Techno synth bass")
    assert "Use electronic drums" in result["text"] and result["status"]["coverage"]=="legacy_partial"
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='feedback_generations'").fetchone()
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='feedback_recall_meta_v1'").fetchone()


def test_record_forget_and_recall_never_start_a_worker(tmp_path,monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError("unrequested thread")
    monkeypatch.setattr(feedback_learning.threading,"Thread",forbidden)
    ident=feedback_learning.record(tmp_path,task("no-thread","Techno synth bass"),-1,"Use electronic drums")
    feedback_learning.guidance(tmp_path,"music","Techno synth bass")
    assert feedback_learning.forget(tmp_path,ident)



def test_lifecycle_stopping_birth_is_not_adopted_and_later_restart_gets_new_owner(tmp_path,monkeypatch):
    from unittest.mock import Mock
    threads=[]
    class FakeThread:
        def __init__(self,target,**kwargs):self.target=target;threads.append(self)
        def start(self):pass
        def join(self,timeout):pass  # Still exiting after bounded join.
    monkeypatch.setattr(feedback_learning,"_maintenance_slot",None)
    monkeypatch.setattr(feedback_learning,"_maintenance_atexit",False)
    monkeypatch.setattr(feedback_learning.threading,"Thread",FakeThread)
    monkeypatch.setattr(feedback_learning.atexit,"register",Mock())
    first=feedback_learning.start_maintenance(tmp_path)
    assert not feedback_learning.stop_maintenance(None)
    assert not feedback_learning.stop_maintenance(first,timeout=0)
    assert feedback_learning.start_maintenance(tmp_path) is None
    assert len(threads)==1
    threads[0].target()  # Old birth finishes; no DB work because stop was set.
    second=feedback_learning.start_maintenance(tmp_path)
    assert second is not None and second!=first and len(threads)==2
    assert not feedback_learning.stop_maintenance(first)
    feedback_learning.stop_maintenance(second,timeout=0);threads[1].target()
    assert feedback_learning._maintenance_slot is None


def test_optional_worker_maintains_maximum_valid_documents_and_recovers_vm_interrupt(tmp_path,monkeypatch):
    import sqlite3
    brief=" ".join(f"{i:03}" for i in range(1000))
    for i in range(3):feedback_learning.record(tmp_path,task("max-"+str(i),brief,title="t"*200),-1,"n"*2000)
    for _ in range(12):
        result=feedback_learning.maintain(tmp_path)
        assert result["status"]!="unavailable"
        assert result["posting_mutations"]<=feedback_learning.RECALL_BATCH_MUTATIONS
        assert result["text_bytes"]<=feedback_learning.RECALL_BATCH_BYTES
        if result["status"]=="ready":break
    else:raise AssertionError("max-sized valid documents made no bounded progress")
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        assert db.execute("SELECT indexed_docs FROM feedback_recall_meta_v1").fetchone()[0]==3
        db.execute("UPDATE feedback SET note='changed' WHERE task_id='max-0'")
        before=db.execute("SELECT * FROM feedback_recall_changes_v1").fetchall()
    original=feedback_learning._deadline
    def interrupt(conn,seconds):
        conn.set_progress_handler(lambda:1,1)
        return feedback_learning.time.monotonic()+seconds
    monkeypatch.setattr(feedback_learning,"_deadline",interrupt)
    assert feedback_learning.maintain(tmp_path)["status"]=="unavailable"
    monkeypatch.setattr(feedback_learning,"_deadline",original)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        assert db.execute("SELECT * FROM feedback_recall_changes_v1").fetchall()==before
    _finish_recall(tmp_path)


def test_tokenizer_version_normalizes_nested_code_filenames(tmp_path):
    import importlib.util
    from pathlib import Path
    source=Path(feedback_learning.__file__).read_text(encoding="utf-8")
    moved=tmp_path/"moved.py";moved.write_text(source,encoding="utf-8")
    spec=importlib.util.spec_from_file_location("owned_moved_feedback",moved)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module._maintenance_slot is None
    assert module._recall_version()==feedback_learning._recall_version()



def test_union_candidate_truncation_is_explicit_even_without_per_term_truncation(tmp_path):
    with feedback_learning._connect(tmp_path) as db:
        for i in range(360):
            word=("amber","birch","cedar","dahlia","elmwood","firwood")[i//60]
            ident="union-"+str(i)
            db.execute("INSERT INTO feedback_generations VALUES(?,0)",(ident,))
            db.execute("INSERT INTO feedback(task_id,generation,kind,rating,title,brief,note,created) VALUES(?,0,'music',-1,?,?,?,?)",(ident,word,word,"Preference "+str(i),float(i)))
    _finish_recall(tmp_path)
    result=feedback_learning.guidance_details(tmp_path,"music","amber birch cedar dahlia elmwood firwood")
    assert result["status"]["candidate_rows"]==300
    assert result["status"]["limited"] is True
    assert result["status"]["metadata_rows"]<=2048


def test_backfill_includes_zero_negative_and_minimum_sqlite_rowids(tmp_path):
    import sqlite3
    ids=(-(2**63),-1,0)
    with feedback_learning._connect(tmp_path) as db:
        for ident in ids:
            name="legacy-"+str(ident)
            db.execute("INSERT INTO feedback_generations VALUES(?,0)",(name,))
            db.execute("INSERT INTO feedback(id,task_id,generation,kind,rating,title,brief,note,created) VALUES(?,?,0,'music',-1,'amber','amber','Preference',1)",(ident,name))
    _finish_recall(tmp_path)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        epoch,started,complete=db.execute("SELECT epoch,started,complete FROM feedback_recall_meta_v1").fetchone()
        assert started==complete==1
        assert [row[0] for row in db.execute("SELECT feedback_id FROM feedback_recall_docs_v1 WHERE epoch=? ORDER BY feedback_id",(epoch,))]==list(ids)
    assert feedback_learning.guidance_details(tmp_path,"music","amber")["status"]["coverage"]=="indexed_snapshot_ready"



def test_oversized_derived_terms_are_bounded_before_python_decode_and_do_not_erase_feedback(tmp_path):
    import sqlite3
    ident=feedback_learning.record(tmp_path,task("derived-corrupt","Techno synth bass"),-1,"Original note")
    _finish_recall(tmp_path)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        epoch=db.execute("SELECT epoch FROM feedback_recall_meta_v1").fetchone()[0]
        db.execute("INSERT INTO feedback_recall_terms_v1 VALUES(?,?,?,'music',0,1)",(epoch,ident,"x"*300000))
        db.execute("UPDATE feedback SET note='Fresh raw note' WHERE id=?",(ident,))
    assert feedback_learning.maintain(tmp_path)["status"]=="unavailable"
    result=feedback_learning.guidance(tmp_path,"music","Techno synth bass")
    assert "Fresh raw note" in result and "Original note" not in result



def test_restart_resumes_cursor_and_durable_old_id_and_beyond_target_changes(tmp_path):
    import importlib.util,sqlite3
    for i in range(30):
        feedback_learning.record(tmp_path,task("restart-"+str(i),"amber" if i==0 else "birch",title="amber" if i==0 else "birch"),-1,"Original note")
    first=feedback_learning.maintain(tmp_path)
    assert first["status"]=="partial"
    feedback_learning.record(tmp_path,task("restart-0","amber",title="amber"),-1,"Updated behind cursor")
    feedback_learning.record(tmp_path,task("beyond","saffron",title="saffron"),-1,"New beyond target")
    spec=importlib.util.spec_from_file_location("owned_feedback_restart",feedback_learning.__file__)
    restarted=importlib.util.module_from_spec(spec);spec.loader.exec_module(restarted)
    assert restarted._maintenance_slot is None
    for _ in range(100):
        if restarted.maintain(tmp_path)["status"]=="ready":break
    else:raise AssertionError("durable restart backlog did not finish")
    assert "Updated behind cursor" in restarted.guidance(tmp_path,"music","amber")
    assert "New beyond target" in restarted.guidance(tmp_path,"music","saffron")
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        epoch=db.execute("SELECT epoch FROM feedback_recall_meta_v1").fetchone()[0]
        assert db.execute("SELECT count(*) FROM feedback_recall_docs_v1 WHERE epoch=?",(epoch,)).fetchone()[0]==31
        assert not db.execute("SELECT 1 FROM feedback_recall_changes_v1").fetchone()
        assert db.execute("SELECT sum(docs) FROM feedback_recall_df_v1 WHERE epoch=?",(epoch,)).fetchone()[0]==db.execute("SELECT count(*) FROM feedback_recall_terms_v1 WHERE epoch=?",(epoch,)).fetchone()[0]


def test_concurrent_wal_commit_uses_one_snapshot_and_next_recall_sees_new_note(tmp_path,monkeypatch):
    import sqlite3
    feedback_learning.record(tmp_path,task("snapshot","amber",title="amber"),-1,"Snapshot before commit")
    _finish_recall(tmp_path)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:db.execute("PRAGMA journal_mode=WAL")
    original=feedback_learning._doc;changed=[False]
    def concurrent(conn,ident,*args,**kwargs):
        if not changed[0]:
            changed[0]=True
            with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as writer:
                writer.execute("UPDATE feedback SET note='Snapshot after commit'")
        return original(conn,ident,*args,**kwargs)
    monkeypatch.setattr(feedback_learning,"_doc",concurrent)
    first=feedback_learning.guidance(tmp_path,"music","amber")
    assert "Snapshot before commit" in first and "Snapshot after commit" not in first
    second=feedback_learning.guidance(tmp_path,"music","amber")
    assert "Snapshot after commit" in second and "Snapshot before commit" not in second


def test_large_partial_backfill_recalls_old_match_without_claiming_full_coverage(tmp_path):
    with feedback_learning._connect(tmp_path) as db:
        db.execute("INSERT INTO feedback_generations VALUES('rare-old',0)")
        db.execute("INSERT INTO feedback(task_id,generation,kind,rating,title,brief,note,created) VALUES('rare-old',0,'music',-1,'amber','amber','Old rare lesson',0)")
        db.executemany("INSERT INTO feedback_generations VALUES(?,0)",(("scale-"+str(i),) for i in range(9999)))
        db.executemany("INSERT INTO feedback(task_id,generation,kind,rating,title,brief,note,created) VALUES(?,0,'music',-1,'birch','birch','Recent unrelated lesson',?)",(("scale-"+str(i),float(i+1)) for i in range(9999)))
    assert not feedback_learning.guidance(tmp_path,"music","amber")
    batch=feedback_learning.maintain(tmp_path)
    assert batch["status"]=="partial" and batch["rows"]<=feedback_learning.RECALL_BATCH_ROWS
    result=feedback_learning.guidance_details(tmp_path,"music","amber")
    assert "Old rare lesson" in result["text"] and "Recent unrelated lesson" not in result["text"]
    assert result["status"]["coverage"]=="partial"
    assert result["status"]["text_rows"]<=300 and result["status"]["metadata_rows"]<=2048


def test_generation_floor_insert_trigger_makes_current_raw_review_visible_without_stale_authority(tmp_path):
    import sqlite3
    feedback_learning.record(tmp_path,task("insert-floor","amber",title="amber"),-1,"Raw current lesson")
    _finish_recall(tmp_path)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:db.execute("DELETE FROM feedback_generations WHERE task_id='insert-floor'")
    assert not feedback_learning.guidance(tmp_path,"music","amber")
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        db.execute("INSERT INTO feedback_generations VALUES('insert-floor',0)")
        assert db.execute("SELECT 1 FROM feedback_recall_changes_v1 WHERE event='floor'").fetchone()
    result=feedback_learning.guidance_details(tmp_path,"music","amber")
    assert "Raw current lesson" in result["text"] and result["status"]["coverage"]=="partial"



def test_complete_rare_recall_does_not_materialize_unrelated_recent_reviews(tmp_path):
    feedback_learning.record(tmp_path,task("rare", "ambernebula",title="ambernebula"),-1,"Rare lesson")
    for i in range(8):
        feedback_learning.record(tmp_path,task("other-"+str(i),"birch",title="birch"),-1,"Unrelated lesson")
    _finish_recall(tmp_path)
    result=feedback_learning.guidance_details(tmp_path,"music","ambernebula")
    assert "Rare lesson" in result["text"]
    assert result["status"]["text_rows"]==1
    assert result["status"]["fallback_used"] is False
    assert result["status"]["limited"] is False


def test_complete_stopword_and_absent_term_recall_materialize_no_texts(tmp_path):
    feedback_learning.record(tmp_path,task("other","birch",title="birch"),-1,"Unrelated lesson")
    _finish_recall(tmp_path)
    for request in ("the and please with your", "unseenzirconium"):
        result=feedback_learning.guidance_details(tmp_path,"music",request)
        assert result["text"]==""
        assert result["status"]["text_rows"]==0
        assert result["status"]["fallback_used"] is False
        assert result["status"]["limited"] is False


def test_partial_recall_still_materializes_recent_fallback(tmp_path):
    feedback_learning.record(tmp_path,task("old","amber",title="amber"),-1,"Indexed lesson")
    _finish_recall(tmp_path)
    feedback_learning.record(tmp_path,task("new","saffron",title="saffron"),-1,"New raw lesson")
    result=feedback_learning.guidance_details(tmp_path,"music","saffron")
    assert "New raw lesson" in result["text"]
    assert result["status"]["coverage"]=="partial"
    assert result["status"]["fallback_used"] is True


def test_capped_postings_still_materialize_recent_fallback(tmp_path):
    for i in range(70):
        feedback_learning.record(tmp_path,task("common-"+str(i),"amber",title="amber"),-1,"Lesson "+str(i))
    _finish_recall(tmp_path)
    result=feedback_learning.guidance_details(tmp_path,"music","amber")
    assert result["status"]["limited"] is True
    assert result["status"]["fallback_used"] is True
    assert result["status"]["candidate_rows"]==70


def test_indexed_hash_drop_marks_partial_and_uses_fresh_raw_fallback(tmp_path):
    import sqlite3
    ident=feedback_learning.record(tmp_path,task("hash-drop","amber",title="amber"),-1,"Raw authoritative lesson")
    _finish_recall(tmp_path)
    with sqlite3.connect(tmp_path / "feedback-learning.sqlite3") as db:
        db.execute("UPDATE feedback_recall_docs_v1 SET source_hash=? WHERE feedback_id=?",("0"*64,ident))
    result=feedback_learning.guidance_details(tmp_path,"music","amber")
    assert "Raw authoritative lesson" in result["text"]
    assert result["status"]["coverage"]=="partial"
    assert result["status"]["fallback_used"] is True


def test_legacy_floor_transition_is_durable_before_next_maintenance(tmp_path):
    import sqlite3
    from contextlib import closing
    import pytest
    # A genuine pre-floor schema; no historical floor/tombstone is guessed.
    with feedback_learning._connect(tmp_path) as db:
        db.execute("DROP TABLE feedback_generations")
        db.execute("INSERT INTO feedback(task_id,generation,kind,rating,title,brief,note,created) VALUES('legacy',0,'music',-1,'Amber melody','Amber melody','Old generation',1)")
    _finish_recall(tmp_path)
    with feedback_learning._recall_read(tmp_path) as db:
        assert db.execute("SELECT floor_mode FROM feedback_recall_meta_v1").fetchone()[0] == 0
    latest = feedback_learning.record(tmp_path, dict(task("legacy", "Amber melody", title="Amber melody"), generation=1), 1)
    assert feedback_learning.forget(tmp_path, latest)
    assert not feedback_learning.guidance(tmp_path, "music", "Amber melody")
    with closing(sqlite3.connect(tmp_path / "feedback-learning.sqlite3")) as db, db:
        assert tuple(db.execute("SELECT floor_mode,complete FROM feedback_recall_meta_v1").fetchone()) == (1,0)
        db.execute("DROP TABLE feedback_generations")
    with pytest.raises(sqlite3.DatabaseError, match="generation floors disappeared"):
        feedback_learning.record(tmp_path, task("unrelated", "Piano sonata"), 1)
    assert not feedback_learning.guidance(tmp_path, "music", "Amber melody")


def test_observed_legacy_floors_stamp_rolls_back_and_rebuilds_normally(tmp_path):
    import sqlite3
    from contextlib import closing
    import pytest
    feedback_learning.record(tmp_path, task("legacy", "Amber melody"), 1)
    _finish_recall(tmp_path)
    with closing(sqlite3.connect(tmp_path / "feedback-learning.sqlite3")) as db, db:
        # Simulate the supported pre-upgrade window: floors already exist,
        # but derived metadata still records its earlier legacy snapshot.
        db.execute("UPDATE feedback_recall_meta_v1 SET floor_mode=0,complete=1")
        for operation in ("insert","update","delete"):
            db.execute("DROP TRIGGER feedback_recall_floor_"+operation+"_v1")
    with pytest.raises(RuntimeError, match="rollback"):
        with feedback_learning._connect(tmp_path) as db:
            assert tuple(db.execute("SELECT floor_mode,complete FROM feedback_recall_meta_v1").fetchone()) == (1,0)
            raise RuntimeError("rollback")
    with feedback_learning._recall_read(tmp_path) as db:
        assert tuple(db.execute("SELECT floor_mode,complete FROM feedback_recall_meta_v1").fetchone()) == (0,1)
    feedback_learning.record(tmp_path, task("current", "Piano sonata"), 1)
    with feedback_learning._recall_read(tmp_path) as db:
        assert tuple(db.execute("SELECT floor_mode,complete FROM feedback_recall_meta_v1").fetchone()) == (1,0)
        assert not feedback_learning._trigger_match(db, True)
    _finish_recall(tmp_path)
    with feedback_learning._recall_read(tmp_path) as db:
        assert feedback_learning._trigger_match(db, True)
        assert db.execute("SELECT complete FROM feedback_recall_meta_v1").fetchone()[0] == 1
