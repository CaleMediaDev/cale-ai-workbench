"""Local image worker diagnostics must not discard a successful PNG result."""

import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

import quick_image


@pytest.mark.parametrize("progress", ["Progress: \U0001f4dd\n".encode("utf-8"), b"legacy progress: \x81\n"])
def test_worker_progress_survives_legacy_windows_pipe_encoding(tmp_path, monkeypatch, progress):
    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    worker = tmp_path / "media_worker.py"
    worker.write_text(
        "import json, sys\n"
        "from PIL import Image\n"
        "request = json.load(sys.stdin)\n"
        "Image.new('RGB', (32, 32), 'orange').save(request['output_path'])\n"
        f"sys.stdout.buffer.write({progress!r})\n"
        "sys.stdout.buffer.write(json.dumps({'path': request['output_path']}).encode('ascii') + b'\\n')\n",
        encoding="utf-8",
    )
    original_run = subprocess.run

    def local_worker(command, **kwargs):
        assert Path(command[1]) == worker
        return original_run([sys.executable, str(worker)], **kwargs)

    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp1252")
    monkeypatch.setattr(quick_image.subprocess, "run", local_worker)
    monkeypatch.setattr(quick_image, "clarify_prompt", lambda text: text)
    output = quick_image.generate(tmp_path, "an orange square")
    with Image.open(output) as picture:
        assert picture.size == (32, 32)
        picture.load()


def test_worker_failure_keeps_unicode_diagnostic(tmp_path, monkeypatch):
    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    worker = tmp_path / "media_worker.py"
    worker.write_text(
        "import sys\n"
        "sys.stdin.read()\n"
        "print('{}', flush=True)\n"
        "sys.stderr.write('Image failed: \\u0410')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    original_run = subprocess.run

    def local_worker(command, **kwargs):
        return original_run([sys.executable, str(worker)], **kwargs)

    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp1252")
    monkeypatch.setattr(quick_image.subprocess, "run", local_worker)
    monkeypatch.setattr(quick_image, "clarify_prompt", lambda text: text)
    with pytest.raises(RuntimeError, match="Image failed: \u0410"):
        quick_image.generate(tmp_path, "an orange square")
