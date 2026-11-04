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


def test_review_requires_comment_for_thumbs_down(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="what to improve"):
        feedback_learning.record(tmp_path, task("song-3", "Music"), -1, "")
