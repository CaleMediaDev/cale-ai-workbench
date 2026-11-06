"""Separate a local song into vocal and instrumental tracks on first use.

Audio Separator runs in its own environment so its dependencies cannot change
the workbench's voice, vision, or ACE-Step runtimes. Model files stay local.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import music_backend

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "Media" / "music" / "audio-separator"
VERSION = "0.48.0"
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _publication_signature(info):
    # Windows path/fd ctime can disagree after linking; content hashes below
    # also protect same-size in-place edits even when mtime is restored.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _stem_digest(path: Path):
    """Hash one rendered stem with bounded memory before publishing it."""
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        if _publication_signature(os.fstat(stream.fileno())) != _publication_signature(before) or size != before.st_size:
            raise OSError("A rendered stem changed before publication")
    return before, digest, size


class _StemPublication:
    """Track created bytes, including a failed copy's known written prefix.

    Ownership alone is insufficient: a user may edit a file in place without
    changing its inode. Rollback requires unchanged metadata AND content.
    """
    def __init__(self, path, info, digest, size, stream=None):
        self.path, self.identity = path, (info.st_dev, info.st_ino)
        self.digest, self.size, self.stream, self.signature = digest, size, stream, None

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, data):
        written = self.stream.write(data)
        if type(written) is not int or not 0 <= written <= len(data):
            raise OSError("Stem copy did not report its written bytes")
        self.digest.update(data[:written])
        self.size += written
        if written != len(data):
            raise OSError("Stem copy could not write the full buffer")
        return written

    def mark(self):
        try:
            current = self.path.lstat()
            if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == self.identity:
                self.signature = _publication_signature(current)
        except OSError:
            pass  # A removed/replaced destination is no longer ours to clean.

    def rollback(self):
        try:
            if self.signature is None or self.signature != _publication_signature(self.path.lstat()):
                return
            digest, size = hashlib.sha256(), 0
            with self.path.open("rb") as stream:
                if _publication_signature(os.fstat(stream.fileno())) != self.signature:
                    return
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                if _publication_signature(os.fstat(stream.fileno())) != self.signature:
                    return
            if (size == self.size and digest.digest() == self.digest.digest()
                    and self.signature == _publication_signature(self.path.lstat())):
                self.path.unlink()
        except OSError:
            pass  # Never mask the original failure or delete an unverifiable file.


def _python() -> Path:
    return ENGINE / ".venv" / "Scripts" / "python.exe"


def install() -> Path:
    """Pin the upstream separator in an isolated runtime, only when needed."""
    marker = ENGINE / ".cale-installed"
    if marker.is_file() and marker.read_text(encoding="ascii") == VERSION and _python().is_file():
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
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=7200, creationflags=HIDE,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout)[-1200:] or "Audio separation failed.")
            staged_files = sorted(
                path for path in staging.iterdir()
                if path.suffix.lower() == ".wav" and not path.is_symlink()
                and path.is_file() and path.stat().st_size > 0
            )
            if len(staged_files) < 2:
                raise RuntimeError("Audio separator did not produce both vocal and instrumental tracks.")
            files = [target / path.name for path in staged_files]
            published = []
            try:
                for path in staged_files:
                    destination = target / path.name
                    original, expected_digest, expected_size = _stem_digest(path)
                    # Prefer a no-replace hard link, but support filesystems
                    # without hard links using an exclusive, rollback-safe copy.
                    try:
                        os.link(path, destination)
                    except OSError as exc:
                        if exc.errno not in {errno.ENOTSUP, errno.EXDEV}:
                            raise
                        publication = None
                        try:
                            # Track actual disk writes, not bytes accepted by a buffer:
                            # a failed flush could otherwise leave an untracked prefix.
                            with destination.open("xb", buffering=0) as output:
                                publication = _StemPublication(destination, os.fstat(output.fileno()), hashlib.sha256(), 0, output)
                                published.append(publication)
                                with path.open("rb") as input_file:
                                    shutil.copyfileobj(input_file, publication)
                                if publication.size != expected_size or publication.digest.digest() != expected_digest.digest():
                                    raise OSError("A rendered stem changed during publication")
                        finally:
                            if publication is not None:
                                publication.mark()  # After close/flush, including copy failure.
                    else:
                        publication = _StemPublication(destination, original, expected_digest, expected_size)
                        publication.mark()
                        published.append(publication)
                        current, actual_digest, actual_size = _stem_digest(destination)
                        if (_publication_signature(current) != _publication_signature(original)
                                or actual_size != expected_size
                                or actual_digest.digest() != expected_digest.digest()):
                            raise OSError("A rendered stem changed during publication")
            except BaseException:
                for publication in reversed(published):
                    publication.rollback()
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
        # A failed copy must not leave an executable that retries will reuse.
        with TemporaryDirectory(prefix=".ffmpeg-", dir=ffmpeg_dir) as staging_dir:
            staged_ffmpeg = Path(staging_dir) / ffmpeg.name
            shutil.copy2(binary, staged_ffmpeg)
            os.replace(staged_ffmpeg, ffmpeg)
    os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")
    separator = Separator(output_dir=target, model_file_dir=str(ENGINE / "models"), output_format="WAV")
    separator.load_model()
    separator.separate(source)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--worker":
    _worker(sys.argv[2], sys.argv[3])
