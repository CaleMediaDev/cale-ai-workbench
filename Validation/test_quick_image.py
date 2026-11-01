"""A direct image request saves a real verified PNG in the user's project area."""

import json
import subprocess
import tempfile
from io import BytesIO
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
            assert request["prompt"] == "a recognizable brown dog"
            assert 0 <= request["seed"] < 2**31
            path = Path(request["output_path"])
            Image.new("RGB", (512, 512), "orange").save(path)
            return subprocess.CompletedProcess(args, 0, json.dumps({"path": str(path)}), "")

        with (patch.object(quick_image.subprocess, "run", side_effect=worker),
              patch.object(quick_image, "clarify_prompt", return_value="a recognizable brown dog")):
            result = quick_image.generate(root, "a dog")
        assert result.parent == root / "Projects/Quick Images"
        with Image.open(result) as image:
            assert image.size == (512, 512)


def test_prompt_clarification_resolves_ambiguous_subject_locally():
    reply = {"message": {"content": "A small red diamond-shaped kite with a string against a clear blue sky."}}
    with patch.object(quick_image, "urlopen", return_value=BytesIO(json.dumps(reply).encode())) as opened:
        result = quick_image.clarify_prompt("a small red kite against a clear blue sky")
    assert "diamond-shaped kite" in result
    assert opened.call_args.args[0].full_url == "http://127.0.0.1:11434/api/chat"


def test_prompt_clarification_falls_back_if_model_changes_subject_or_is_offline():
    prompt = "a small red kite against a clear blue sky"
    reply = {"message": {"content": "A brown bird flying over a lake."}}
    with patch.object(quick_image, "urlopen", return_value=BytesIO(json.dumps(reply).encode())):
        assert quick_image.clarify_prompt(prompt) == prompt
    with patch.object(quick_image, "urlopen", side_effect=OSError("offline")):
        assert quick_image.clarify_prompt(prompt) == prompt
