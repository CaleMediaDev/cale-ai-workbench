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


def test_direct_image_rejects_valid_checksums_but_invalid_pixels(tmp_path):
    import struct
    import zlib

    import pytest

    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    output = tmp_path / "broken.png"

    def worker(*args, **kwargs):
        request = json.loads(kwargs["input"])
        path = Path(request["output_path"])
        Image.new("RGB", (64, 64), "orange").save(path)
        data = bytearray(path.read_bytes())
        offset = 8
        while offset < len(data):
            length = struct.unpack_from(">I", data, offset)[0]
            if data[offset + 4:offset + 8] == b"IDAT":
                data[offset + 8:offset + 10] = b"\x00\x00"
                checksum = zlib.crc32(data[offset + 4:offset + 8 + length])
                struct.pack_into(">I", data, offset + 8 + length, checksum)
                break
            offset += length + 12
        else:
            raise AssertionError("Expected an IDAT chunk")
        path.write_bytes(data)
        # Metadata and chunk verification alone do not detect this corruption.
        with Image.open(path) as picture:
            picture.verify()
        return subprocess.CompletedProcess(args, 0, json.dumps({"path": str(path)}), "")

    with (patch.object(quick_image.subprocess, "run", side_effect=worker),
          patch.object(quick_image, "clarify_prompt", return_value="a dog")):
        with pytest.raises(OSError):
            quick_image.generate(tmp_path, "a dog", str(output))


def test_non_png_deliverable_is_rejected_and_retried_on_recovery(tmp_path):
    import pytest

    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    output = tmp_path / "image.png"

    def worker(*args, **kwargs):
        request = json.loads(kwargs["input"])
        path = Path(request["output_path"])
        Image.new("RGB", (64, 64), "orange").save(path, format="JPEG")
        return subprocess.CompletedProcess(args, 0, json.dumps({"path": str(path)}), "")

    with (patch.object(quick_image.subprocess, "run", side_effect=worker),
          patch.object(quick_image, "clarify_prompt", return_value="a dog")):
        with pytest.raises(ValueError, match="did not save a PNG image"):
            quick_image.generate(tmp_path, "a dog", str(output))

    # The file has decodable pixels, but is not the requested PNG deliverable.
    with Image.open(output) as picture:
        assert picture.format == "JPEG"
        picture.load()
    assert not quick_image.image_file_ok(output)

    job_id = "d" * 32
    quick_image._write_job(tmp_path, job_id, {
        "job_id": job_id, "status": "working", "prompt": "a dog",
        "output_path": str(output),
    })
    with (patch.object(quick_image, "_worker_running", return_value=False),
          patch.object(quick_image, "_launch_job") as launch):
        quick_image.recover_image_jobs(tmp_path)
        launch.assert_called_once_with(tmp_path, job_id)
    assert quick_image.image_job_status(tmp_path, job_id)["status"] == "queued"
    assert not output.exists()


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


def test_direct_image_job_survives_starting_process_and_verifies_pixels(tmp_path):
    python = tmp_path / "Toolbox/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"launcher")
    output = tmp_path / "project/image.png"
    with patch.object(quick_image.subprocess, "Popen") as process:
        started = quick_image.start_image_job(tmp_path, "a dog", str(output))
        process.assert_called_once()
    job_id = started["job_id"]
    assert quick_image.image_job_status(tmp_path, job_id)["status"] == "queued"
    output.parent.mkdir()
    Image.new("RGB", (64, 64), "orange").save(output)
    assert quick_image.image_file_ok(output)
    with patch.object(quick_image, "_worker_running", return_value=False), patch.object(
        quick_image, "_launch_job"
    ) as relaunch:
        quick_image.recover_image_jobs(tmp_path)
        relaunch.assert_not_called()
    # Fresh queued jobs are allowed 30 seconds to start before recovery.
    info = quick_image.image_job_status(tmp_path, job_id)
    info["created"] = 1
    quick_image._write_job(tmp_path, job_id, info)
    with patch.object(quick_image, "_worker_running", return_value=False):
        quick_image.recover_image_jobs(tmp_path)
    assert quick_image.image_job_status(tmp_path, job_id)["status"] == "complete"


