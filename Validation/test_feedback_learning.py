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
