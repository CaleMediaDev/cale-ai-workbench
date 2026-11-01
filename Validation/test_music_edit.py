"""A music edit must produce playable audio without replacing its inputs."""

import asyncio
import json
import math
import struct
import wave

import pytest

import music_edit
import toolbox_mcp


def tone(path, seconds=1.0, frequency=440):
    rate = 16000
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"".join(
            struct.pack("<h", round(5000 * math.sin(2 * math.pi * frequency * n / rate)))
            for n in range(round(rate * seconds))
        ))


def duration(path):
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def test_edit_music_renders_playable_trimmed_audio(tmp_path):
    source = tmp_path / "song.wav"
    output = tmp_path / "clip.wav"
    tone(source, 1.2)
    original = source.read_bytes()
    result = music_edit.edit_music(str(source), str(output), 0.2, 0.5, 0.1, 0.1)
    assert result["path"] == str(output)
    assert 0.45 <= duration(output) <= 0.55
    assert source.read_bytes() == original
    with pytest.raises(ValueError, match="new output"):
        music_edit.edit_music(str(source), str(output))


def test_mix_music_renders_both_tracks_and_is_discoverable(tmp_path):
    background = tmp_path / "music.wav"
    foreground = tmp_path / "voice.wav"
    output = tmp_path / "mix.wav"
    tone(background, 0.8, 440)
    tone(foreground, 0.3, 880)
    names = [item["name"] for item in toolbox_mcp.toolbox_search("mix narration with background music")["tools"]]
    assert "local.mix_music" in names
    assert "local.edit_music" in [item["name"] for item in toolbox_mcp.toolbox_search("trim fade song")["tools"]]
    result = asyncio.run(toolbox_mcp.toolbox_call("local.mix_music", json.dumps({
        "background_path": str(background), "foreground_path": str(foreground),
        "output_path": str(output), "foreground_start_seconds": 0.2,
    })))
    assert result["path"] == str(output)
    assert 0.75 <= duration(output) <= 0.85
    assert duration(background) == pytest.approx(0.8)
    assert duration(foreground) == pytest.approx(0.3)


def test_mix_music_refuses_to_replace_source(tmp_path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    tone(first)
    tone(second)
    with pytest.raises(ValueError, match="new output"):
        music_edit.mix_music(str(first), str(second), str(first))
