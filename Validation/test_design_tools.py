"""Local design helpers keep source findings and color math inspectable."""

import json
import os
import subprocess

import pytest

from design_tools import add_astro_ai_ui, check_color_contrast, luminance


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_astro_kit_does_not_write_through_junction_parent(tmp_path, monkeypatch):
    import design_tools

    kit = tmp_path / "kit"
    (kit / "src").mkdir(parents=True)
    (kit / "src/Message.astro").write_text("component", encoding="utf-8")
    (kit / "LICENSE").write_text("fixture license", encoding="utf-8")
    monkeypatch.setattr(design_tools, "AI_UI_KIT", kit)
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text('{"dependencies":{"astro":"*"}}', encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    kept = outside / "keep.txt"
    kept.write_bytes(b"unrelated content")
    junction = project / "src"
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    with pytest.raises(FileExistsError, match="no files were changed"):
        add_astro_ai_ui(str(project))
    assert junction.is_junction()
    assert list(outside.iterdir()) == [kept]
    assert kept.read_bytes() == b"unrelated content"


def test_space_shooter_is_new_offline_file_with_safe_title_and_no_overwrite(tmp_path):
    import design_tools

    page = tmp_path / "game.html"
    title = "__CONFIG__ </title><script>window.injected=true</script>"
    created = design_tools.create_space_shooter(str(page), title=title)
    source = page.read_text(encoding="utf-8")
    assert created["tests_run"] is False and created["bytes"] == len(page.read_bytes())
    assert "__CONFIG__ &lt;/title&gt;" in source and "<script>window.injected" not in source
    assert "https://" not in source and "http://" not in source
    before = page.read_bytes()
    with pytest.raises(FileExistsError):
        design_tools.create_space_shooter(str(page))
    assert page.read_bytes() == before
    for arguments in ({"title": ""}, {"title": "x" * 81}, {"title": "line\nbreak"}, {"accent": "red"}):
        target = tmp_path / "invalid.html"
        with pytest.raises(ValueError):
            design_tools.create_space_shooter(str(target), **arguments)
        assert not target.exists()
    with pytest.raises(ValueError):
        design_tools.create_space_shooter(str(tmp_path / "game.exe"))


@pytest.mark.parametrize("title", ["Flight \ud800", "Flight \udfff"])
def test_space_shooter_rejects_surrogate_title_before_creating_output(tmp_path, title):
    import design_tools

    page = tmp_path / "new-project" / "game.html"
    with pytest.raises(ValueError, match="printable game title"):
        design_tools.create_space_shooter(str(page), title=title)

    assert not page.exists()
    assert not page.parent.exists()
    design_tools.create_space_shooter(str(page), title="Valid flight")
    assert "Valid flight" in page.read_text(encoding="utf-8")


def test_space_shooter_actual_engine_passes_logic_and_extra_collision_boundaries(tmp_path):
    import re

    import design_tools
    import model_fitness

    page = tmp_path / "game.html"
    design_tools.create_space_shooter(str(page))
    source = page.read_text(encoding="utf-8")
    checks = model_fitness.grade_game_html(source, design_tools.ROOT)
    assert len(checks) == 13 and all(checks.values())
    extra = r'''
const g=window.game,checks={};
g.reset();g.waveSpawned=false;
const edgeEnemy={x:100,y:100,w:30,h:30,speed:0,hp:1};
g.enemies=[edgeEnemy];g.bullets=[{x:117,y:100,w:4,h:16,speed:0}];g.update(0,{});
checks.edge_has_no_damage=g.score===0&&edgeEnemy.hp===1;
g.reset();g.waveSpawned=false;
const victim={x:100,y:100,w:30,h:30,speed:0};g.enemies=[victim];
g.bullets=[{x:100,y:100,w:4,h:16,speed:0},{x:100,y:100,w:4,h:16,speed:0}];g.update(0,{});
checks.one_enemy_one_score=g.score===10;
g.reset();g.waveSpawned=false;g.enemies=[{x:100,y:100,w:30,h:30,speed:0,hp:2}];
g.bullets=[{x:100,y:100,w:4,h:16,speed:0}];g.update(0,{});
checks.armored_hit=g.score===0&&g.enemies[0].hp===1;
g.bullets=[{x:100,y:100,w:4,h:16,speed:0}];g.update(0,{});checks.armored_kill=g.score===10;
g.reset();g.waveSpawned=false;g.enemies=[{x:g.player.x,y:g.player.y,w:30,h:30,speed:0}];g.update(0,{});
checks.shield_time=g.player.invincible>=1;g.update(1.3,{});
g.enemies=[{x:g.player.x,y:g.player.y,w:30,h:30,speed:0}];g.update(0,{});checks.shield_expires=g.lives===1;
g.reset();g.waveSpawned=false;g.enemies=[{x:100,y:820,w:30,h:30,speed:0}];g.update(0,{});checks.breach_costs_life=g.lives===2;
g.reset();g.waveSpawned=true;g.score=7;g.enemies=[];g.update(0,{});checks.wave_not_score_modulo=g.wave===2;
return JSON.stringify(checks);
'''
    code = model_fitness.GAME_DOM + "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", source, re.S))
    additional = model_fitness.grade_game_code(code, design_tools.ROOT, extra)
    assert len(additional) == 8 and all(additional.values())


