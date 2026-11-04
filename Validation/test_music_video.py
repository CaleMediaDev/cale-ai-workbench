"""Verify that local music gets a playable video without changing the song."""

import asyncio
import json
import math
import struct
import subprocess
import wave

import imageio_ffmpeg
import pytest
from PIL import Image, ImageChops

import music_video
import toolbox_mcp


def test_music_video_rejects_dangling_output_symlink(tmp_path, monkeypatch):
    song = tmp_path / "song.wav"
    song.write_bytes(b"source audio")
    video = tmp_path / "song-video.mp4"
    target = tmp_path / "missing-target.mp4"
    try:
        video.symlink_to(target.name)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symlinks is unavailable on this platform")

    def unexpected_run(*args, **kwargs):
        pytest.fail("An existing output symlink must be rejected before rendering")

    monkeypatch.setattr(music_video.subprocess, "run", unexpected_run)
    with pytest.raises(ValueError, match="new MP4"):
        music_video.make_music_video(str(song), str(video))

    assert video.is_symlink()
    assert video.readlink() == type(video)(target.name)
    assert not target.exists()
    assert song.read_bytes() == b"source audio"
    assert not list(tmp_path.glob("cale-music-video-*"))


def test_music_video_timeout_removes_partial_output_and_allows_retry(tmp_path, monkeypatch):
    song = tmp_path / "song.wav"
    video = tmp_path / "song-video.mp4"
    with wave.open(str(song), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    original = song.read_bytes()
    calls = []
    monkeypatch.setattr(music_video.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")

    def fake_run(command, **kwargs):
        rendered = type(video)(command[-1])
        assert not rendered.exists()
        assert not video.exists()
        calls.append(command)
        if len(calls) == 1:
            rendered.write_bytes(b"incomplete MP4")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        rendered.write_bytes(b"completed MP4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(music_video.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        music_video.make_music_video(str(song), str(video))
    assert not video.exists()
    assert song.read_bytes() == original

    result = music_video.make_music_video(str(song), str(video))
    assert result["path"] == str(video)
    assert video.read_bytes() == b"completed MP4"
    assert len(calls) == 2
    assert song.read_bytes() == original


def test_music_video_interruption_removes_partial_output_and_allows_retry(tmp_path, monkeypatch):
    song = tmp_path / "song.wav"
    video = tmp_path / "song-video.mp4"
    with wave.open(str(song), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    original = song.read_bytes()
    calls = []
    monkeypatch.setattr(music_video.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")

    def fake_run(command, **kwargs):
        rendered = type(video)(command[-1])
        assert not rendered.exists()
        assert not video.exists()
        calls.append(command)
        if len(calls) == 1:
            rendered.write_bytes(b"interrupted MP4")
            raise KeyboardInterrupt()
        rendered.write_bytes(b"completed MP4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(music_video.subprocess, "run", fake_run)
    with pytest.raises(KeyboardInterrupt):
        music_video.make_music_video(str(song), str(video))
    assert not video.exists()
    assert song.read_bytes() == original

    result = music_video.make_music_video(str(song), str(video))
    assert result["path"] == str(video)
    assert video.read_bytes() == b"completed MP4"
    assert len(calls) == 2
    assert song.read_bytes() == original


def test_music_video_empty_output_is_removed_and_allows_retry(tmp_path, monkeypatch):
    song = tmp_path / "song.wav"
    video = tmp_path / "song-video.mp4"
    with wave.open(str(song), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    original = song.read_bytes()
    calls = []
    monkeypatch.setattr(music_video.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")

    def fake_run(command, **kwargs):
        rendered = type(video)(command[-1])
        assert not rendered.exists()
        assert not video.exists()
        calls.append(command)
        rendered.write_bytes(b"" if len(calls) == 1 else b"completed MP4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(music_video.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Local music video failed"):
        music_video.make_music_video(str(song), str(video))
    assert not video.exists()

    result = music_video.make_music_video(str(song), str(video))
    assert result["path"] == str(video)
    assert video.read_bytes() == b"completed MP4"
    assert len(calls) == 2
    assert song.read_bytes() == original


def test_music_video_invalid_diagnostic_bytes_allow_cleanup_and_retry(tmp_path, monkeypatch):
    song = tmp_path / "song.wav"
    video = tmp_path / "song-video.mp4"
    with wave.open(str(song), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    original = song.read_bytes()
    calls = []
    monkeypatch.setattr(music_video.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")

    def fake_run(command, **kwargs):
        rendered = type(video)(command[-1])
        assert not rendered.exists()
        assert not video.exists()
        calls.append(command)
        if len(calls) == 1:
            rendered.write_bytes(b"incomplete MP4")
            # Model subprocess text decoding of non-UTF-8 FFmpeg diagnostics.
            stderr = b"Encoder failed: \xff".decode(
                "utf-8", errors=kwargs.get("errors", "strict")
            )
            return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)
        rendered.write_bytes(b"completed MP4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(music_video.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Encoder failed:"):
        music_video.make_music_video(str(song), str(video))
    assert not video.exists()

    result = music_video.make_music_video(str(song), str(video))
    assert result["path"] == str(video)
    assert video.read_bytes() == b"completed MP4"
    assert len(calls) == 2
    assert song.read_bytes() == original


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_music_video_preserves_concurrently_created_destination(tmp_path, monkeypatch, outcome):
    song = tmp_path / "song.wav"
    song.write_bytes(b"source audio")
    video = tmp_path / "song-video.mp4"
    renders = []
    monkeypatch.setattr(music_video.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")

    def fake_run(command, **kwargs):
        rendered = type(video)(command[-1])
        renders.append(rendered)
        # Another process creates the requested destination after validation.
        video.write_bytes(b"another process's video")
        if rendered != video:
            rendered.write_bytes(b"our render")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(
            command, 1 if outcome == "failure" else 0,
            stdout="", stderr="encoder failed" if outcome == "failure" else "",
        )

    monkeypatch.setattr(music_video.subprocess, "run", fake_run)
    error = {"success": FileExistsError, "failure": RuntimeError,
             "timeout": subprocess.TimeoutExpired}[outcome]
    with pytest.raises(error):
        music_video.make_music_video(str(song), str(video))
    assert video.read_bytes() == b"another process's video"
    assert song.read_bytes() == b"source audio"
    assert renders and renders[0] != video
    assert not renders[0].exists()
    assert not list(tmp_path.glob("cale-music-video-*"))


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


def test_music_video_uses_supplied_cover_art_without_changing_it(tmp_path):
    song = tmp_path / "song.wav"
    cover = tmp_path / "cover.png"
    video = tmp_path / "cover-video.mp4"
    frame = tmp_path / "frame.png"
    with wave.open(str(song), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"".join(
            struct.pack("<h", round(5000 * math.sin(2 * math.pi * 440 * n / 16000)))
            for n in range(16000)
        ))
    Image.new("RGB", (320, 240), (220, 20, 20)).save(cover)
    original_song, original_cover = song.read_bytes(), cover.read_bytes()
    schema = toolbox_mcp.toolbox_schema("local.make_music_video")
    assert "background_image" in schema["inputSchema"]["properties"]
    result = asyncio.run(toolbox_mcp.toolbox_call("local.make_music_video", json.dumps({
        "source_path": str(song), "output_path": str(video),
        "layout": "vertical", "background_image": str(cover),
    })))
    assert result["background_image"] == str(cover)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(video),
                    "-frames:v", "1", str(frame)], check=True, capture_output=True)
    with Image.open(frame) as image:
        assert image.size == (720, 1280)
        red, green, blue = image.convert("RGB").getpixel((20, 20))
        assert red > green * 2 and red > blue * 2
    assert song.read_bytes() == original_song
    assert cover.read_bytes() == original_cover
    with pytest.raises(ValueError, match="background image"):
        music_video.make_music_video(str(song), str(tmp_path / "bad.mp4"),
                                     background_image=str(song))


def test_music_video_burns_timed_srt_captions_without_changing_inputs(tmp_path):
    song = tmp_path / "song.wav"
    captions = tmp_path / "lyrics.srt"
    plain = tmp_path / "plain.mp4"
    captioned = tmp_path / "captioned.mp4"
    with wave.open(str(song), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"".join(
            struct.pack("<h", round(5000 * math.sin(2 * math.pi * 440 * n / 16000)))
            for n in range(16000)
        ))
    captions.write_text("1\n00:00:00,000 --> 00:00:01,000\nHELLO CALE\n\n", encoding="utf-8")
    original_song, original_captions = song.read_bytes(), captions.read_bytes()
    schema = toolbox_mcp.toolbox_schema("local.make_music_video")
    assert "captions_path" in schema["inputSchema"]["properties"]
    music_video.make_music_video(str(song), str(plain))
    result = asyncio.run(toolbox_mcp.toolbox_call("local.make_music_video", json.dumps({
        "source_path": str(song), "output_path": str(captioned),
        "captions_path": str(captions),
    })))
    assert result["captions_path"] == str(captions)
    frames = []
    for video, name in ((plain, "plain.png"), (captioned, "captioned.png")):
        frame = tmp_path / name
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-ss", "0.5", "-i", str(video),
                        "-frames:v", "1", str(frame)], check=True, capture_output=True)
        with Image.open(frame) as image:
            frames.append(image.convert("RGB"))
    difference = ImageChops.difference(*frames)
    assert difference.getbbox() is not None
    assert sum(1 for pixel in difference.crop((0, 500, 1280, 720)).get_flattened_data()
               if max(pixel) > 60) > 100
    assert song.read_bytes() == original_song
    assert captions.read_bytes() == original_captions
    with pytest.raises(ValueError, match="SRT"):
        music_video.make_music_video(str(song), str(tmp_path / "bad.mp4"),
                                     captions_path=str(song))



def test_branded_song_video_preserves_art_audio_and_reuses_export(tmp_path):
    import base64
    import hashlib
    import io

    import song_video

    source = tmp_path / "test song.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(struct.pack("<h", 1000) * 16000)
    original = source.read_bytes()
    artwork = base64.b64decode(song_video.BRANDED_COVER_BASE64)
    assert hashlib.sha256(artwork).hexdigest() == song_video.BRANDED_COVER_SHA256
    result = song_video.render_branded(str(source))
    video = result["path"]
    assert result["reused"] is False
    assert song_video.render_branded(str(source))["reused"] is True
    assert len(list(tmp_path.glob("*.mp4"))) == 1
    frame = tmp_path / "frame.png"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", video,
                    "-frames:v", "1", str(frame)], check=True, capture_output=True)
    with Image.open(frame) as actual, Image.open(io.BytesIO(artwork)) as reference:
        assert actual.size == (1024, 1024)
        reference = reference.convert("RGB").resize((1024, 1024))
        diff = ImageChops.difference(actual.convert("RGB"), reference)
        from PIL import ImageStat
        assert max(ImageStat.Stat(diff).mean) < 12
    probe = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", video],
                           capture_output=True, text=True)
    assert "Video: h264" in probe.stderr and "Audio: aac" in probe.stderr
    assert source.read_bytes() == original
    # Changed audio cannot reuse a stale video, even if its timestamp is restored.
    with source.open("ab") as stream:
        stream.write(b"new metadata")
    changed = song_video.render_branded(str(source))
    assert changed["path"] != video
    assert song_video.render_branded(str(source))["path"] == changed["path"]
