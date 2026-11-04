"""A finished song should include a local video and truthful captions."""

import math
import os
import struct
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

import song_video


@pytest.mark.parametrize("start,end", [
    (0, float("inf")), (0, "Infinity"), (0, 1e308),
    (0, 10**400), (10**400, 1), (0.0001, 0.0002),
])
def test_invalid_transcription_times_use_authored_caption_fallback(start, end):
    rows, source = song_video.caption_rows(
        [{"start": start, "end": end, "text": "unusable timing"}],
        "Authored line", "Song", False, 10,
    )
    assert rows == [(0.0, 10.0, "Authored line")]
    assert source == "estimated_from_authored_lyrics"


def test_invalid_timing_does_not_discard_valid_transcription():
    rows, source = song_video.caption_rows(
        [{"start": 0, "end": float("inf"), "text": "invalid"},
         {"start": 1, "end": 2, "text": "Actual words"}],
        "Authored line", "Song", False, 10,
    )
    assert rows == [(1.0, 2.0, "Actual words")]
    assert source == "offline_transcription"


def test_caption_rows_use_real_word_timing_and_do_not_invent_instrumental_lyrics():
    rows, source = song_video.caption_rows(
        [{"start": 1.2, "end": 2.4, "text": "  Hello  Cale "}],
        "authored words", "Beach song", False, 10,
    )
    assert rows == [(1.2, 2.4, "Hello Cale")]
    assert source == "offline_transcription"
    rows, source = song_video.caption_rows([], "words that are not sung", "Beach song", True, 10)
    assert rows == [(0.0, 7.0, "Instrumental — Beach song")]
    assert source == "title_only"


def test_song_render_creates_mp4_and_editable_srt_without_changing_audio(tmp_path):
    song = tmp_path / "beach.wav"
    with wave.open(str(song), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"".join(
            struct.pack("<h", round(5000 * math.sin(2 * math.pi * 440 * n / 16000)))
            for n in range(16000)
        ))
    original = song.read_bytes()
    result = song_video.render(str(song), segments=[
        {"start": 0.1, "end": 0.9, "text": "Hello Cale"}
    ], title="Beach song", duration=1)
    assert result["caption_source"] == "offline_transcription"
    assert Path(result["path"]).stat().st_size > 1000
    assert "00:00:00,100 --> 00:00:00,900\nHello Cale" in Path(
        result["captions_path"]
    ).read_text(encoding="utf-8")
    assert song.read_bytes() == original


def test_interrupted_video_is_not_claimed_ready_or_overwritten(tmp_path):
    song = tmp_path / "song.wav"
    with wave.open(str(song), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 16000)
    partial = tmp_path / "song-video.mp4"
    partial.write_bytes(b"not an MP4" * 200)
    (tmp_path / "song.captions.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nSong\n", encoding="utf-8"
    )
    result = song_video.render(str(song), title="Song", instrumental=True, duration=1)
    assert Path(result["path"]).name == "song-video-2.mp4"
    assert song_video._video_playable(Path(result["path"]))
    assert partial.read_bytes() == b"not an MP4" * 200


def test_changed_song_and_edited_captions_get_fresh_videos(tmp_path):
    song = tmp_path / "song[final].wav"

    def write_song(frequency):
        with wave.open(str(song), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"".join(
                struct.pack("<h", round(5000 * math.sin(2 * math.pi * frequency * n / 16000)))
                for n in range(16000)
            ))

    write_song(440)
    first = song_video.render(str(song), segments=[
        {"start": 0.1, "end": 0.9, "text": "First version"}
    ], duration=1)
    first_video = Path(first["path"])
    first_captions = Path(first["captions_path"])
    old_video = first_video.read_bytes()
    old_captions = first_captions.read_bytes()
    original_time = song.stat()
    write_song(660)
    os.utime(song, ns=(original_time.st_atime_ns, original_time.st_mtime_ns))

    with patch.object(song_video.music_video, "make_music_video",
                      side_effect=RuntimeError("encoder busy")):
        with pytest.raises(RuntimeError, match="encoder busy"):
            song_video.render(str(song), segments=[
                {"start": 0.1, "end": 0.9, "text": "Second version"}
            ], duration=1)
    caption_count = sum(path.name.startswith(song.stem + ".captions-")
                        for path in tmp_path.iterdir())
    second = song_video.render(str(song), segments=[
        {"start": 0.1, "end": 0.9, "text": "Second version"}
    ], duration=1)
    assert sum(path.name.startswith(song.stem + ".captions-")
               for path in tmp_path.iterdir()) == caption_count
    assert second["path"] != first["path"]
    assert second["captions_path"] != first["captions_path"]
    assert "Second version" in Path(second["captions_path"]).read_text()
    assert first_video.read_bytes() == old_video
    assert first_captions.read_bytes() == old_captions
    assert song_video.render(str(song), duration=1)["path"] == second["path"]

    Path(second["captions_path"]).write_text(
        "1\n00:00:00,100 --> 00:00:00,900\nEdited by user\n", encoding="utf-8"
    )
    third = song_video.render(str(song), duration=1)
    assert third["path"] not in {first["path"], second["path"]}
    assert third["captions_path"] == second["captions_path"]
    assert song_video._video_playable(Path(third["path"]))
