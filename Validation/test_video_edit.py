"""Render short real clips to verify the offline agent video tools."""

import asyncio
import json
import subprocess
import sys

import imageio_ffmpeg
import pytest

import toolbox_mcp
import video_edit


def test_replacing_soundtrack_preserves_compatible_video_packets(tmp_path):
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    source, audio, output = (tmp_path / name for name in ("source.mp4", "soundtrack.wav", "voiced.mp4"))
    subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "testsrc2=s=160x90:r=10:d=0.5", "-c:v", "libx264", "-crf", "30", "-preset", "medium", str(source)],
        check=True, capture_output=True, timeout=30)
    subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "sine=frequency=600:duration=1", str(audio)], check=True, capture_output=True, timeout=30)

    def video_hash(path):
        return subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-map", "0:v:0", "-c:v", "copy", "-f", "streamhash", "-hash", "sha256", "pipe:1"],
            check=True, capture_output=True, timeout=30).stdout

    original = video_hash(source)
    video_edit.add_video_audio(str(source), str(audio), str(output))
    assert video_hash(output) == original


def test_soundtrack_replacement_does_not_retry_unrelated_io_failure(tmp_path, monkeypatch):
    source, audio, output = (tmp_path / name for name in ("source.mp4", "soundtrack.wav", "voiced.mp4"))
    source.write_bytes(b"video")
    audio.write_bytes(b"audio")
    attempts = []

    def inaccessible(arguments, destination):
        attempts.append(arguments)
        raise RuntimeError("Permission denied")

    monkeypatch.setattr(video_edit, "_run", inaccessible)
    with pytest.raises(RuntimeError, match="Permission denied"):
        video_edit.add_video_audio(str(source), str(audio), str(output))
    assert len(attempts) == 1
    assert not output.exists()


def _clip(path, color):
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c={color}:s=160x90:r=10:d=0.5",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                    "-shortest", "-c:v", "libx264", "-c:a", "aac", str(path)],
                   check=True, capture_output=True)


def test_agent_can_trim_join_and_replace_soundtrack(tmp_path):
    first, second = tmp_path / "one.mp4", tmp_path / "two.mp4"
    _clip(first, "red")
    _clip(second, "blue")
    edited, joined, voiced = (tmp_path / name for name in ("edited.mp4", "joined.mp4", "voiced.mp4"))
    names = [item["name"] for item in toolbox_mcp.toolbox_search("trim and join video clips")["tools"]]
    assert "local.edit_video" in names or "local.join_videos" in names
    edit = asyncio.run(toolbox_mcp.toolbox_call("local.edit_video", json.dumps({
        "source_path": str(first), "output_path": str(edited),
        "duration_seconds": .3, "layout": "square", "speed": 1.25,
    })))
    assert edit["path"] == str(edited) and edited.stat().st_size > 1000
    joined_result = video_edit.join_videos([str(first), str(second)], str(joined), "square")
    assert joined_result["path"] == str(joined) and joined.stat().st_size > 1000
    audio = tmp_path / "song.wav"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=600:duration=1", str(audio)],
                   check=True, capture_output=True)
    video_edit.add_video_audio(str(joined), str(audio), str(voiced))
    assert voiced.stat().st_size > 1000
    for path in (edited, joined, voiced):
        probe = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                               capture_output=True, text=True)
        assert "Video:" in probe.stderr
    assert first.exists() and second.exists()
    with pytest.raises(ValueError):
        video_edit.edit_video(str(first), str(first))