def test_recovery_retries_png_with_valid_checksums_but_invalid_pixels(tmp_path):
    import struct
    import zlib

    output = tmp_path / "broken.png"
    Image.new("RGB", (64, 64), "orange").save(output)
    data = bytearray(output.read_bytes())
    offset = 8
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        if data[offset + 4:offset + 8] == b"IDAT":
            # Break the zlib header while retaining a valid PNG chunk checksum.
            data[offset + 8:offset + 10] = b"\x00\x00"
            checksum = zlib.crc32(data[offset + 4:offset + 8 + length])
            struct.pack_into(">I", data, offset + 8 + length, checksum)
            break
        offset += length + 12
    else:
        raise AssertionError("Expected an IDAT chunk")
    output.write_bytes(data)
    assert output.stat().st_size >= 100
    with Image.open(output) as picture:
        picture.verify()
    assert not quick_image.image_file_ok(output)

    job_id = "c" * 32
    quick_image._write_job(tmp_path, job_id, {
        "job_id": job_id, "status": "working", "prompt": "a dog",
        "output_path": str(output),
    })
    with patch.object(quick_image, "_worker_running", return_value=False), patch.object(
        quick_image, "_launch_job"
    ) as launch:
        quick_image.recover_image_jobs(tmp_path)
        launch.assert_called_once_with(tmp_path, job_id)
    assert quick_image.image_job_status(tmp_path, job_id)["status"] == "queued"
    assert not output.exists()


def test_image_job_pause_and_resume_reuse_same_id(tmp_path):
    job_id = "a" * 32
    quick_image._write_job(tmp_path, job_id, {
        "job_id": job_id, "status": "working", "current": "Generating",
        "prompt": "a dog", "output_path": str(tmp_path / "dog.png"),
    })
    with patch.object(quick_image, "_worker_running", return_value=False), patch.object(
        quick_image, "_launch_job"
    ) as launch:
        assert quick_image.hold_image_job(tmp_path, job_id, paused=True)["status"] == "paused"
        quick_image.recover_image_jobs(tmp_path)
        launch.assert_not_called()
        assert quick_image.resume_image_job(tmp_path, job_id)["status"] == "queued"
        launch.assert_called_once_with(tmp_path, job_id)


def test_missing_toolbox_runtime_marks_started_job_as_error(tmp_path):
    import pytest

    with pytest.raises(RuntimeError, match="Toolbox runtime is missing"):
        quick_image.start_image_job(tmp_path, "a dog", str(tmp_path / "dog.png"))

    jobs = list((tmp_path / "LongTasks/image-jobs").glob("*.json"))
    assert len(jobs) == 1
    job_id = jobs[0].stem
    info = quick_image.image_job_status(tmp_path, job_id)
    assert info["status"] == "error"
    assert "Toolbox runtime is missing" in info["current"]

    # Age the record so the queued-job grace period cannot hide a retry.
    info["created"] = 1
    quick_image._write_job(tmp_path, job_id, info)
    with patch.object(quick_image, "_launch_job") as launch:
        quick_image.recover_image_jobs(tmp_path)
        launch.assert_not_called()
    assert quick_image.image_job_status(tmp_path, job_id)["status"] == "error"


def test_hold_image_job_handles_access_denied(tmp_path):
    import psutil

    job_id = "b" * 32
    quick_image._write_job(tmp_path, job_id, {
        "job_id": job_id, "status": "working", "current": "Generating",
        "prompt": "a dog", "output_path": str(tmp_path / "dog.png"),
        "worker_pid": 12345,
    })
    with patch.object(quick_image, "_worker_running", return_value=True), \
         patch("psutil.Process", side_effect=psutil.AccessDenied("denied")):
        try:
            quick_image.hold_image_job(tmp_path, job_id)
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "Permission denied" in str(e)
