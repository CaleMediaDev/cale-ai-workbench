"""Transcribe a local instrument recording to editable MIDI with Basic Pitch.

The optional model runs in an isolated Python environment. Installing it on
first use keeps a fresh Workbench installation small and avoids changing the
voice or music-generation dependencies.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import music_backend

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "Media" / "music" / "basic-pitch"
VERSION = "0.4.0"
RUNTIME_REVISION = VERSION + "-2"
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}
NOTE_COLUMNS = ["start_time_s", "end_time_s", "pitch_midi", "velocity", "pitch_bend"]


def _python() -> Path:
    return ENGINE / ".venv" / "Scripts" / "python.exe"


def install() -> Path:
    """Install the pinned CPU-capable inference runtime on first use."""
    marker = ENGINE / ".cale-installed"
    if marker.is_file() and marker.read_text(encoding="ascii") == RUNTIME_REVISION and _python().is_file():
        return _python()
    if shutil.disk_usage(ROOT).free < 3 * 1024**3:
        raise RuntimeError("Audio-to-MIDI needs at least 3 GB of free disk space.")
    uv = music_backend._tool("uv")
    ENGINE.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["UV_PYTHON_INSTALL_DIR"] = str(ROOT / "Media" / "runtime")
    env["UV_CACHE_DIR"] = str(ROOT / "Media" / "uv-cache")
    # Python 3.10 uses the ONNX model on Windows, avoiding a large TensorFlow install.
    if not _python().is_file():
        subprocess.run(
            [uv, "venv", str(ENGINE / ".venv"), "--python", "3.10"],
            env=env, check=True, timeout=300, creationflags=HIDE,
        )
    subprocess.run(
        [uv, "pip", "install", "--python", str(_python()),
         f"basic-pitch=={VERSION}", "imageio-ffmpeg==0.6.0", "setuptools==80.9.0"],
        env=env, check=True, timeout=3600, creationflags=HIDE,
    )
    marker.write_text(RUNTIME_REVISION, encoding="ascii")
    return _python()


def normalize_note_events(path: Path) -> None:
    """Make Basic Pitch's unquoted pitch-bend arrays valid five-column CSV."""
    temporary_file = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent,
        prefix=path.name + ".", suffix=".tmp", delete=False,
    )
    temporary = Path(temporary_file.name)
    try:
        with temporary_file as target, path.open("r", encoding="utf-8", newline="") as source:
            reader = csv.reader(source)
            writer = csv.writer(target)
            if next(reader, None) != NOTE_COLUMNS:
                raise RuntimeError("Basic Pitch returned an unexpected note-event CSV format.")
            writer.writerow(NOTE_COLUMNS)
            for row in reader:
                if not row:
                    continue
                if len(row) < 5:
                    raise RuntimeError("Basic Pitch returned an incomplete note event.")
                bends = row[4:]
                if bends == [""]:
                    bends = []
                elif bends[0].lstrip().startswith("["):
                    bends = json.loads(",".join(bends))
                row = row[:4] + [json.dumps([float(value) for value in bends], allow_nan=False)]
                writer.writerow(row)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def transcribe_to_midi(source_path: str, output_dir: str) -> dict:
    """Write a MIDI transcription and note-event CSV without changing the source."""
    source = Path(source_path).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError("Choose an existing WAV, MP3, FLAC, M4A or OGG audio file.")
    target = Path(output_dir).expanduser().resolve()
    if target == source.parent:
        raise ValueError("Choose a separate output folder for the MIDI file.")
    if target.exists() and not target.is_dir():
        raise ValueError("Choose an output folder, not an existing file.")
    if target.exists() and any(target.iterdir()):
        raise ValueError("Choose an empty output folder to protect existing files.")
    target.mkdir(parents=True, exist_ok=True)
    with music_backend._setup_lock():
        python = install()
    script = (
        "import os,sys; from pathlib import Path; "
        "import imageio_ffmpeg; "
        "os.environ['PATH']=str(Path(imageio_ffmpeg.get_ffmpeg_exe()).parent)"
        "+os.pathsep+os.environ.get('PATH',''); "
        "from basic_pitch.inference import predict_and_save; "
        "from basic_pitch import ICASSP_2022_MODEL_PATH; "
        "predict_and_save([sys.argv[1]],sys.argv[2],True,False,False,True,ICASSP_2022_MODEL_PATH)"
    )
    # Failed inference or CSV validation must not leave partial outputs that
    # prevent retrying with the same otherwise-empty destination.
    with tempfile.TemporaryDirectory(prefix=".cale-midi-", dir=target) as directory:
        staging = Path(directory)
        result = subprocess.run(
            [str(python), "-c", script, str(source), str(staging)],
            capture_output=True, text=True, timeout=3600, creationflags=HIDE,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout)[-1200:] or "Audio-to-MIDI failed.")
        midi = sorted(
            path for path in staging.glob("*.mid")
            if path.is_file() and path.stat().st_size > 0
        )
        if not midi:
            raise RuntimeError("Basic Pitch finished without a MIDI file.")
        for notes in staging.glob("*.csv"):
            normalize_note_events(notes)
        files = sorted(path for path in staging.iterdir() if path.is_file() and path.stat().st_size > 0)
        published = []
        try:
            for path in files:
                destination = target / path.name
                # Staging is on this filesystem. A link publishes complete
                # bytes exclusively instead of overwriting a concurrent file.
                os.link(path, destination)
                published.append((path, destination))
        except BaseException:
            for staged, destination in reversed(published):
                try:
                    if destination.samefile(staged):
                        destination.unlink()
                except OSError:
                    pass  # Preserve a replacement or an inaccessible file.
            raise
        midi = [target / path.name for path in midi]
        files = [target / path.name for path in files]
    return {"source": str(source), "output_dir": str(target),
            "midi_path": str(midi[0]), "files": [str(path) for path in files],
            "model": "Spotify Basic Pitch"}