def test_space_shooter_browser_layout_keyboard_touch_pause_and_blur(tmp_path):
    from playwright.sync_api import sync_playwright

    import design_tools

    page_path = tmp_path / "game.html"
    design_tools.create_space_shooter(str(page_path))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        for width, height in ((375, 812), (375, 600), (1280, 900), (900, 600)):
            page.set_viewport_size({"width": width, "height": height})
            page.goto(page_path.as_uri())
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth&&document.documentElement.scrollHeight<=innerHeight")
        page.set_viewport_size({"width": 375, "height": 812})
        page.click("#restart")
        before = page.evaluate("game.player.x")
        right = page.locator("#right").bounding_box()
        page.mouse.move(right["x"] + right["width"] / 2, right["y"] + right["height"] / 2)
        page.mouse.down()
        page.wait_for_timeout(180)
        page.mouse.up()
        assert page.evaluate("game.player.x") > before + 5
        page.click("#pause")
        before = page.evaluate("game.player.x")
        page.keyboard.down("ArrowRight")
        page.wait_for_timeout(100)
        page.keyboard.up("ArrowRight")
        assert page.evaluate("game.player.x") == before
        page.click("#pause")
        page.keyboard.down("ArrowRight")
        page.wait_for_timeout(100)
        page.evaluate("dispatchEvent(new Event('blur'))")
        before = page.evaluate("game.player.x")
        page.wait_for_timeout(100)
        page.keyboard.up("ArrowRight")
        assert page.evaluate("game.player.x") == before
        page.keyboard.press("Enter")
        page.keyboard.down("Space")
        page.wait_for_timeout(80)
        assert page.evaluate("game.bullets.length") > 0
        page.keyboard.up("Space")
        assert not errors
        browser.close()


def test_touch_control_remains_held_until_last_pointer_releases(tmp_path):
    from playwright.sync_api import sync_playwright

    import design_tools

    page_path = tmp_path / "game.html"
    design_tools.create_space_shooter(str(page_path))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.add_init_script("window.requestAnimationFrame=()=>0;")
            page.goto(page_path.as_uri())
            result = page.evaluate("""() => {
                const button=document.getElementById('right');
                // Synthetic pointers do not have native pointer capture.
                button.setPointerCapture=()=>{};
                const send=(type,pointerId)=>button.dispatchEvent(
                    new PointerEvent(type,{pointerId,bubbles:true}));
                game.reset();
                send('pointerdown',1);
                send('pointerdown',2);
                send('pointerup',1);
                // Capture loss for an already released pointer is harmless.
                send('lostpointercapture',1);
                const start=game.player.x;
                frame(previous+50);
                const afterFirstRelease=game.player.x;
                send('pointercancel',2);
                frame(previous+50);
                return {start,afterFirstRelease,afterLastRelease:game.player.x};
            }""")
            assert result["afterFirstRelease"] > result["start"]
            assert result["afterLastRelease"] == result["afterFirstRelease"]
        finally:
            browser.close()


