"""Local design helpers keep source findings and color math inspectable."""

import json

import pytest

from design_tools import add_astro_ai_ui, check_color_contrast, luminance


def test_black_and_white_have_maximum_contrast():
    result = check_color_contrast("#000000", "#FFFFFF")
    assert result["ratio"] == 21
    assert result["normal_text_AAA"] is True


def test_same_color_fails_text_contrast():
    result = check_color_contrast("#444444", "#444444")
    assert result["ratio"] == 1
    assert result["normal_text_AA"] is False


def test_rejects_non_hex_color():
    with pytest.raises(ValueError):
        luminance("red")


def test_installs_local_ai_components_into_astro_project(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"astro": "^5"}}), encoding="utf-8")
    first = add_astro_ai_ui(str(tmp_path))
    assert first["files_added"] > 15
    assert first["components"] >= 15
    assert (tmp_path / "src/vendor/ai-ui/components/ai-ui/Message.astro").is_file()
    assert (tmp_path / "src/vendor/ai-ui/LICENSE").is_file()
    assert add_astro_ai_ui(str(tmp_path))["files_added"] == 0


def test_rejects_non_astro_project(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "*"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Astro"):
        add_astro_ai_ui(str(tmp_path))


def test_conflict_does_not_copy_other_files(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"astro": "^5"}}), encoding="utf-8")
    existing = tmp_path / "src/vendor/ai-ui/components/ai-ui/Message.astro"
    existing.parent.mkdir(parents=True)
    existing.write_text("custom component", encoding="utf-8")
    with pytest.raises(FileExistsError):
        add_astro_ai_ui(str(tmp_path))
    assert existing.read_text(encoding="utf-8") == "custom component"
    assert not (tmp_path / "src/vendor/ai-ui/LICENSE").exists()


def test_3d_scene_rejects_unbounded_or_invalid_geometry():
    import media_worker

    for objects in ([], [{"type": "code", "python": "print('no')"}],
                    [{"type": "box", "size": [1, -1, 1]}],
                    [{"type": "sphere", "position": [float("nan"), 0, 0]}],
                    [{"type": "box", "color": [0, 0, 0, 999]}],
                    [{"type": "box"}] * 65):
        with pytest.raises(ValueError):
            media_worker.validate_3d_objects(objects)
    assert media_worker.validate_3d_objects([{"type": "cone"}])[0]["rotation"] == [0, 0, 0]


def test_two_small_gpus_do_not_qualify_as_one_large_gpu():
    from types import SimpleNamespace
    from unittest.mock import patch

    import media_worker

    with patch("psutil.virtual_memory", return_value=SimpleNamespace(total=32 * 1024**3)), \
         patch.object(media_worker.subprocess, "run", return_value=SimpleNamespace(stdout="0,4096\n1,4096\n")):
        with pytest.raises(RuntimeError, match="one NVIDIA GPU"):
            media_worker.three_hardware()
    with patch("psutil.virtual_memory", return_value=SimpleNamespace(total=32 * 1024**3)), \
         patch.object(media_worker.subprocess, "run", return_value=SimpleNamespace(stdout="0,4096\n1,12288\n")):
        assert media_worker.three_hardware()["gpu_index"] == 1
    assert media_worker.three_hardware(False) == {"device": "cpu"}


def test_3d_overwrite_is_rejected_before_install_or_launch(tmp_path):
    from unittest.mock import patch

    import media_worker

    output = tmp_path / "existing.glb"
    output.write_bytes(b"original")
    with patch.object(media_worker.subprocess, "Popen") as launch:
        with pytest.raises(ValueError, match="new GLB"):
            media_worker.start_3d_job(str(output), objects=[{"type": "box"}])
        launch.assert_not_called()
    assert output.read_bytes() == b"original"


