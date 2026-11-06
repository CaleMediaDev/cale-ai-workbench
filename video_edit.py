"""Offline video editing primitives for agent tasks.

Each operation creates a fresh MP4. FFmpeg receives argument arrays rather than
shell commands, so user-supplied filenames never become executable syntax.
"""

from __future__ import annotations

import errno
import math
import os
import stat
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg

VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
AUDIO = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _existing(value: str, extensions: set[str]) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in extensions:
        raise ValueError("Choose an existing supported media file.")
    return path


def _destination(value: str, sources: list[Path]) -> Path:
    path = Path(value).expanduser().resolve()
    if path.suffix.lower() != ".mp4":
        raise ValueError("Save the edited video as an MP4 file.")
    if path.exists() or path in sources:
        raise ValueError("Choose a new output filename; existing videos are preserved.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _number(value: float, minimum: float, maximum: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}.")
    return number


def _publish_no_replace(staged: Path, output: Path) -> None:
    """Publish a complete file without replacing a concurrently created path."""
    try:
        if os.name == "nt":
            # Windows rename refuses an existing destination without requiring hard links.
            os.rename(staged, output)
        else:
            # POSIX rename replaces existing files. A same-filesystem hard link
            # instead provides atomic no-clobber publication on supported stores.
            os.link(staged, output)
    except FileExistsError:
        raise ValueError("Choose a new output filename; existing videos are preserved.") from None
    except (OSError, NotImplementedError) as error:
        if os.name != "nt" and (isinstance(error, NotImplementedError) or
                getattr(error, "errno", None) in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV}):
            raise RuntimeError("This destination does not support no-overwrite video publication using hard links. Choose another local folder.") from error
        raise RuntimeError("Video publication failed. Existing destination paths are preserved; choose another output folder.") from error


def _run(arguments: list[str], output: Path, timeout: int = 7200) -> dict:
    # Render only in a new private directory on the destination filesystem.
    # Engine failure, timeout and cancellation never authorize public cleanup.
    directory = tempfile.TemporaryDirectory(prefix=".video-edit-", dir=output.parent,
                                            ignore_cleanup_errors=True)
    try:
        staged = Path(directory.name) / output.name
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner",
                   "-loglevel", "error", "-abort_on", "empty_output", "-n",
                   *arguments, str(staged)]
        result = subprocess.run(command, capture_output=True, text=True,
                                errors="replace", timeout=timeout, creationflags=HIDE)
        details = None
        if not result.returncode:
            try:
                details = staged.lstat()
            except OSError:
                pass  # Unreadable or missing staged output cannot be published.
        if result.returncode or details is None or not stat.S_ISREG(details.st_mode) or not details.st_size:
            diagnostic = (result.stderr or result.stdout) or "Video edit failed."
            # Keep classification from the full stderr while bounding its text;
            # add_video_audio permits one retry only for this exact muxer error.
            marker = "codec not currently supported in container"
            message = diagnostic[-1200:]
            if marker in diagnostic.casefold() and marker not in message.casefold():
                message = marker + "\n" + message[-(1200 - len(marker) - 1):]
            raise RuntimeError(message)
        _publish_no_replace(staged, output)
        return {"path": str(output), "bytes": details.st_size}
    finally:
        try:
            directory.cleanup()
        except OSError:
            # Only app-owned staging may remain locked. Do not replace a real
            # engine exception or remove any public path to make cleanup work.
            pass


