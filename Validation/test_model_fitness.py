"""Hardware and measured throughput must both qualify a local agent model."""

import io
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_fitness
import model_library


def test_avatar_headroom_requires_matching_live_process_and_preserves_physical_gpu(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import psutil

    state = tmp_path / "LongTasks"
    state.mkdir()
    marker = state / "avatar-gpu-active-123.json"
    record = {"pid": 123, "started": 100.0, "reserved_bytes": 2 * 1024 ** 3,
              "phase": "active", "updated": 101.0}
    marker.write_text(json.dumps(record), encoding="utf-8")
    process = SimpleNamespace(is_running=lambda: True, create_time=lambda: 100.0,
        cmdline=lambda: ["python.exe", "-u", str(tmp_path / "camera_presence.py"), "--avatar"])
    monkeypatch.setattr(psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(model_fitness, "gpu_vram_gb", lambda: 12.0)
    assert model_fitness.avatar_reserved_vram_gb(tmp_path) == 2
    assert model_fitness.routing_vram_gb(tmp_path) == 10
    assert model_fitness.gpu_vram_gb() == 12
    # Stale file timestamps alone do not imply that its process stopped.
    record["updated"] = 0
    marker.write_text(json.dumps(record), encoding="utf-8")
    assert model_fitness.avatar_reserved_vram_gb(tmp_path) == 2
    process.create_time = lambda: 200.0
    assert model_fitness.routing_vram_gb(tmp_path) == 12
    process.create_time = lambda: 100.0
    process.cmdline = lambda: ["python.exe", str(tmp_path / "different.py"), "--avatar"]
    assert model_fitness.avatar_reserved_vram_gb(tmp_path) == 0
    process.cmdline = lambda: ["python.exe", "-I", "-B", str(tmp_path / "video_generation.py"), "avatar-profile-probe"]
    assert model_fitness.avatar_reserved_vram_gb(tmp_path) == 2
    process.cmdline = lambda: ["python.exe", "-c", "print('avatar-profile-probe')"]
    assert model_fitness.avatar_reserved_vram_gb(tmp_path) == 0
    process.cmdline = lambda: ["python.exe", "-I", "-B", str(tmp_path / "video_generation.py"), "avatar-profile-probe"]
    canonical = {**record, "reserved_bytes": 3 * 1024 ** 3}
    (state / "avatar-gpu-current.json").write_text(json.dumps(canonical), encoding="utf-8")
    assert model_fitness.avatar_reserved_vram_gb(tmp_path) == 3
    (state / "avatar-gpu-current.json").unlink()
    marker.write_text("[]", encoding="utf-8")
    assert model_fitness.avatar_reserved_vram_gb(tmp_path) == 0
    record["started"] = float("nan")
    marker.write_text(json.dumps(record), encoding="utf-8")
    assert model_fitness.avatar_reserved_vram_gb(tmp_path) == 0


def test_avatar_headroom_ignores_overflowing_timestamp_and_recovers(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import psutil

    state = tmp_path / "LongTasks"
    state.mkdir()
    record = {"pid": 123, "started": 100.0, "reserved_bytes": 2 * 1024 ** 3,
              "phase": "active"}
    process = SimpleNamespace(
        is_running=lambda: True,
        create_time=lambda: 100.0,
        cmdline=lambda: ["python.exe", str(tmp_path / "camera_presence.py"), "--avatar"],
    )
    monkeypatch.setattr(psutil, "Process", lambda _pid: process)
    (state / "avatar-gpu-active-123.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    current = state / "avatar-gpu-current.json"
    current.write_text(
        json.dumps({**record, "started": 10 ** 400}), encoding="utf-8"
    )
    # This is valid JSON below the marker size limit, but float() overflows.
    assert current.stat().st_size < 4096
    assert model_fitness.avatar_reserved_vram_gb(tmp_path) == 2
    current.write_text(
        json.dumps({**record, "reserved_bytes": 3 * 1024 ** 3}), encoding="utf-8"
    )
    assert model_fitness.avatar_reserved_vram_gb(tmp_path) == 3


def test_avatar_headroom_ignores_nested_json_and_recovers(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import psutil
    import pytest

    depth = sys.getrecursionlimit() + 100
    nested = "[" * depth + "0" + "]" * depth
    if len(nested) > 4096:
        pytest.skip("Decoder recursion limit exceeds the marker size limit")
    try:
        json.loads(nested)
    except RecursionError:
        pass
    else:
        pytest.skip("This JSON decoder accepts the bounded nested marker")

    state = tmp_path / "LongTasks"
    state.mkdir()
    record = {"pid": 123, "started": 100.0, "reserved_bytes": 2 * 1024 ** 3,
              "phase": "active"}
    process = SimpleNamespace(
        is_running=lambda: True,
        create_time=lambda: 100.0,
        cmdline=lambda: ["python.exe", str(tmp_path / "camera_presence.py"), "--avatar"],
    )
    monkeypatch.setattr(psutil, "Process", lambda _pid: process)
    (state / "avatar-gpu-active-123.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    current = state / "avatar-gpu-current.json"
    current.write_text(nested, encoding="utf-8")
    assert current.stat().st_size <= 4096
    assert model_fitness.avatar_reserved_vram_gb(tmp_path) == 2

    current.write_text(
        json.dumps({**record, "reserved_bytes": 3 * 1024 ** 3}), encoding="utf-8"
    )
    assert model_fitness.avatar_reserved_vram_gb(tmp_path) == 3


def test_27b_hardware_rejection_happens_before_download():
    import pytest

    with (
        patch.object(model_library, "total_ram_gb", return_value=128),
        patch.object(model_fitness, "gpu_vram_gb", return_value=12),
        patch.object(model_library, "urlopen") as download,
    ):
        with pytest.raises(ValueError, match="too slow"):
            model_library.install("qwen38_code")
    download.assert_not_called()


def test_remote_ollama_alias_cannot_run_local_game_benchmark():
    import pytest

    with patch.object(
        model_library, "post", return_value={"remote_host": "cloud.example"}
    ) as post:
        with pytest.raises(ValueError, match="physically installed"):
            model_fitness.probe_game_coding("remote", Path("missing"))
    post.assert_called_once()


def test_unproven_coding_candidate_cannot_replace_main_weights():
    catalog = {
        "main": {"alias": "main"},
        "new": {"alias": "new", "source": "new:e4b", "coding_probe": True},
    }
    tags = {
        "models": [
            {"name": "main:latest", "size": 6 * 2**30},
            {"name": "new:latest", "size": 7 * 2**30},
        ]
    }
    with (
        patch.object(model_library, "CATALOG", catalog),
        patch.object(
            model_library, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())
        ),
        patch.object(model_library, "alias_update_waits_for_tasks", return_value=False),
        patch.object(model_fitness, "suitable", return_value=True),
        patch.object(model_fitness, "game_build_approved", return_value=False),
        patch.object(
            model_library, "post", return_value={"capabilities": ["tools"]}
        ) as post,
    ):
        assert model_library.repair_main_alias(128, 12) is False
    post.assert_called_once()


def test_12gb_gpu_rejects_79b_but_accepts_compact_tool_models():
    assert not model_fitness.hardware_allows(48.2, 128, 12)
    assert model_fitness.hardware_allows(21.1, 128, 12)
    assert model_fitness.hardware_allows(6.1, 32, 12)


def test_unknown_gpu_keeps_only_small_models():
    assert model_fitness.hardware_allows(6.1, 24, 0)
    assert not model_fitness.hardware_allows(21.1, 128, 0)


def test_coding_candidates_require_gpu_headroom_not_just_system_ram():
    qwen = model_library.CATALOG["qwen38_code"]
    assert not model_fitness.profile_allows(qwen, 128, 12)
    assert model_fitness.profile_allows(qwen, 64, 24)
    assert not model_fitness.profile_allows(
        model_library.CATALOG["qwen25_code14"], 64, 8
    )
    assert model_fitness.profile_allows(model_library.CATALOG["qwen25_code7"], 32, 8)
    assert model_fitness.profile_allows({"alias": "main"}, 32, 12)
    assert (
        model_library.preferred_context(
            model_library.CATALOG["qwen25_code7"],
            {"model_info": {"qwen.context_length": 32768}},
            "source",
        )
        == 32768
    )


def test_full_game_grader_passes_known_game_logic_and_rejects_frame_bug():
    import pytest

    root = Path(__file__).resolve().parents[1]
    if not (
        root / "Toolbox/node-v26.13.0-win-x64/node.exe"
    ).is_file() and not shutil.which("node"):
        pytest.skip("Node runtime is required for the actual JS grader check")
    # A deliberately plain fixture checks the grader, not visual game quality.
    code = r"""<html><canvas id="gameCanvas"></canvas><script>
    const g=window.game={reset(){Object.assign(this,{player:{x:300,y:700,w:40,h:40,invincible:0},
      bullets:[],enemies:[],score:0,lives:3,wave:1,playing:true,waveSpawned:false});},
    update(dt,k){if(!this.playing)return;
      const dir=(k.ArrowRight?1:0)-(k.ArrowLeft?1:0);
      this.player.x=Math.max(20,Math.min(580,this.player.x+dir*120*dt));
      this.player.invincible=Math.max(0,this.player.invincible-dt);
      if(k.Space)this.bullets.push({x:this.player.x,y:this.player.y-30,w:4,h:16,speed:200});
      const hits=(a,b)=>Math.abs(a.x-b.x)<(a.w+b.w)/2&&Math.abs(a.y-b.y)<(a.h+b.h)/2;
      this.bullets.forEach(b=>b.y-=b.speed*dt);
      for(let i=this.enemies.length-1;i>=0;i--){const e=this.enemies[i];e.y+=e.speed*dt;
        const j=this.bullets.findIndex(b=>hits(e,b));
        if(j>=0){this.bullets.splice(j,1);this.enemies.splice(i,1);this.score+=10;continue;}
        if(!this.player.invincible&&hits(e,this.player)){this.lives--;this.player.invincible=1;
          this.enemies.splice(i,1);if(!this.lives)this.playing=false;}}
      if(this.waveSpawned&&!this.enemies.length){this.wave++;this.waveSpawned=false;}
    }};g.reset();window.addEventListener('blur',()=>{});
    const c=document.getElementById('gameCanvas').getContext('2d');for(let i=0;i<8;i++)c.fillRect(i,0,1,1);
    </script></html>"""
    checks = model_fitness.grade_game_html(code, root)
    assert len(checks) == 13 and all(checks.values()), checks
    broken = code.replace("dir*120*dt", "dir*120/60")
    assert model_fitness.grade_game_html(broken, root)["frame_independence"] is False
    assert model_fitness.grade_game_html("<script>window.game={};</script>", root) == {
        "valid_code": False
    }


def test_game_ranking_uses_matching_weights_hardware_quality_and_wall_time():
    catalog = {
        "fast": {"alias": "cale-fast", "source": "old:9b"},
        "new": {"alias": "new", "source": "new:e4b"},
    }
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "LongTasks").mkdir()
        records = {
            source: {
                "version": model_fitness.GAME_PROBE_VERSION,
                "passed": True,
                "seconds": seconds,
                "build": {"passed": True, "seconds": seconds * 10},
                "updated": time.time(),
                "weights": "a" * 64,
                "ram_gb": 32,
                "vram_gb": 12,
            }
            for source, seconds in (("old:9b", 7), ("new:e4b", 4))
        }
        path = root / "LongTasks/game-model-probes.json"
        path.write_text(json.dumps(records))
        weights = {"cale-fast": "a" * 64, "new": "a" * 64}
        with (
            patch.object(model_fitness, "gpu_vram_gb", return_value=12),
            patch.object(model_library, "total_ram_gb", return_value=32),
        ):

            def rank():
                return model_fitness.rank_game_models(
                    ["cale-fast", "new"], catalog, root, weights
                )

            assert rank() == ["new", "cale-fast"]
            records["new:e4b"]["passed"] = False
            path.write_text(json.dumps(records))
            assert rank() == ["cale-fast", "new"]
            records["new:e4b"]["passed"] = True
            records["new:e4b"]["weights"] = "b" * 64
            path.write_text(json.dumps(records))
            assert rank() == ["cale-fast", "new"]
            records["new:e4b"]["weights"] = "a" * 64
            records["new:e4b"]["vram_gb"] = 24
            path.write_text(json.dumps(records))
            assert rank() == ["cale-fast", "new"]
            records["new:e4b"]["vram_gb"] = 12
            records["old:9b"]["updated"] = time.time() - 31 * 86400
            path.write_text(json.dumps(records))
            # A qualified candidate need not wait for a failing or stale
            # baseline to pass before it can become the preferred builder.
            assert rank() == ["new", "cale-fast"]


def test_game_ranking_nested_cache_falls_back_and_allows_retry():
    catalog = {
        "old": {"alias": "cale-fast", "source": "old:9b"},
        "new": {"alias": "new", "source": "new:e4b", "coding_probe": True},
    }
    depth = max(5000, sys.getrecursionlimit() + 100)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "LongTasks").mkdir()
        path = root / "LongTasks/game-model-probes.json"
        path.write_text("[" * depth + "0" + "]" * depth, encoding="utf-8")
        assert model_fitness.rank_game_models(
            ["new", "cale-fast"], catalog, root
        ) == ["cale-fast"]
        path.write_text("{}", encoding="utf-8")
        assert model_fitness.rank_game_models(
            ["new", "cale-fast"], catalog, root
        ) == ["cale-fast"]


def test_game_probe_current_rejects_nested_cache_and_recovers():
    depth = max(5000, sys.getrecursionlimit() + 100)
    source = "candidate:7b"
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        path = root / "LongTasks/game-model-probes.json"
        path.parent.mkdir()
        path.write_text("[" * depth + "0" + "]" * depth, encoding="utf-8")
        with (
            patch.object(model_library, "post") as post,
            patch.object(model_library, "weight_blob", return_value="a" * 64),
            patch.object(model_fitness, "gpu_vram_gb", return_value=12),
            patch.object(model_library, "total_ram_gb", return_value=32),
        ):
            assert model_fitness.game_probe_current(source, root) is False
            post.assert_not_called()
            path.write_text(
                json.dumps({source: {
                    "version": model_fitness.GAME_PROBE_VERSION,
                    "weights": "a" * 64,
                    "vram_gb": 12,
                    "ram_gb": 32,
                    "updated": time.time(),
                    "build": {"passed": True},
                }}),
                encoding="utf-8",
            )
            assert model_fitness.game_probe_current(
                source, root, info={"model": source}
            ) is True
            post.assert_not_called()


def test_game_probe_requires_two_complete_independent_passes():
    with tempfile.TemporaryDirectory() as folder:
        checks = {str(i): True for i in range(13)}
        replies = [
            {"modelfile": "FROM sha256-" + "a" * 64},
            {"message": {"content": '{"code":"first"}'}, "eval_count": 100},
            {"message": {"content": '{"code":"second"}'}, "eval_count": 100},
            {"message": {"content": '{"html":"<html>game</html>"}'}, "eval_count": 100},
        ]
        with (
            patch.object(model_library, "post", side_effect=replies),
            patch.object(
                model_fitness,
                "grade_game_code",
                side_effect=[checks, {"invalid": False}],
            ),
            patch.object(
                model_fitness, "grade_game_html", return_value={"live_api": False}
            ),
            patch.object(model_fitness, "gpu_vram_gb", return_value=12),
            patch.object(model_library, "total_ram_gb", return_value=32),
        ):
            result = model_fitness.probe_game_coding("candidate:7b", folder)
        assert not result["passed"]
        saved = json.loads(
            (Path(folder) / "LongTasks/game-model-probes.json").read_text()
        )
        assert saved["candidate:7b"]["samples"][0]["checks"] == checks
        assert len(saved["candidate:7b"]["samples"]) == 2
        assert saved["candidate:7b"]["build"]["passed"] is False


def test_game_probe_nested_completion_json_is_saved_as_failed_samples():
    nested = "[" * (max(5000, sys.getrecursionlimit() + 100)) + "0"
    nested += "]" * (max(5000, sys.getrecursionlimit() + 100))
    replies = [
        {"modelfile": "FROM sha256-" + "a" * 64},
        *[{"message": {"content": nested}} for _ in range(3)],
    ]
    with tempfile.TemporaryDirectory() as folder:
        with (
            patch.object(model_library, "post", side_effect=replies) as post,
            patch.object(model_fitness, "grade_game_code") as grade_code,
            patch.object(model_fitness, "grade_game_html") as grade_html,
            patch.object(model_fitness, "gpu_vram_gb", return_value=12),
            patch.object(model_library, "total_ram_gb", return_value=32),
        ):
            result = model_fitness.probe_game_coding("candidate:7b", folder)
        assert post.call_count == 4
        grade_code.assert_not_called()
        grade_html.assert_not_called()
        assert result["passed"] is False
        assert [sample["checks"] for sample in result["samples"]] == [
            {"valid_code": False},
            {"valid_code": False},
        ]
        assert result["build"]["passed"] is False
        assert result["build"]["checks"] == {"valid_html": False}
        saved = json.loads(
            (Path(folder) / "LongTasks/game-model-probes.json").read_text()
        )
        assert saved["candidate:7b"] == result


def test_game_probe_null_completion_content_is_saved_as_failure():
    replies = [
        {"modelfile": "FROM sha256-" + "a" * 64},
        *[{"message": {"content": None}} for _ in range(3)],
    ]
    with tempfile.TemporaryDirectory() as folder:
        with (
            patch.object(model_library, "post", side_effect=replies) as post,
            patch.object(model_fitness, "grade_game_code") as grade_code,
            patch.object(model_fitness, "grade_game_html") as grade_html,
            patch.object(model_fitness, "gpu_vram_gb", return_value=12),
            patch.object(model_library, "total_ram_gb", return_value=32),
        ):
            result = model_fitness.probe_game_coding("candidate:7b", folder)
        assert post.call_count == 4
        grade_code.assert_not_called()
        grade_html.assert_not_called()
        assert result["passed"] is False
        assert [sample["checks"] for sample in result["samples"]] == [
            {"valid_code": False},
            {"valid_code": False},
        ]
        assert result["build"]["passed"] is False
        assert result["build"]["checks"] == {"valid_html": False}
        saved = json.loads(
            (Path(folder) / "LongTasks/game-model-probes.json").read_text()
        )
        assert saved["candidate:7b"] == result


def test_game_probe_replaces_nested_cache_with_completed_report():
    depth = max(5000, sys.getrecursionlimit() + 100)
    replies = [
        {"modelfile": "FROM sha256-" + "a" * 64},
        {"message": {"content": '{"code":"first"}'}},
        {"message": {"content": '{"code":"second"}'}},
        {"message": {"content": '{"html":"<html>game</html>"}'}},
    ]
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        path = root / "LongTasks/game-model-probes.json"
        path.parent.mkdir()
        path.write_text("[" * depth + "0" + "]" * depth, encoding="utf-8")
        with (
            patch.object(model_library, "post", side_effect=replies) as post,
            patch.object(
                model_fitness, "grade_game_code", return_value={"valid_code": False}
            ) as grade_code,
            patch.object(
                model_fitness, "grade_game_html", return_value={"valid_html": False}
            ) as grade_html,
            patch.object(model_fitness, "gpu_vram_gb", return_value=12),
            patch.object(model_library, "total_ram_gb", return_value=32),
        ):
            result = model_fitness.probe_game_coding("candidate:7b", root)
        assert post.call_count == 4
        assert grade_code.call_count == 2
        grade_html.assert_called_once()
        assert result["passed"] is False
        assert len(result["samples"]) == 2
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "candidate:7b": result
        }


def test_small_functions_alone_do_not_promote_new_game_builder():
    catalog = {
        "old": {"alias": "cale-fast", "source": "old:9b"},
        "new": {"alias": "new", "source": "new:e4b", "coding_probe": True},
    }
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        assert model_fitness.rank_game_models(["new", "cale-fast"], catalog, root) == [
            "cale-fast"
        ]
        (root / "LongTasks").mkdir()
        (root / "LongTasks/game-model-probes.json").write_text(
            json.dumps(
                {
                    "new:e4b": {
                        "version": model_fitness.GAME_PROBE_VERSION,
                        "passed": True,
                        "build": {"passed": False},
                        "seconds": 1,
                        "weights": "a" * 64,
                        "ram_gb": 32,
                        "vram_gb": 12,
                        "updated": time.time(),
                    }
                }
            )
        )
        with (
            patch.object(model_fitness, "gpu_vram_gb", return_value=12),
            patch.object(model_library, "total_ram_gb", return_value=32),
        ):
            assert model_fitness.rank_game_models(
                ["new", "cale-fast"], catalog, root, {"new": "a" * 64}
            ) == ["cale-fast"]


def test_game_grader_invalid_json_is_a_failed_grade_and_allows_retry():
    invalid = type("Result", (), {"stdout": "not JSON\n"})()
    valid = type("Result", (), {"stdout": '{"movement":true}'})()
    with (
        patch("shutil.which", return_value="node"),
        patch.object(
            model_fitness.subprocess, "run", side_effect=[invalid, valid]
        ) as run,
    ):
        # Generated code can replace the serializer used by the JS checks.
        assert model_fitness.grade_game_code(
            "JSON.stringify = () => 'not JSON';", Path("missing")
        ) == {"valid_code": False}
        assert model_fitness.grade_game_code("", Path("missing")) == {
            "movement": True
        }
    assert run.call_count == 2


def test_game_grader_oversized_integer_is_a_failed_grade_and_allows_retry():
    import pytest

    limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if not limit:
        pytest.skip("Python integer-string conversion limit is not enabled")
    invalid = type(
        "Result", (), {"stdout": '{"movement":' + "9" * (limit + 1) + "}"}
    )()
    valid = type("Result", (), {"stdout": '{"movement":true}'})()
    with (
        patch("shutil.which", return_value="node"),
        patch.object(
            model_fitness.subprocess, "run", side_effect=[invalid, valid]
        ) as run,
    ):
        assert model_fitness.grade_game_code("", Path("missing")) == {
            "valid_code": False
        }
        assert model_fitness.grade_game_code("", Path("missing")) == {
            "movement": True
        }
    assert run.call_count == 2


def test_game_grader_nested_json_is_a_failed_grade_and_allows_retry():
    nested = type("Result", (), {"stdout": "[" * 10000 + "0" + "]" * 10000})()
    valid = type("Result", (), {"stdout": '{"movement":true}'})()
    with (
        patch("shutil.which", return_value="node"),
        patch.object(
            model_fitness.subprocess, "run", side_effect=[nested, valid]
        ) as run,
    ):
        # Generated code can replace the serializer and exceed decoder depth.
        assert model_fitness.grade_game_code(
            "JSON.stringify = () => '['.repeat(10000) + '0' + ']'.repeat(10000);",
            Path("missing"),
        ) == {"valid_code": False}
        assert model_fitness.grade_game_code("", Path("missing")) == {
            "movement": True
        }
    assert run.call_count == 2


def test_game_grader_process_timeout_is_a_failed_grade_and_allows_retry():
    result = type("Result", (), {"stdout": '{"movement":true}'})()
    with (
        patch("shutil.which", return_value="node"),
        patch.object(
            model_fitness.subprocess,
            "run",
            side_effect=[
                model_fitness.subprocess.TimeoutExpired("node", 8),
                result,
            ],
        ) as run,
    ):
        assert model_fitness.grade_game_code(
            "Promise.resolve().then(() => { while (true) {} });", Path("missing")
        ) == {"valid_code": False}
        assert model_fitness.grade_game_code("", Path("missing")) == {
            "movement": True
        }
    assert run.call_count == 2


def test_game_grader_nonzero_exit_is_a_failed_grade_and_allows_retry():
    result = type("Result", (), {"stdout": '{"movement":true}'})()
    with (
        patch("shutil.which", return_value="node"),
        patch.object(
            model_fitness.subprocess,
            "run",
            side_effect=[
                model_fitness.subprocess.CalledProcessError(
                    1, "node", output="", stderr="Unhandled rejection"
                ),
                result,
            ],
        ) as run,
    ):
        assert model_fitness.grade_game_code(
            "Promise.reject(new Error('failed'));", Path("missing")
        ) == {"valid_code": False}
        assert model_fitness.grade_game_code("", Path("missing")) == {
            "movement": True
        }
    assert run.call_count == 2


def test_game_grader_launch_failure_is_a_failed_grade_and_allows_retry():
    result = type("Result", (), {"stdout": '{"movement":true}'})()
    for error in (FileNotFoundError("node disappeared"), PermissionError("node denied")):
        with (
            patch("shutil.which", return_value="node"),
            patch.object(
                model_fitness.subprocess, "run", side_effect=[error, result]
            ) as run,
        ):
            assert model_fitness.grade_game_code("", Path("missing")) == {
                "valid_code": False
            }
            assert model_fitness.grade_game_code("", Path("missing")) == {
                "movement": True
            }
        assert run.call_count == 2


def test_game_grader_has_time_and_node_permission_limits():
    result = type("Result", (), {"stdout": '{"valid_code":false}'})()
    with (
        patch("shutil.which", return_value="node"),
        patch.object(model_fitness.subprocess, "run", return_value=result) as run,
    ):
        assert model_fitness.grade_game_code("while(true){}", Path("missing")) == {
            "valid_code": False
        }
    assert "--permission" in run.call_args.args[0]
    assert run.call_args.kwargs["timeout"] == 8
    assert "timeout:1000" in run.call_args.args[0][-1]


def test_game_grader_outer_timeout_marks_code_invalid():
    with (
        patch("shutil.which", return_value="node"),
        patch.object(
            model_fitness.subprocess,
            "run",
            side_effect=model_fitness.subprocess.TimeoutExpired("node", 8),
        ) as run,
    ):
        assert model_fitness.grade_game_code(
            "Promise.resolve().then(function spin(){Promise.resolve().then(spin);});",
            Path("missing"),
        ) == {"valid_code": False}
    run.assert_called_once()
    assert run.call_args.kwargs["timeout"] == 8


def test_repeated_slow_completions_disqualify_model():
    with patch.object(model_fitness, "retirement_speed", return_value=2.2):
        assert not model_fitness.suitable("cale-qwen35", 20.6, 128, 12)
    with patch.object(model_fitness, "retirement_speed", return_value=18.4):
        assert model_fitness.suitable("cale-qwen36", 21.1, 128, 12)


def test_retirement_waits_for_idle_and_preserves_unowned_source():
    item = {"alias": "cale-slow", "source": "someone/slow:latest"}
    with (
        patch.object(model_library, "alias_update_waits_for_tasks", return_value=True),
        patch.object(model_library, "delete_model") as delete,
    ):
        assert not model_library.retire_slow_model(item)
        delete.assert_not_called()
    with (
        patch.object(model_library, "alias_update_waits_for_tasks", return_value=False),
        patch.object(model_library, "owned_sources", return_value=set()),
        patch.object(model_library, "delete_model") as delete,
    ):
        assert model_library.retire_slow_model(item)
        delete.assert_called_once_with("cale-slow")


def test_ollama_model_removal_uses_delete_verb():
    with patch.object(
        model_library, "urlopen", return_value=io.BytesIO(b"")
    ) as open_url:
        model_library.delete_model("cale-slow")
    request = open_url.call_args.args[0]
    assert request.get_method() == "DELETE"


def test_retirement_succeeds_if_alias_was_already_deleted():
    item = {"alias": "cale-slow", "source": "someone/slow:latest"}
    missing = HTTPError(
        "http://127.0.0.1:11434/api/delete", 404, "model not found", {}, None
    )
    with (
        patch.object(model_library, "alias_update_waits_for_tasks", return_value=False),
        patch.object(model_library, "owned_sources", return_value=set()),
        patch.object(model_library, "delete_model", side_effect=missing) as delete,
    ):
        assert model_library.retire_slow_model(item)
        delete.assert_called_once_with("cale-slow")


def test_retirement_does_not_hide_ollama_failures():
    item = {"alias": "cale-slow", "source": "someone/slow:latest"}
    unavailable = HTTPError(
        "http://127.0.0.1:11434/api/delete", 503, "unavailable", {}, None
    )
    with (
        patch.object(model_library, "alias_update_waits_for_tasks", return_value=False),
        patch.object(model_library, "delete_model", side_effect=unavailable),
    ):
        assert not model_library.retire_slow_model(item)
