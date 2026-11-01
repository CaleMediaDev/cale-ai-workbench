"""A direct image request saves a real verified PNG in the user's project area."""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import quick_image


def test_direct_image_uses_local_worker_and_checks_saved_file():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        python = root / "Media/python/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"")

        def worker(*args, **kwargs):
            request = json.loads(kwargs["input"])
            assert request["action"] == "generate_image"
            assert request["prompt"] == "a dog"
            path = Path(request["output_path"])
            Image.new("RGB", (512, 512), "orange").save(path)
            return subprocess.CompletedProcess(args, 0, json.dumps({"path": str(path)}), "")

        with patch.object(quick_image.subprocess, "run", side_effect=worker):
            result = quick_image.generate(root, "a dog")
        assert result.parent == root / "Projects/Quick Images"
        with Image.open(result) as image:
            assert image.size == (512, 512)
