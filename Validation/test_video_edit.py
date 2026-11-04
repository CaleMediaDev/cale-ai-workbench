"""Render short real clips to verify the offline agent video tools."""

import asyncio
import json
import subprocess

import imageio_ffmpeg
import pytest

import toolbox_mcp
import video_edit


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
