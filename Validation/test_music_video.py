"""Verify that local music gets a playable video without changing the song."""

import asyncio
import json
import math
import struct
import subprocess
import wave

import imageio_ffmpeg
import pytest

import toolbox_mcp


def test_music_video_renders_offline_and_preserves_source(tmp_path):
    song = tmp_path / "song.wav"
    video = tmp_path / "song-video.mp4"
    with wave.open(str(song), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"".join(
            struct.pack("<h", round(5000 * math.sin(2 * math.pi * 440 * n / 16000)))
            for n in range(16000)
        ))
    original = song.read_bytes()
    names = [item["name"] for item in toolbox_mcp.toolbox_search("make music video visualizer")["tools"]]
    assert "local.make_music_video" in names
    result = asyncio.run(toolbox_mcp.toolbox_call("local.make_music_video", json.dumps({
        "source_path": str(song), "output_path": str(video), "layout": "square",
    })))
    assert result["path"] == str(video)
    assert video.stat().st_size > 1000
    probe = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(video)],
                           capture_output=True, text=True)
    assert "Video: h264" in probe.stderr
    assert "Audio: aac" in probe.stderr
    assert "720x720" in probe.stderr
    assert song.read_bytes() == original
    with pytest.raises(ValueError, match="new MP4"):
        asyncio.run(toolbox_mcp.toolbox_call("local.make_music_video", json.dumps({
            "source_path": str(song), "output_path": str(video),
        })))
