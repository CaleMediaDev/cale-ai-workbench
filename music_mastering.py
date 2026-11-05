"""Master a local song against a reference with the optional Matchering runtime.

The GPL-licensed Matchering package is installed separately on first use; no
Matchering source or model weights are copied into the Workbench distribution.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __name__ != "__main__":
    import music_backend

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "Media" / "music" / "matchering"
VERSION = "2.0.6"
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SUPPORTED = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}


def _python() -> Path:
    return ENGINE / ".venv" / "Scripts" / "python.exe"


def install() -> Path:
    """Prepare the pinned offline processing runtime only when requested."""
    marker = ENGINE / ".cale-installed"
    if marker.is_file() and marker.read_text(encoding="ascii") == VERSION and _python().is_file():
        return _python()
    if shutil.disk_usage(ROOT).free < 2 * 1024**3:
        raise RuntimeError("Music mastering needs at least 2 GB of free disk space.")
    uv = music_backend._tool("uv")
    ENGINE.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["UV_PYTHON_INSTALL_DIR"] = str(ROOT / "Media" / "runtime")
    env["UV_CACHE_DIR"] = str(ROOT / "Media" / "uv-cache")
    if not _python().is_file():
        subprocess.run([uv, "venv", str(ENGINE / ".venv"), "--python", "3.11"],
                       env=env, check=True, timeout=300, creationflags=HIDE)
    subprocess.run([uv, "pip", "install", "--python", str(_python()),
                    f"matchering=={VERSION}", "imageio-ffmpeg==0.6.0"],
                   env=env, check=True, timeout=1800, creationflags=HIDE)
    marker.write_text(VERSION, encoding="ascii")
    return _python()


def master_music(source_path: str, reference_path: str, output_path: str) -> dict:
    """Write a new mastered WAV; preserve both input audio files."""
    source = Path(source_path).expanduser().resolve(strict=True)
    reference = Path(reference_path).expanduser().resolve(strict=True)
    output = Path(output_path).expanduser()
    output = output.parent.resolve() / output.name
    if not source.is_file() or source.suffix.lower() not in SUPPORTED:
        raise ValueError("Choose an existing WAV, MP3, FLAC, M4A or OGG source song.")
    if not reference.is_file() or reference.suffix.lower() not in SUPPORTED:
        raise ValueError("Choose an existing WAV, MP3, FLAC, M4A or OGG reference song.")
    if source.samefile(reference):
        raise ValueError("Choose a different reference song to match.")
    if output.suffix.lower() != ".wav":
        raise ValueError("The mastered output must be a WAV file.")
    if output.exists() or output.is_symlink() or output in (source, reference):
        raise ValueError("Choose a new output filename so existing audio is preserved.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with music_backend._setup_lock():
        python = install()
    # Render privately on the destination filesystem. Publishing must not replace
    # a file created after the initial existence check, even on worker failure.
    with tempfile.TemporaryDirectory(prefix="cale-master-output-", dir=output.parent) as temporary:
        staged = Path(temporary) / "master.wav"
        result = subprocess.run(
            [str(python), str(Path(__file__).resolve()), "--worker", str(source),
             str(reference), str(staged)], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=3600, creationflags=HIDE,
        )
        if result.returncode or not staged.is_file() or staged.stat().st_size == 0:
            raise RuntimeError((result.stderr or result.stdout)[-1200:] or "Music mastering failed.")
        try:
            os.link(staged, output)
        except FileExistsError:
            raise ValueError("Choose a new output filename so existing audio is preserved.") from None
        except OSError as exc:
            if (exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS}
                    and getattr(exc, "winerror", None) not in {1, 50}):
                raise
            # Some destination filesystems cannot create hard links. Exclusive
            # creation still protects files created during rendering.
            try:
                destination = output.open("xb")
            except FileExistsError:
                raise ValueError("Choose a new output filename so existing audio is preserved.") from None
            created_stat = os.fstat(destination.fileno())
            try:
                with destination:
                    with staged.open("rb") as rendered:
                        shutil.copyfileobj(rendered, destination)
            except BaseException as exc:
                try:
                    # Another operation may have moved or replaced the partial
                    # file. Do not deliberately remove a different file or link.
                    if os.path.samestat(created_stat, output.lstat()):
                        output.unlink()
                except FileNotFoundError:
                    pass
                except OSError as cleanup_error:
                    raise exc from cleanup_error
                raise
    return {"source": str(source), "reference": str(reference), "path": str(output),
            "engine": "Matchering 2.0.6"}


def _worker(source: str, reference: str, output: str) -> None:
    """Convert common formats to WAV, then master in a separate process."""
    import tempfile

    import imageio_ffmpeg
    import matchering as mg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="cale-master-") as temporary:
        inputs = []
        for index, path in enumerate((source, reference)):
            wav = str(Path(temporary) / f"input-{index}.wav")
            subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-i", path,
                            "-ac", "2", "-ar", "44100", "-c:a", "pcm_f32le", wav], check=True,
                           timeout=900, creationflags=HIDE)
            inputs.append(wav)
        mg.process(target=inputs[0], reference=inputs[1], results=[mg.pcm24(output)])


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "--worker":
    _worker(sys.argv[2], sys.argv[3], sys.argv[4])
