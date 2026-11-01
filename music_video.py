"""Render a shareable music visualizer entirely on the user's computer."""

from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg

SIZES = {"wide": (1280, 720), "vertical": (720, 1280), "square": (720, 720)}
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def make_music_video(source_path: str, output_path: str, layout: str = "wide",
                     background_image: str = "") -> dict:
    """Pair a local song and optional cover art with an animated spectrum."""
    source = Path(source_path).expanduser().resolve(strict=True)
    output = Path(output_path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in {".wav", ".mp3", ".flac", ".m4a", ".ogg"}:
        raise ValueError("Choose an existing WAV, MP3, FLAC, M4A or OGG audio file.")
    if layout not in SIZES:
        raise ValueError("Choose wide, vertical or square video layout.")
    if output.suffix.lower() != ".mp4" or output == source or output.exists():
        raise ValueError("Save the video as a new MP4 file without replacing an existing file.")
    image = None
    if background_image:
        image = Path(background_image).expanduser().resolve(strict=True)
        if not image.is_file() or image.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("Choose an existing PNG, JPG or WebP background image.")
    width, height = SIZES[layout]
    output.parent.mkdir(parents=True, exist_ok=True)
    # FFmpeg's spectrum filter draws frames from audio, so the video stops with
    # the actual song and never needs an estimated duration or a cloud service.
    inputs = ["-i", str(source)]
    spectrum = f"[0:a]showspectrum=s={width}x{height}:mode=combined:slide=scroll:color=rainbow:fps=30"
    if image is None:
        filters = spectrum + "[v]"
    else:
        inputs.extend(["-loop", "1", "-framerate", "30", "-i", str(image)])
        # The cover fills the frame; black spectrum pixels become transparent
        # so the animated colors sit over the person's own image.
        filters = (
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},format=rgba[cover];"
            + spectrum + ",format=rgba,colorkey=0x000000:0.05:0.1[spec];"
            "[cover][spec]overlay=shortest=1:format=auto,format=yuv420p[v]"
        )
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
        *inputs, "-filter_complex", filters,
        "-map", "[v]", "-map", "0:a:0", "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-crf", "23", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-shortest", str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600, creationflags=HIDE)
    if result.returncode or not output.is_file() or not output.stat().st_size:
        if result.returncode and output.is_file() and "already exists" not in result.stderr.lower():
            output.unlink(missing_ok=True)
        raise RuntimeError((result.stderr or result.stdout)[-800:] or "Local music video failed.")
    return {"source": str(source), "path": str(output), "layout": layout,
            "background_image": str(image) if image else "", "kind": "music_visualizer"}
