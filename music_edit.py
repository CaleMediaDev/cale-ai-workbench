"""Small, offline audio edits for songs created or supplied by the user.

FFmpeg is already bundled with the Workbench toolbox through imageio-ffmpeg.
These tools write new files, leaving recordings and generated songs intact.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import imageio_ffmpeg

SUPPORTED = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}
OUTPUTS = {".wav", ".mp3", ".flac"}
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _source(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in SUPPORTED:
        raise ValueError("Choose an existing WAV, MP3, FLAC, M4A or OGG audio file.")
    return path


def _output(value: str, sources: tuple[Path, ...]) -> Path:
    path = Path(value).expanduser().resolve()
    if path.suffix.lower() not in OUTPUTS:
        raise ValueError("Save edited audio as a new WAV, MP3 or FLAC file.")
    if path.exists() or path in sources:
        raise ValueError("Choose a new output filename so existing audio is preserved.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _run(arguments: list[str], output: Path) -> None:
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-n", *arguments, str(output)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600, creationflags=HIDE)
    if result.returncode or not output.is_file() or not output.stat().st_size:
        # FFmpeg's -n refuses to overwrite an output created during this call.
        # Preserve that file, but remove failed or empty output to allow retrying.
        if (output.is_file() and "already exists" not in result.stderr.lower()
                and (result.returncode or not output.stat().st_size)):
            output.unlink(missing_ok=True)
        raise RuntimeError((result.stderr or result.stdout)[-800:] or "Local audio edit failed.")


def edit_music(source_path: str, output_path: str, start_seconds: float = 0,
               duration_seconds: float | None = None, fade_in_seconds: float = 0,
               fade_out_seconds: float = 0, gain_db: float = 0) -> dict:
    """Trim, fade and adjust gain in one pass; duration is the saved clip length."""
    source = _source(source_path)
    output = _output(output_path, (source,))
    timings = (start_seconds, fade_in_seconds, fade_out_seconds)
    if duration_seconds is not None:
        timings += (duration_seconds,)
    if not all(math.isfinite(value) for value in timings):
        raise ValueError("Start, duration and fade lengths must be finite.")
    if start_seconds < 0 or duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("Start must be nonnegative and duration must be positive.")
    if fade_in_seconds < 0 or fade_out_seconds < 0:
        raise ValueError("Fade lengths must be nonnegative.")
    if fade_out_seconds and duration_seconds is None:
        raise ValueError("Choose a clip duration when using fade-out.")
    if duration_seconds is not None and max(fade_in_seconds, fade_out_seconds) > duration_seconds:
        raise ValueError("A fade cannot be longer than the saved clip.")
    if not -60 <= gain_db <= 24:
        raise ValueError("Gain must be between -60 and +24 dB.")
    filters = []
    if gain_db:
        filters.append(f"volume={gain_db:g}dB")
    if fade_in_seconds:
        filters.append(f"afade=t=in:st=0:d={fade_in_seconds:g}")
    if fade_out_seconds:
        filters.append(f"afade=t=out:st={duration_seconds - fade_out_seconds:g}:d={fade_out_seconds:g}")
    args = ["-ss", str(start_seconds), "-i", str(source)]
    if duration_seconds is not None:
        args += ["-t", str(duration_seconds)]
    if filters:
        args += ["-af", ",".join(filters)]
    args += ["-vn"]
    _run(args, output)
    return {"source": str(source), "path": str(output), "edit": "trim/fade/gain"}


def mix_music(background_path: str, foreground_path: str, output_path: str,
              background_gain_db: float = -10, foreground_gain_db: float = 0,
              foreground_start_seconds: float = 0) -> dict:
    """Mix voice or another track over a song; preserve both inputs."""
    background = _source(background_path)
    foreground = _source(foreground_path)
    output = _output(output_path, (background, foreground))
    if background == foreground:
        raise ValueError("Choose two different audio files to mix.")
    if not all(-60 <= gain <= 24 for gain in (background_gain_db, foreground_gain_db)):
        raise ValueError("Track gain must be between -60 and +24 dB.")
    if not 0 <= foreground_start_seconds <= 36000:
        raise ValueError("Foreground start must be between 0 and 10 hours.")
    delay = round(foreground_start_seconds * 1000)
    graph = (f"[0:a]volume={background_gain_db:g}dB[bg];"
             f"[1:a]volume={foreground_gain_db:g}dB,adelay={delay}:all=1[fg];"
             "[bg][fg]amix=inputs=2:duration=longest:normalize=0[mix]")
    _run(["-i", str(background), "-i", str(foreground), "-filter_complex", graph,
          "-map", "[mix]", "-vn"], output)
    return {"background": str(background), "foreground": str(foreground),
            "path": str(output), "edit": "mix"}
