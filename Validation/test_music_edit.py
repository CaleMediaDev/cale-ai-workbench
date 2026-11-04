"""A music edit must produce playable audio without replacing its inputs."""

import asyncio
import json
import math
import struct
import wave
from pathlib import Path

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


@pytest.mark.parametrize("parameter", [
    "start_seconds", "duration_seconds", "fade_in_seconds", "fade_out_seconds",
])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_edit_music_rejects_nonfinite_timings(tmp_path, monkeypatch, parameter, value):
    source = tmp_path / "song.wav"
    output = tmp_path / "clip.wav"
    tone(source)
    original = source.read_bytes()
    calls = []
    monkeypatch.setattr(music_edit, "_run", lambda *args: calls.append(args))
    options = {"duration_seconds": 0.5, parameter: value}

    with pytest.raises(ValueError, match="finite"):
        music_edit.edit_music(str(source), str(output), **options)

    assert calls == []
    assert not output.exists()
    assert source.read_bytes() == original


def test_edit_music_preserves_fade_timing_precision(tmp_path, monkeypatch):
    source = tmp_path / "song.wav"
    output = tmp_path / "clip.wav"
    original = b"original audio"
    source.write_bytes(original)
    calls = []
    monkeypatch.setattr(music_edit, "_run", lambda *args: calls.append(args))
    clip_duration = 10000.0
    fade_in = 0.123456789
    fade_out = 0.001

    music_edit.edit_music(
        str(source), str(output), duration_seconds=clip_duration,
        fade_in_seconds=fade_in, fade_out_seconds=fade_out,
    )

    assert len(calls) == 1
    arguments, destination = calls[0]
    filters = arguments[arguments.index("-af") + 1].split(",")
    fade_fields = [dict(field.split("=", 1) for field in item.split(":")[1:])
                   for item in filters]
    assert float(fade_fields[0]["d"]) == fade_in
    assert float(fade_fields[1]["d"]) == fade_out
    assert float(fade_fields[1]["st"]) == clip_duration - fade_out
    assert float(fade_fields[1]["st"]) < clip_duration
    assert destination == output
    assert not output.exists()
    assert source.read_bytes() == original


@pytest.mark.parametrize("gain", [-59.99999, 12.3456789])
def test_music_filters_preserve_gain_precision(tmp_path, monkeypatch, gain):
    source = tmp_path / "song.wav"
    foreground = tmp_path / "voice.wav"
    output = tmp_path / "clip.wav"
    original = b"original audio"
    source.write_bytes(original)
    foreground.write_bytes(original)
    calls = []
    monkeypatch.setattr(music_edit, "_run", lambda *args: calls.append(args))

    music_edit.edit_music(str(source), str(output), gain_db=gain)
    music_edit.mix_music(
        str(source), str(foreground), str(output),
        background_gain_db=gain, foreground_gain_db=gain,
    )

    assert len(calls) == 2
    edit_args, edit_output = calls[0]
    edit_filter = edit_args[edit_args.index("-af") + 1]
    assert float(edit_filter.split("=", 1)[1].removesuffix("dB")) == gain
    mix_args, mix_output = calls[1]
    graph = mix_args[mix_args.index("-filter_complex") + 1]
    gain_fields = graph.split("volume=")[1:]
    assert len(gain_fields) == 2
    assert all(float(field.split("dB", 1)[0]) == gain for field in gain_fields)
    assert edit_output == mix_output == output
    assert not output.exists()
    assert source.read_bytes() == original
    assert foreground.read_bytes() == original


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