@pytest.mark.parametrize("existing_output", [False, True])
def test_run_timeout_cleans_partial_output_only(tmp_path, monkeypatch, existing_output):
    output = tmp_path / "edited.mp4"
    if existing_output:
        output.write_bytes(b"existing video")
    timeout_error = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

    def timed_out(command, **kwargs):
        assert command[-1] == str(output)
        if not existing_output:
            output.write_bytes(b"partial video")
        raise timeout_error

    monkeypatch.setattr(video_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(video_edit.subprocess, "run", timed_out)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        video_edit._run([], output, timeout=1)
    assert caught.value is timeout_error
    if existing_output:
        assert output.read_bytes() == b"existing video"
    else:
        assert not output.exists()
        assert video_edit._destination(str(output), []) == output


@pytest.mark.parametrize("existing_output", [False, True])
def test_run_interruption_cleans_partial_output_only(tmp_path, monkeypatch, existing_output):
    output = tmp_path / "edited.mp4"
    if existing_output:
        output.write_bytes(b"existing video")
    interruption = KeyboardInterrupt()

    def interrupted(command, **kwargs):
        assert command[-1] == str(output)
        if not existing_output:
            output.write_bytes(b"partial video")
        raise interruption

    monkeypatch.setattr(video_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(video_edit.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        video_edit._run([], output)
    assert caught.value is interruption
    if existing_output:
        assert output.read_bytes() == b"existing video"
    else:
        assert not output.exists()
        assert video_edit._destination(str(output), []) == output


@pytest.mark.parametrize("overwrite_refused", [False, True])
def test_run_distinguishes_overwrite_refusal_from_output_filename(tmp_path, monkeypatch, overwrite_refused):
    output = tmp_path / "already exists.mp4"

    def failed_output(command, **kwargs):
        assert command[-1] == str(output)
        if overwrite_refused:
            # Another writer created the destination after _run checked it.
            output.write_bytes(b"existing video")
            stderr = f"File '{output}' already exists. Exiting.\n"
        else:
            output.write_bytes(b"partial video")
            stderr = f"Error opening output file {output}.\nError opening output files: Invalid argument\n"
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(video_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(video_edit.subprocess, "run", failed_output)
    with pytest.raises(RuntimeError):
        video_edit._run([], output)
    if overwrite_refused:
        assert output.read_bytes() == b"existing video"
    else:
        assert not output.exists()
        assert video_edit._destination(str(output), []) == output


@pytest.mark.parametrize("interrupted", [False, True])
def test_run_preserves_original_exception_when_destination_becomes_directory(tmp_path, monkeypatch, interrupted):
    output = tmp_path / "edited.mp4"
    error = KeyboardInterrupt() if interrupted else subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

    def failed_process(command, **kwargs):
        assert command[-1] == str(output)
        # Another process creates a directory after the initial existence check.
        output.mkdir()
        (output / "keep.txt").write_text("preserve me")
        raise error

    monkeypatch.setattr(video_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(video_edit.subprocess, "run", failed_process)

    with pytest.raises(type(error)) as caught:
        video_edit._run([], output, timeout=1)

    assert caught.value is error
    assert output.is_dir()
    assert (output / "keep.txt").read_text() == "preserve me"


@pytest.mark.parametrize("interrupted", [False, True])
def test_run_preserves_original_exception_when_cleanup_is_denied(tmp_path, monkeypatch, interrupted):
    output = tmp_path / "edited.mp4"
    error = KeyboardInterrupt() if interrupted else subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)
    cleanup_attempts = []

    def failed_process(command, **kwargs):
        assert command[-1] == str(output)
        output.write_bytes(b"partial video")
        raise error

    def cannot_delete(path, **kwargs):
        cleanup_attempts.append(path)
        raise PermissionError("Output is locked")

    monkeypatch.setattr(video_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(video_edit.subprocess, "run", failed_process)
    monkeypatch.setattr(type(output), "unlink", cannot_delete)

    with pytest.raises(type(error)) as caught:
        video_edit._run([], output, timeout=1)

    assert caught.value is error
    assert cleanup_attempts == [output]
    assert output.read_bytes() == b"partial video"


def test_run_cleans_partial_output_with_undecodable_diagnostic(tmp_path, monkeypatch):
    output = tmp_path / "edited.mp4"
    real_run = subprocess.run

    def failed_process(command, **kwargs):
        assert command[-1] == str(output)
        # Exercise subprocess's real text decoding, without depending on FFmpeg
        # or the host locale to produce a particular diagnostic encoding.
        kwargs["encoding"] = "utf-8"
        script = (
            "import pathlib, sys; "
            "pathlib.Path(sys.argv[1]).write_bytes(b'partial video'); "
            "sys.stderr.buffer.write(b'Invalid filename: \\xff\\n'); "
            "sys.exit(1)"
        )
        return real_run([sys.executable, "-c", script, str(output)], **kwargs)

    monkeypatch.setattr(video_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(video_edit.subprocess, "run", failed_process)

    with pytest.raises(RuntimeError, match="Invalid filename"):
        video_edit._run([], output, timeout=30)

    assert not output.exists()
    assert video_edit._destination(str(output), []) == output


def test_run_removes_empty_output_after_successful_process(tmp_path, monkeypatch):
    output = tmp_path / "edited.mp4"

    def empty_output(command, **kwargs):
        assert command[-1] == str(output)
        output.write_bytes(b"")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(video_edit.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(video_edit.subprocess, "run", empty_output)
    with pytest.raises(RuntimeError, match="Video edit failed"):
        video_edit._run([], output)
    assert not output.exists()
    assert video_edit._destination(str(output), []) == output


def test_replace_soundtrack_accepts_vp8_webm(tmp_path):
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    source = tmp_path / "source.webm"
    audio = tmp_path / "soundtrack.wav"
    output = tmp_path / "voiced.mp4"
    subprocess.run([
        executable, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=red:s=160x90:r=10:d=1",
        "-c:v", "libvpx", str(source),
    ], check=True, capture_output=True, timeout=30)
    subprocess.run([
        executable, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=600:duration=1", str(audio),
    ], check=True, capture_output=True, timeout=30)
    original = source.read_bytes()

    result = video_edit.add_video_audio(str(source), str(audio), str(output))

    assert result["path"] == str(output)
    video = subprocess.run([
        executable, "-hide_banner", "-loglevel", "error", "-i", str(output),
        "-map", "0:v:0", "-frames:v", "1", "-pix_fmt", "rgb24",
        "-f", "rawvideo", "pipe:1",
    ], check=True, capture_output=True, timeout=30)
    assert len(video.stdout) == 160 * 90 * 3
    soundtrack = subprocess.run([
        executable, "-hide_banner", "-loglevel", "error", "-i", str(output),
        "-map", "0:a:0", "-ac", "1", "-ar", "8000",
        "-f", "s16le", "pipe:1",
    ], check=True, capture_output=True, timeout=30)
    assert len(soundtrack.stdout) > 8000
    assert source.read_bytes() == original


def test_edit_video_original_layout_pads_odd_dimensions(tmp_path):
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    source = tmp_path / "odd.mkv"
    output = tmp_path / "edited.mp4"
    subprocess.run([
        executable, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=161x91:rate=10:duration=0.5",
        "-c:v", "ffv1", "-pix_fmt", "yuv444p", str(source),
    ], check=True, capture_output=True, timeout=30)
    original = source.read_bytes()

    result = video_edit.edit_video(str(source), str(output), layout="original")

    assert result["path"] == str(output)
    frame = subprocess.run([
        executable, "-hide_banner", "-loglevel", "error", "-i", str(output),
        "-map", "0:v:0", "-frames:v", "1", "-pix_fmt", "rgb24",
        "-f", "rawvideo", "pipe:1",
    ], check=True, capture_output=True, timeout=30)
    assert len(frame.stdout) == 162 * 92 * 3
    assert source.read_bytes() == original


@pytest.mark.parametrize("operation", ["edit", "join"])
def test_fixed_layout_preserves_anamorphic_display_proportions(tmp_path, operation):
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    source = tmp_path / "anamorphic.mp4"
    output = tmp_path / "square.mp4"
    subprocess.run([
        executable, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=white:s=160x90:r=10:d=0.5",
        "-vf", "setsar=2", "-c:v", "libx264", str(source),
    ], check=True, capture_output=True, timeout=30)

    if operation == "edit":
        video_edit.edit_video(str(source), str(output), layout="square")
    else:
        video_edit.join_videos([str(source), str(source)], str(output), layout="square")

    frame = subprocess.run([
        executable, "-hide_banner", "-loglevel", "error", "-i", str(output),
        "-map", "0:v:0", "-frames:v", "1", "-pix_fmt", "gray",
        "-f", "rawvideo", "pipe:1",
    ], check=True, capture_output=True, timeout=30)
    assert len(frame.stdout) == 1080 * 1080
    # Display aspect is (160 * 2) / 90, not 160 / 90. In a
    # 1080-square canvas the white picture must be about 304 pixels high.
    white_rows = [y for y in range(1080) if frame.stdout[y * 1080 + 540] > 200]
    assert len(white_rows) == pytest.approx(1080 * 90 / 320, abs=3)
    assert (white_rows[0] + white_rows[-1]) / 2 == pytest.approx(539.5, abs=2)


def test_edit_video_extreme_speed_chains_atempo(tmp_path):
    """Speed 0.25 and 4.0 should work by chaining atempo filters."""
    clip = tmp_path / "clip.mp4"
    _clip(clip, "green")
    slow = tmp_path / "slow.mp4"
    fast = tmp_path / "fast.mp4"
    # 0.25x requires atempo=0.5,atempo=0.5
    video_edit.edit_video(str(clip), str(slow), duration_seconds=0.3, speed=0.25)
    assert slow.stat().st_size > 1000
    # 4.0x requires atempo=2.0,atempo=2.0
    video_edit.edit_video(str(clip), str(fast), duration_seconds=0.3, speed=4.0)
    assert fast.stat().st_size > 1000
    for path in (slow, fast):
        probe = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                               capture_output=True, text=True)
        assert "Audio:" in probe.stderr


@pytest.mark.parametrize("speed", [0.25, 4.0])
@pytest.mark.parametrize("with_audio", [False, True])
def test_speed_change_trims_requested_source_segment_before_retiming(tmp_path, speed, with_audio):
    """A source interval of four seconds becomes 16s slow or 1s fast."""
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    source, output = tmp_path / "source.mp4", tmp_path / "retimed.mp4"
    args = [executable, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=red:s=160x90:r=20:d=6"]
    if with_audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:a", "aac"]
    args += ["-c:v", "libx264", str(source)]
    subprocess.run(args, check=True, capture_output=True, timeout=30)
    video_edit.edit_video(str(source), str(output), start_seconds=1, duration_seconds=4, speed=speed)
    # Decode actual video frames and PCM audio instead of trusting container
    # duration, which can conceal a truncated or mismatched individual stream.
    video = subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-i", str(output),
        "-map", "0:v:0", "-vf", "fps=20", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"],
        check=True, capture_output=True, timeout=30)
    expected = 4 / speed
    actual_video = len(video.stdout) / (160 * 90 * 20)
    assert actual_video == pytest.approx(expected, abs=0.2)
    if with_audio:
        audio = subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-i", str(output),
            "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
            check=True, capture_output=True, timeout=30)
        actual_audio = len(audio.stdout) / (8000 * 2)
        assert actual_audio == pytest.approx(expected, abs=0.2)
        assert abs(actual_video - actual_audio) < 0.2
