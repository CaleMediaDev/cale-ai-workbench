"""A finished song should include a local video and truthful captions."""

import math
import struct
import wave
from pathlib import Path

import song_video


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