def test_edit_music_removes_empty_output_after_successful_exit(tmp_path, monkeypatch):
    source = tmp_path / "song.wav"
    output = tmp_path / "clip.wav"
    original = b"original audio left untouched by the mocked subprocess"
    source.write_bytes(original)
    calls = []
    monkeypatch.setattr(music_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")

    def empty_success(command, **kwargs):
        calls.append(command)
        assert "-n" in command
        assert kwargs["timeout"] == 3600
        assert source.read_bytes() == original
        Path(command[-1]).write_bytes(b"")
        return music_edit.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(music_edit.subprocess, "run", empty_success)

    # A failed validation must not leave an empty file blocking a retry.
    for _ in range(2):
        with pytest.raises(RuntimeError, match="Local audio edit failed"):
            music_edit.edit_music(str(source), str(output))
        assert not output.exists()
        assert source.read_bytes() == original

    assert len(calls) == 2


def test_edit_music_rejects_header_only_wav_after_successful_exit(tmp_path, monkeypatch):
    source = tmp_path / "song.wav"
    output = tmp_path / "clip.wav"
    original = b"original audio"
    source.write_bytes(original)
    staged_paths = []
    monkeypatch.setattr(music_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")

    def header_only_success(command, **kwargs):
        assert kwargs["timeout"] == 3600
        assert "-n" in command
        staged = Path(command[-1])
        staged_paths.append(staged)
        with wave.open(str(staged), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"")
        assert staged.stat().st_size > 0
        return music_edit.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(music_edit.subprocess, "run", header_only_success)

    # A seek beyond EOF can succeed while producing only a WAV header.
    for _ in range(2):
        with pytest.raises(RuntimeError, match="no audio frames"):
            music_edit.edit_music(str(source), str(output), start_seconds=100)
        assert not output.exists()
        assert source.read_bytes() == original
        assert all(not path.parent.exists() for path in staged_paths)

    assert len(staged_paths) == 2


def test_edit_music_rejects_zero_sample_rate_wav(tmp_path, monkeypatch):
    source = tmp_path / "song.wav"
    output = tmp_path / "clip.wav"
    original = b"original audio"
    source.write_bytes(original)
    staged_paths = []
    monkeypatch.setattr(music_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")

    def invalid_rate_success(command, **kwargs):
        assert kwargs["timeout"] == 3600
        assert "-n" in command
        staged = Path(command[-1])
        staged_paths.append(staged)
        with wave.open(str(staged), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 160)
        data = bytearray(staged.read_bytes())
        # The PCM fmt chunk stores sample rate and byte rate at offsets 24 and 28.
        struct.pack_into("<II", data, 24, 0, 0)
        staged.write_bytes(data)
        with wave.open(str(staged), "rb") as audio:
            assert audio.getframerate() == 0
            assert audio.getnframes() == 160
        return music_edit.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(music_edit.subprocess, "run", invalid_rate_success)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="invalid WAV sample rate"):
            music_edit.edit_music(str(source), str(output))
        assert not output.exists()
        assert source.read_bytes() == original
        assert all(not path.parent.exists() for path in staged_paths)

    assert len(staged_paths) == 2


@pytest.mark.parametrize("missing_bytes", [1, 320])
def test_edit_music_rejects_truncated_wav_after_successful_exit(tmp_path, monkeypatch, missing_bytes):
    source = tmp_path / "song.wav"
    output = tmp_path / "clip.wav"
    original = b"original audio"
    source.write_bytes(original)
    staged_paths = []
    monkeypatch.setattr(music_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")

    def truncated_success(command, **kwargs):
        assert kwargs["timeout"] == 3600
        assert "-n" in command
        staged = Path(command[-1])
        staged_paths.append(staged)
        with wave.open(str(staged), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 160)
        staged.write_bytes(staged.read_bytes()[:-missing_bytes])
        # The header still declares frames even when their data is missing.
        with wave.open(str(staged), "rb") as audio:
            assert audio.getnframes() == 160
        return music_edit.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(music_edit.subprocess, "run", truncated_success)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="truncated WAV"):
            music_edit.edit_music(str(source), str(output))
        assert not output.exists()
        assert source.read_bytes() == original
        assert all(not path.parent.exists() for path in staged_paths)

    assert len(staged_paths) == 2


@pytest.mark.parametrize("channels", [1, 2])
def test_edit_music_rejects_incomplete_final_wav_frame(tmp_path, monkeypatch, channels):
    source = tmp_path / "song.wav"
    output = tmp_path / "clip.wav"
    original = b"original audio"
    source.write_bytes(original)
    staged_paths = []
    monkeypatch.setattr(music_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")

    def incomplete_success(command, **kwargs):
        assert kwargs["timeout"] == 3600
        assert "-n" in command
        staged = Path(command[-1])
        staged_paths.append(staged)
        frame_size = channels * 2
        with wave.open(str(staged), "wb") as audio:
            audio.setnchannels(channels)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00" * (160 * frame_size + 1))
        # The frame count hides the extra byte declared in the data chunk.
        with wave.open(str(staged), "rb") as audio:
            assert audio.getnframes() == 160
            assert len(audio.readframes(160)) == 160 * frame_size
            assert audio.readframes(1) == b"\x00"
        return music_edit.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(music_edit.subprocess, "run", incomplete_success)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="incomplete WAV frame"):
            music_edit.edit_music(str(source), str(output))
        assert not output.exists()
        assert source.read_bytes() == original
        assert all(not path.parent.exists() for path in staged_paths)

    assert len(staged_paths) == 2


def test_edit_music_timeout_discards_partial_audio_and_allows_retry(tmp_path, monkeypatch):
    source = tmp_path / "song.wav"
    output = tmp_path / "clip.wav"
    original = b"original audio"
    source.write_bytes(original)
    staged_paths = []
    monkeypatch.setattr(music_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")

    def timeout_then_success(command, **kwargs):
        assert "-n" in command
        assert kwargs["timeout"] == 3600
        staged = Path(command[-1])
        staged_paths.append(staged)
        assert staged != output
        assert not output.exists()
        staged.write_bytes(b"partial audio")
        if len(staged_paths) == 1:
            raise music_edit.subprocess.TimeoutExpired(command, kwargs["timeout"])
        with wave.open(str(staged), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 160)
        return music_edit.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(music_edit.subprocess, "run", timeout_then_success)

    with pytest.raises(music_edit.subprocess.TimeoutExpired):
        music_edit.edit_music(str(source), str(output))

    assert not output.exists()
    assert not staged_paths[0].parent.exists()
    assert source.read_bytes() == original

    music_edit.edit_music(str(source), str(output))
    assert duration(output) == pytest.approx(0.01)
    assert source.read_bytes() == original
    assert len(staged_paths) == 2
    assert all(not path.parent.exists() for path in staged_paths)


def test_edit_music_preserves_destination_created_during_render(tmp_path, monkeypatch):
    output = tmp_path / "clip.wav"
    monkeypatch.setattr(music_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")

    def successful_render(command, **kwargs):
        with wave.open(str(command[-1]), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 160)
        output.write_bytes(b"concurrently created audio")
        return music_edit.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(music_edit.subprocess, "run", successful_render)

    with pytest.raises(FileExistsError):
        music_edit._run([], output)

    assert output.read_bytes() == b"concurrently created audio"
    assert list(tmp_path.iterdir()) == [output]


def test_mix_music_refuses_to_replace_source(tmp_path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    tone(first)
    tone(second)
    with pytest.raises(ValueError, match="new output"):
        music_edit.mix_music(str(first), str(second), str(first))