def test_3d_cpu_job_is_durable_and_does_not_require_gpu(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import patch

    import media_worker

    monkeypatch.setenv("CALE_3D_HOME", str(tmp_path / "runtime"))
    with patch.object(media_worker.subprocess, "Popen", return_value=SimpleNamespace(pid=123)), \
         patch.object(media_worker, "three_hardware", return_value={"device": "cpu"}) as hardware:
        result = media_worker.start_3d_job(str(tmp_path / "ship.glb"), objects=[{"type": "cone"}])
    hardware.assert_called_once_with(False)
    saved = json.loads(media_worker.three_job_path(result["job_id"]).read_text())
    assert saved["engine"] == "trimesh" and saved["objects"][0]["type"] == "cone"
    assert media_worker.three_job_status(result["job_id"])["state"] == "queued"
    with pytest.raises(ValueError, match="Invalid 3D"):
        media_worker.three_job_path("../outside")


def test_changed_3d_deliverable_does_not_keep_reporting_success(tmp_path, monkeypatch):
    import media_worker

    monkeypatch.setenv("CALE_3D_HOME", str(tmp_path))
    output = tmp_path / "ship.glb"
    output.write_bytes(b"verified mesh bytes")
    job_id = "a" * 32
    media_worker.three_write(job_id, state="complete", output_path=str(output),
        mesh={"sha256": media_worker._three_digest(output)})
    assert media_worker.three_job_status(job_id)["state"] == "complete"
    output.write_bytes(b"changed")
    assert media_worker.three_job_status(job_id)["state"] == "failed"


def test_3d_cancellation_never_terminates_a_reused_pid(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    import media_worker

    monkeypatch.setenv("CALE_3D_HOME", str(tmp_path))
    job_id = "b" * 32
    media_worker.three_write(job_id, state="generating", worker_pid=123, worker_started=10)
    process = MagicMock()
    process.create_time.return_value = 20
    with patch("psutil.Process", return_value=process):
        with pytest.raises(ValueError, match="no longer belongs"):
            media_worker.cancel_3d_job(job_id)
    process.terminate.assert_not_called()


def test_3d_cancellation_records_state_before_terminating_owned_worker(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    import media_worker

    monkeypatch.setenv("CALE_3D_HOME", str(tmp_path))
    job_id = "c" * 32
    media_worker.three_write(job_id, state="generating", worker_pid=123, worker_started=10)
    process = MagicMock()
    process.create_time.return_value = 10
    process.cmdline.return_value = ["python", media_worker.__file__, "--3d-job", job_id]
    process.children.return_value = []
    recorded = []
    process.terminate.side_effect = lambda: recorded.append(json.loads(media_worker.three_job_path(job_id).read_text())["state"])
    with patch("psutil.Process", return_value=process), patch("psutil.wait_procs", return_value=([], [])):
        result = media_worker.cancel_3d_job(job_id)
    assert recorded == ["cancelled"] and result["state"] == "cancelled"


def test_3d_job_follows_only_its_own_task_pause_stop_or_deletion(tmp_path, monkeypatch):
    import sqlite3

    import media_worker

    monkeypatch.setattr(media_worker, "ROOT", tmp_path)
    database = tmp_path / "LongTasks/projects.db"
    database.parent.mkdir()
    task_id = "a" * 12
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE projects(id TEXT,status TEXT)")
        connection.executemany("INSERT INTO projects VALUES(?,?)", [(task_id, "working"), ("b" * 12, "stopped")])
    assert not media_worker.three_task_stopped(task_id)
    assert not media_worker.three_task_stopped("")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE projects SET status='paused' WHERE id=?", (task_id,))
    assert media_worker.three_task_stopped(task_id)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM projects WHERE id=?", (task_id,))
    assert media_worker.three_task_stopped(task_id)


def test_cancelled_3d_job_cannot_be_resurrected_by_late_render_progress(tmp_path, monkeypatch):
    import media_worker

    monkeypatch.setenv("CALE_3D_HOME", str(tmp_path))
    job_id = "d" * 32
    media_worker.three_write(job_id, state="generating")
    media_worker.three_write(job_id, state="cancelled")
    media_worker.three_write(job_id, state="complete", progress_percent=100)
    assert media_worker.three_job_status(job_id)["state"] == "cancelled"
