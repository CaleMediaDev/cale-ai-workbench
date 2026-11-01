"""Render a shareable music visualizer entirely on the user's computer."""

from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg

SIZES = {"wide": (1280, 720), "vertical": (720, 1280), "square": (720, 720)}
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def make_music_video(source_path: str, output_path: str, layout: str = "wide") -> dict:
    """Pair a local song with an animated spectrum in a new H.264/AAC MP4."""
    source = Path(source_path).expanduser().resolve(strict=True)
    output = Path(output_path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in {".wav", ".mp3", ".flac", ".m4a", ".ogg"}:
        raise ValueError("Choose an existing WAV, MP3, FLAC, M4A or OGG audio file.")
    if layout not in SIZES:
        raise ValueError("Choose wide, vertical or square video layout.")
    if output.suffix.lower() != ".mp4" or output == source or output.exists():
        raise ValueError("Save the video as a new MP4 file without replacing an existing file.")
    width, height = SIZES[layout]
    output.parent.mkdir(parents=True, exist_ok=True)
    # FFmpeg's spectrum filter draws frames from audio, so the video stops with
    # the actual song and never needs an estimated duration or a cloud service.
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
        "-i", str(source), "-filter_complex",
        f"[0:a]showspectrum=s={width}x{height}:mode=combined:slide=scroll:color=rainbow:fps=30[v]",
        "-map", "[v]", "-map", "0:a:0", "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-crf", "23", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-shortest", str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600, creationflags=HIDE)
    if result.returncode or not output.is_file() or not output.stat().st_size:
        if result.returncode and output.is_file() and "already exists" not in result.stderr.lower():
            output.unlink(missing_ok=True)
        raise RuntimeError((result.stderr or result.stdout)[-800:] or "Local music video failed.")
    return {"source": str(source), "path": str(output), "layout": layout, "kind": "music_visualizer"}
