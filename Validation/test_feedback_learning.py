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


def test_review_requires_comment_for_thumbs_down(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="what to improve"):
        feedback_learning.record(tmp_path, task("song-3", "Music"), -1, "")
