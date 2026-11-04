"""Separate a local song into vocal and instrumental tracks on first use.

Audio Separator runs in its own environment so its dependencies cannot change
the workbench's voice, vision, or ACE-Step runtimes. Model files stay local.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import music_backend

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "Media" / "music" / "audio-separator"
VERSION = "0.48.0"
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _python() -> Path:
    return ENGINE / ".venv" / "Scripts" / "python.exe"


def install() -> Path:
    """Pin the upstream separator in an isolated runtime, only when needed."""
    marker = ENGINE / ".cale-installed"
    if marker.is_file() and marker.read_bytes() == VERSION.encode("ascii") and _python().is_file():
        return _python()
    if shutil.disk_usage(ROOT).free < 6 * 1024**3:
        raise RuntimeError("Separating audio needs at least 6 GB of free disk space.")
    uv = music_backend._tool("uv")
    ENGINE.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["UV_PYTHON_INSTALL_DIR"] = str(ROOT / "Media" / "runtime")
    env["UV_CACHE_DIR"] = str(ROOT / "Media" / "uv-cache")
    # A rebuild must not retain a success marker if either subprocess fails.
    marker.unlink(missing_ok=True)
    subprocess.run(
        [uv, "venv", str(ENGINE / ".venv"), "--python", "3.11"],
        env=env, check=True, timeout=300, creationflags=HIDE,
    )
    subprocess.run(
        [uv, "pip", "install", "--python", str(_python()),
         f"audio-separator[gpu]=={VERSION}", "imageio-ffmpeg==0.6.0"],
        env=env, check=True, timeout=3600, creationflags=HIDE,
    )
    marker.write_text(VERSION, encoding="ascii")
    return _python()


def separate_stems(source_path: str, output_dir: str) -> dict:
    """Return two real local files; never overwrite or modify the input."""
    source = Path(source_path).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in {".wav", ".mp3", ".flac", ".m4a", ".ogg"}:
        raise ValueError("Choose an existing WAV, MP3, FLAC, M4A or OGG audio file.")
    target = Path(output_dir).expanduser().resolve()
    if source.parent == target:
        raise ValueError("Choose a separate output folder for the vocal tracks.")
    with music_backend._setup_lock(), music_backend._music_gpu_lease():
        if target.exists() and any(target.iterdir()):
            raise ValueError("Choose an empty output folder to protect existing audio files.")
        target.mkdir(parents=True, exist_ok=True)
        python = install()
        with TemporaryDirectory(prefix=".stems-", dir=target) as staging_dir:
            staging = Path(staging_dir)
            result = subprocess.run(
                [str(python), str(Path(__file__).resolve()), "--worker", str(source), str(staging)],
                capture_output=True, text=True, timeout=7200, creationflags=HIDE,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout)[-1200:] or "Audio separation failed.")
            staged_files = sorted(
                path for path in staging.iterdir()
                if path.suffix.lower() == ".wav" and path.is_file() and path.stat().st_size > 0
            )
            if len(staged_files) < 2:
                raise RuntimeError("Audio separator did not produce both vocal and instrumental tracks.")
            files = [target / path.name for path in staged_files]
            published = []
            try:
                for path in staging.iterdir():
                    destination = target / path.name
                    path.rename(destination)
                    published.append(destination)
            except OSError:
                for path in reversed(published):
                    path.rename(staging / path.name)
                raise
    return {"source": str(source), "output_dir": str(target),
            "files": [str(path) for path in files], "model": "Audio Separator"}


def _worker(source: str, target: str) -> None:
    """Run inference out of process so PyTorch memory is released afterward."""
    import imageio_ffmpeg
    from audio_separator.separator import Separator

    binary = Path(imageio_ffmpeg.get_ffmpeg_exe())
    ffmpeg_dir = ENGINE / "bin"
    ffmpeg_dir.mkdir(exist_ok=True)
    ffmpeg = ffmpeg_dir / "ffmpeg.exe"
    if not ffmpeg.exists():
        shutil.copy2(binary, ffmpeg)
    os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")
    separator = Separator(output_dir=target, model_file_dir=str(ENGINE / "models"), output_format="WAV")
    separator.load_model()
    separator.separate(source)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--worker":
    _worker(sys.argv[2], sys.argv[3])