def test_default_starter_verifier_tests_actual_browser_physics_and_rejects_changed_mechanics(tmp_path):
    import design_tools

    page = tmp_path / "game.html"
    design_tools.create_space_shooter(str(page))
    source = page.read_text(encoding="utf-8")
    correct = design_tools.verify_space_shooter(str(page))
    assert correct["passed"] and len(correct["logic"]) == 18 and correct["browser"]["passed"]
    assert page.read_text(encoding="utf-8") == source
    page.write_text(source.replace("this.player.invincible=1.2", "this.player.invincible=0"), encoding="utf-8")
    broken = design_tools.verify_space_shooter(str(page))
    assert not broken["passed"] and broken["logic"]["damage"] is False


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


@pytest.mark.parametrize("package", [
    None,
    [],
    {"dependencies": None},
    {"dependencies": ["astro"]},
    {"dependencies": {"astro": "^5"}, "devDependencies": None},
    {"dependencies": {"astro": "^5"}, "peerDependencies": "astro"},
])
def test_rejects_invalid_manifest_structure_before_copy(tmp_path, package):
    (tmp_path / "package.json").write_text(json.dumps(package), encoding="utf-8")

    with pytest.raises(ValueError, match="package.json"):
        add_astro_ai_ui(str(tmp_path))

    assert not (tmp_path / "src").exists()


def test_installs_kit_with_bom_prefixed_package_manifest(tmp_path, monkeypatch):
    import design_tools

    kit = tmp_path / "kit"
    (kit / "src").mkdir(parents=True)
    (kit / "src/Message.astro").write_text("<p>Message</p>", encoding="utf-8")
    (kit / "LICENSE").write_text("fixture license", encoding="utf-8")
    monkeypatch.setattr(design_tools, "AI_UI_KIT", kit)

    project = tmp_path / "project"
    project.mkdir()
    manifest = project / "package.json"
    manifest.write_bytes(
        b'\xef\xbb\xbf' + json.dumps({"dependencies": {"astro": "^5"}}).encode("utf-8")
    )
    original_manifest = manifest.read_bytes()

    result = add_astro_ai_ui(str(project))

    destination = project / "src/vendor/ai-ui"
    assert result["files_added"] == 2
    assert (destination / "Message.astro").read_text(encoding="utf-8") == "<p>Message</p>"
    assert (destination / "LICENSE").read_text(encoding="utf-8") == "fixture license"
    assert add_astro_ai_ui(str(project))["files_added"] == 0
    assert manifest.read_bytes() == original_manifest


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


def test_blocked_kit_parent_does_not_copy_earlier_files(tmp_path, monkeypatch):
    import design_tools

    kit = tmp_path / "kit"
    (kit / "src/z").mkdir(parents=True)
    (kit / "src/a.astro").write_text("early component", encoding="utf-8")
    (kit / "src/z/component.astro").write_text("nested component", encoding="utf-8")
    (kit / "LICENSE").write_text("fixture license", encoding="utf-8")
    monkeypatch.setattr(design_tools, "AI_UI_KIT", kit)

    project = tmp_path / "project"
    destination = project / "src/vendor/ai-ui"
    destination.mkdir(parents=True)
    (project / "package.json").write_text(
        json.dumps({"dependencies": {"astro": "^5"}}), encoding="utf-8"
    )
    blocker = destination / "z"
    blocker.write_text("keep this file", encoding="utf-8")

    with pytest.raises(FileExistsError, match="no files were changed"):
        add_astro_ai_ui(str(project))

    assert blocker.read_text(encoding="utf-8") == "keep this file"
    assert not (destination / "a.astro").exists()
    assert not (destination / "LICENSE").exists()
    assert list(destination.iterdir()) == [blocker]