def edit_video(source_path: str, output_path: str, start_seconds: float = 0,
               duration_seconds: float | None = None, layout: str = "original",
               speed: float = 1) -> dict:
    """Trim a source interval, resize, and change its playback speed.

    duration_seconds measures source time after start_seconds, before speed
    changes: four source seconds at speed 0.25 produce about sixteen seconds.
    """
    source = _existing(source_path, VIDEO)
    output = _destination(output_path, [source])
    start = _number(start_seconds, 0, 86400, "Start")
    duration = None if duration_seconds is None else _number(duration_seconds, .05, 86400, "Duration")
    rate = _number(speed, .25, 4, "Speed")
    sizes = {"original": None, "wide": (1920, 1080), "vertical": (1080, 1920), "square": (1080, 1080)}
    if layout not in sizes:
        raise ValueError("Layout must be original, wide, vertical or square.")
    args = ["-ss", str(start)]
    if duration is not None:
        # Input -t limits the source interval. Output -t would truncate a
        # slowed clip and let a sped-up clip read beyond the requested trim.
        # https://ffmpeg.org/ffmpeg.html#Main-options
        args += ["-t", str(duration)]
    args += ["-i", str(source)]
    filters = []
    if rate != 1:
        filters.append(f"setpts=PTS/{rate:g}")
    if sizes[layout]:
        width, height = sizes[layout]
        # Preserve display proportions before fitting square-pixel layouts.
        filters.extend(["scale=iw*sar:ih", "setsar=1"])
        filters.append(f"scale={width}:{height}:force_original_aspect_ratio=decrease")
        filters.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
        filters.append("setsar=1")
    else:
        # yuv420p requires even dimensions, including for original-layout inputs.
        filters.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")
    if filters:
        args += ["-vf", ",".join(filters)]
    if rate != 1:
        # Keep each stage within 0.5-2: valid slowdown and blended speed-up.
        # https://ffmpeg.org/ffmpeg-filters.html#atempo
        tempo_filters = []
        remaining = rate
        while remaining > 2.0:
            tempo_filters.append("atempo=2.0")
            remaining /= 2.0
        while remaining < 0.5:
            tempo_filters.append("atempo=0.5")
            remaining /= 0.5
        tempo_filters.append(f"atempo={remaining:g}")
        args += ["-af", ",".join(tempo_filters)]
    args += ["-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
    return {**_run(args, output), "edit": "trim/layout/speed", "source": str(source)}


def join_videos(source_paths: list[str], output_path: str, layout: str = "wide") -> dict:
    """Join two or more clips, normalizing dimensions and frame rate."""
    if not 2 <= len(source_paths) <= 30:
        raise ValueError("Choose 2 to 30 clips in playback order.")
    sources = [_existing(value, VIDEO) for value in source_paths]
    output = _destination(output_path, sources)
    sizes = {"wide": (1920, 1080), "vertical": (1080, 1920), "square": (1080, 1080)}
    if layout not in sizes:
        raise ValueError("Layout must be wide, vertical or square.")
    width, height = sizes[layout]
    args = []
    graph = []
    for index, source in enumerate(sources):
        args += ["-i", str(source)]
        graph.append(f"[{index}:v:0]scale=iw*sar:ih,setsar=1,"
                     f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                     f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,"
                     f"setpts=PTS-STARTPTS[v{index}]")
    # The concat filter accepts video-only clips; an optional soundtrack can be
    # added later with add_video_audio without inventing silent input streams.
    graph.append("".join(f"[v{index}]" for index in range(len(sources))) +
                 f"concat=n={len(sources)}:v=1:a=0[out]")
    args += ["-filter_complex", ";".join(graph), "-map", "[out]", "-an",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    return {**_run(args, output), "edit": "join", "sources": [str(p) for p in sources]}


def add_video_audio(source_path: str, audio_path: str, output_path: str,
                    audio_gain_db: float = 0) -> dict:
    """Replace a video's soundtrack with a local narration or song."""
    source = _existing(source_path, VIDEO)
    audio = _existing(audio_path, AUDIO)
    output = _destination(output_path, [source, audio])
    gain = _number(audio_gain_db, -60, 24, "Audio gain")
    inputs = ["-i", str(source), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0"]
    soundtrack = ["-c:a", "aac", "-af", f"volume={gain:g}dB",
                  "-shortest", "-movflags", "+faststart"]
    try:
        # Keep compatible video packets unchanged and avoid a costly re-encode.
        result = _run([*inputs, "-c:v", "copy", *soundtrack], output)
    except RuntimeError as error:
        if "codec not currently supported in container" not in str(error).casefold():
            raise
        # FFmpeg rejected this bitstream for MP4 (for example VP8 WebM).
        # Only that muxer error warrants one bounded conversion retry.
        result = _run([*inputs, "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                       "-pix_fmt", "yuv420p", *soundtrack], output)
    return {**result, "edit": "replace soundtrack", "source": str(source)}