def test_dangling_kit_parent_symlink_is_rejected_before_copy(tmp_path, monkeypatch):
    import design_tools

    kit = tmp_path / "kit"
    (kit / "src/z").mkdir(parents=True)
    (kit / "src/a.astro").write_text("early component", encoding="utf-8")
    (kit / "src/z/component.astro").write_text("nested component", encoding="utf-8")
    (kit / "LICENSE").write_text("fixture license", encoding="utf-8")
    monkeypatch.setattr(design_tools, "AI_UI_KIT", kit)

    project = tmp_path / "project"
    destination = project / "src/vendor/ai-ui"
    destination.mkdir(parents=True)
    (project / "package.json").write_text(
        json.dumps({"dependencies": {"astro": "^5"}}), encoding="utf-8"
    )
    outside = tmp_path / "missing-directory"
    link = destination / "z"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symlinks is not supported in this environment")

    with pytest.raises(FileExistsError, match="no files were changed"):
        add_astro_ai_ui(str(project))

    assert link.is_symlink()
    assert link.readlink() == outside
    assert not outside.exists()
    assert not (destination / "a.astro").exists()
    assert not (destination / "LICENSE").exists()
    assert list(destination.iterdir()) == [link]


def test_existing_kit_parent_symlink_is_rejected_before_copy(tmp_path, monkeypatch):
    import design_tools

    kit = tmp_path / "kit"
    (kit / "src/z").mkdir(parents=True)
    (kit / "src/a.astro").write_text("early component", encoding="utf-8")
    (kit / "src/z/component.astro").write_text("nested component", encoding="utf-8")
    (kit / "LICENSE").write_text("fixture license", encoding="utf-8")
    monkeypatch.setattr(design_tools, "AI_UI_KIT", kit)

    project = tmp_path / "project"
    destination = project / "src/vendor/ai-ui"
    destination.mkdir(parents=True)
    (project / "package.json").write_text(
        json.dumps({"dependencies": {"astro": "^5"}}), encoding="utf-8"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    link = destination / "z"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symlinks is not supported in this environment")

    with pytest.raises(FileExistsError, match="no files were changed"):
        add_astro_ai_ui(str(project))

    assert link.is_symlink()
    assert link.readlink() == outside
    assert list(outside.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert not (destination / "a.astro").exists()
    assert not (destination / "LICENSE").exists()
    assert list(destination.iterdir()) == [link]


def test_dangling_kit_destination_symlink_is_rejected_before_copy(tmp_path, monkeypatch):
    import design_tools

    kit = tmp_path / "kit"
    (kit / "src").mkdir(parents=True)
    (kit / "src/a.astro").write_text("early component", encoding="utf-8")
    (kit / "src/z.astro").write_text("later component", encoding="utf-8")
    (kit / "LICENSE").write_text("fixture license", encoding="utf-8")
    monkeypatch.setattr(design_tools, "AI_UI_KIT", kit)

    project = tmp_path / "project"
    destination = project / "src/vendor/ai-ui"
    destination.mkdir(parents=True)
    (project / "package.json").write_text(
        json.dumps({"dependencies": {"astro": "^5"}}), encoding="utf-8"
    )
    outside = tmp_path / "outside.astro"
    link = destination / "z.astro"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symlinks is not supported in this environment")

    with pytest.raises(FileExistsError, match="no files were changed"):
        add_astro_ai_ui(str(project))

    assert link.is_symlink()
    assert link.readlink() == outside
    assert not outside.exists()
    assert not (destination / "a.astro").exists()
    assert not (destination / "LICENSE").exists()
    assert list(destination.iterdir()) == [link]


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



def test_game_checker_matches_requested_lives_and_rejects_default_regression(tmp_path):
    import design_tools

    path = tmp_path / "index.html"
    design_tools.create_space_shooter(str(path))
    assert not design_tools.verify_space_shooter(str(path), 5)["passed"]
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("lives:3", "lives:5").replace("this.lives=3", "this.lives=5"), encoding="utf-8")
    report = design_tools.verify_space_shooter(str(path), 5)
    assert report["passed"] and report["starting_lives"] == 5
    assert len(report["logic"]) == 18
    assert not design_tools.verify_space_shooter(str(path), 3)["passed"]
