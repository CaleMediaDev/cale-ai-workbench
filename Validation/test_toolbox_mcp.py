"""A broken optional backend must not disable independent local tools."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import toolbox_mcp


@pytest.mark.parametrize("already_exited", [False, True])
def test_cancelled_media_call_stops_and_reaps_worker(tmp_path, already_exited):
    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()

    async def check():
        started = asyncio.Event()
        cleanup = []

        class FakeProcess:
            returncode = None

            async def communicate(self, data):
                started.set()
                await asyncio.Future()

            def kill(self):
                cleanup.append("kill")
                if already_exited:
                    raise ProcessLookupError("Worker exited while cancellation arrived")

            async def wait(self):
                cleanup.append("wait")
                self.returncode = -1
                return self.returncode

        async def fake_spawn(*args, **kwargs):
            return FakeProcess()

        with (patch.object(toolbox_mcp, "ROOT", tmp_path),
              patch.object(toolbox_mcp.asyncio, "create_subprocess_exec", fake_spawn)):
            task = asyncio.create_task(toolbox_mcp.media_run("generate_image"))
            await started.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Media call swallowed cancellation")
        assert cleanup == ["kill", "wait"]

    asyncio.run(check())


@pytest.mark.parametrize("exit_state", ["running", "already_exited", "during_kill"])
def test_media_timeout_reaps_worker_and_preserves_timeout(tmp_path, exit_state):
    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    cleanup = []

    class FakeProcess:
        returncode = 0 if exit_state == "already_exited" else None

        async def communicate(self, data):
            await asyncio.Future()

        def kill(self):
            cleanup.append("kill")
            if exit_state == "during_kill":
                raise ProcessLookupError("Worker exited just before kill")

        async def wait(self):
            cleanup.append("wait")
            self.returncode = -1
            return self.returncode

    async def fake_spawn(*args, **kwargs):
        return FakeProcess()

    with (patch.object(toolbox_mcp, "ROOT", tmp_path),
          patch.object(toolbox_mcp.asyncio, "create_subprocess_exec", fake_spawn)):
        with pytest.raises(TimeoutError, match="Local generate_image exceeded 0 seconds"):
            asyncio.run(toolbox_mcp.media_run("generate_image", timeout=0))

    assert cleanup == (["wait"] if exit_state == "already_exited" else ["kill", "wait"])


def test_3d_generation_and_cpu_geometry_are_discoverable_and_callable():
    found = toolbox_mcp.toolbox_search("3d model mesh")
    names = {item["name"] for item in found["tools"]}
    assert {"local.start_3d_job", "local.build_3d_scene", "local.model_3d_job_status"} <= names
    with patch.object(toolbox_mcp, "start_3d_job", return_value={"job_id": "job"}) as start:
        result = asyncio.run(toolbox_mcp.toolbox_call("local.start_3d_job",
            json.dumps({"output_path": "ship.glb", "image_path": "ship.png"})))
    assert result["job_id"] == "job"
    start.assert_called_once_with(output_path="ship.glb", image_path="ship.png")
    with patch.object(toolbox_mcp, "build_3d_scene", return_value={"job_id": "cpu"}):
        result = asyncio.run(toolbox_mcp.toolbox_call("local.build_3d_scene",
            json.dumps({"output_path": "ship.glb", "objects": [{"type": "box"}]})))
    assert result["job_id"] == "cpu"


def test_project_checks_use_only_registered_task_folder(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    state = tmp_path / "LongTasks"
    state.mkdir()
    with sqlite3.connect(state / "projects.db") as conn:
        conn.execute("CREATE TABLE projects (folder TEXT)")
        conn.execute("INSERT INTO projects VALUES (?)", (str(folder),))
    project = {"id": "task-1", "folder": str(folder)}
    import long_tasks

    with (patch.object(toolbox_mcp, "ROOT", tmp_path),
          patch.object(long_tasks, "STATE", state),
          patch.object(long_tasks, "projects", return_value=[project]),
          patch.object(long_tasks, "verify_project", return_value=(True, "Checks passed")) as verify):
        result = asyncio.run(toolbox_mcp.run_project_checks(str(folder), 60))
        assert result["passed"] is True
        assert result["evidence"] == "Checks passed"
        assert result["verification_log"] is None
        verify.assert_called_once_with(project, 60)
        try:
            asyncio.run(toolbox_mcp.run_project_checks(str(tmp_path), 60))
        except ValueError as exc:
            assert "exact folder" in str(exc)
        else:
            raise AssertionError("Unregistered folder was accepted")


def test_music_tool_is_discoverable_and_callable_from_toolbox_search():
    found = toolbox_mcp.toolbox_search("music audio generate")
    names = [tool["name"] for tool in found["tools"]]
    assert names[0] == "local.make_music"
    schema = toolbox_mcp.toolbox_schema(names[0])
    assert schema["inputSchema"]["required"] == ["prompt", "output_path"]

    async def fake_music(**arguments):
        return {"path": arguments["output_path"], "model": "ACE-Step 1.5"}

    with patch.object(toolbox_mcp, "make_music", fake_music):
        result = asyncio.run(toolbox_mcp.toolbox_call(
            names[0], json.dumps({"prompt": "beach guitar", "output_path": "song.mp3"})
        ))
    assert result["path"] == "song.mp3"


def test_agent_started_music_carries_the_registered_task_id():
    import music_backend

    with patch.object(music_backend, "project_for_output", return_value="a" * 12), \
         patch.object(music_backend, "start_music_job", return_value={"job_id": "x"}) as start:
        result = toolbox_mcp.start_music_job("female vocal dubstep", "C:/Tasks/song.wav")
    assert result["job_id"] == "x"
    assert start.call_args.kwargs["task_id"] == "a" * 12


def test_audio_stem_tool_is_discoverable_and_callable():
    found = toolbox_mcp.toolbox_search("separate vocals instrumental")
    assert any(tool["name"] == "local.separate_music" for tool in found["tools"])
    schema = toolbox_mcp.toolbox_schema("local.separate_music")
    assert schema["inputSchema"]["required"] == ["source_path", "output_dir"]

    async def fake_separation(**arguments):
        return {"files": [arguments["output_dir"] + "/vocals.wav"]}

    with patch.object(toolbox_mcp, "separate_music", fake_separation):
        result = asyncio.run(toolbox_mcp.toolbox_call(
            "local.separate_music", json.dumps({"source_path": "song.mp3", "output_dir": "stems"})
        ))
    assert result["files"] == ["stems/vocals.wav"]


def test_audio_to_midi_tool_is_discoverable_and_callable():
    found = toolbox_mcp.toolbox_search("audio midi transcribe")
    assert any(tool["name"] == "local.audio_to_midi" for tool in found["tools"])
    schema = toolbox_mcp.toolbox_schema("local.audio_to_midi")
    assert schema["inputSchema"]["required"] == ["source_path", "output_dir"]

    async def fake_transcription(**arguments):
        return {"midi_path": arguments["output_dir"] + "/melody.mid"}

    with patch.object(toolbox_mcp, "audio_to_midi", fake_transcription):
        result = asyncio.run(toolbox_mcp.toolbox_call(
            "local.audio_to_midi", json.dumps({"source_path": "melody.wav", "output_dir": "notes"})
        ))
    assert result["midi_path"] == "notes/melody.mid"


def test_analyze_music_tool_is_discoverable_and_callable():
    found = toolbox_mcp.toolbox_search("song tempo key chords")
    assert any(tool["name"] == "local.analyze_music" for tool in found["tools"])
    assert toolbox_mcp.toolbox_schema("local.analyze_music")["inputSchema"]["required"] == ["source_path"]

    async def fake_analysis(**arguments):
        return {"source": arguments["source_path"], "tempo_bpm": 120}

    with patch.object(toolbox_mcp, "analyze_music", fake_analysis):
        result = asyncio.run(toolbox_mcp.toolbox_call(
            "local.analyze_music", json.dumps({"source_path": "song.wav"})
        ))
    assert result["tempo_bpm"] == 120


def test_preview_backend_errors_preserve_successful_browser_result(tmp_path):
    class PreviewBackendError(Exception):
        pass

    async def check(failing_stage):
        calls = []
        original = {"isError": False, "content": [{"type": "text", "text": "done"}]}

        class FakeSession:
            async def call_tool(self, name, arguments):
                calls.append(name)
                if name == "navigate_page":
                    return SimpleNamespace(
                        is_error=False, model_dump=lambda **_kwargs: original
                    )
                if name == failing_stage:
                    raise PreviewBackendError("preview backend failed")
                assert name == "list_pages"
                return SimpleNamespace(
                    is_error=False,
                    content=[SimpleNamespace(text="1: https://example.com [selected]")],
                )

        with (patch.dict(toolbox_mcp.CATALOG, {"devtools.navigate_page": {}}, clear=True),
              patch.dict(toolbox_mcp.SESSIONS, {"devtools": FakeSession()}, clear=True),
              patch.object(toolbox_mcp, "registered_project", return_value=tmp_path)):
            result = await toolbox_mcp.toolbox_call(
                "devtools.navigate_page", "{}", project_folder=str(tmp_path)
            )
        assert result == original
        assert calls == (["navigate_page", "list_pages"] if failing_stage == "list_pages"
                         else ["navigate_page", "list_pages", "take_screenshot"])

    for failing_stage in ("list_pages", "take_screenshot"):
        asyncio.run(check(failing_stage))


def test_cancelled_backend_initialization_closes_partial_backend():
    async def check():
        initializing = asyncio.Event()
        opened = []
        closed = []

        @asynccontextmanager
        async def fake_stdio(params):
            opened.append("stdio")
            try:
                yield (object(), object())
            finally:
                closed.append("stdio")

        class FakeSession:
            async def __aenter__(self):
                opened.append("session")
                return self

            async def __aexit__(self, *_args):
                closed.append("session")

            async def initialize(self):
                initializing.set()
                await asyncio.Future()

        async def consume():
            async with toolbox_mcp.lifespan(None):
                raise AssertionError("Cancelled startup must not yield")

        with (patch.object(toolbox_mcp, "stdio_client", fake_stdio),
              patch.object(toolbox_mcp, "ClientSession",
                           lambda *_args, **_kwargs: FakeSession()),
              patch.dict(toolbox_mcp.SESSIONS, {}, clear=True),
              patch.dict(toolbox_mcp.CATALOG, {}, clear=True),
              patch.dict(toolbox_mcp.ERRORS, {}, clear=True)):
            task = asyncio.create_task(consume())
            await initializing.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert opened == ["stdio", "session"]
            assert closed == ["session", "stdio"]
            assert toolbox_mcp.SESSIONS == {}
            assert toolbox_mcp.CATALOG == {}

    asyncio.run(check())


def test_exceptional_lifespan_exit_clears_closed_backend_registries():
    closed = []

    @asynccontextmanager
    async def fake_stdio(params):
        if "rea.mjs" in " ".join(params.args):
            raise OSError("REA could not start")
        yield (object(), object())

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed.append(True)

        async def initialize(self):
            pass

        async def list_tools(self):
            tool = SimpleNamespace(
                name="inspect_page",
                model_dump=lambda **_kwargs: {"description": "Inspect a browser page"},
            )
            return SimpleNamespace(tools=[tool])

    async def check():
        with (patch.object(toolbox_mcp, "stdio_client", fake_stdio),
              patch.object(toolbox_mcp, "ClientSession",
                           lambda *_args, **_kwargs: FakeSession())):
            with pytest.raises(RuntimeError, match="lifespan consumer failed"):
                async with toolbox_mcp.lifespan(None):
                    assert "devtools" in toolbox_mcp.SESSIONS
                    assert "devtools.inspect_page" in toolbox_mcp.CATALOG
                    assert "rea" in toolbox_mcp.ERRORS
                    raise RuntimeError("lifespan consumer failed")
        assert closed == [True]
        assert toolbox_mcp.SESSIONS == {}
        assert toolbox_mcp.CATALOG == {}
        assert toolbox_mcp.ERRORS == {}
        assert toolbox_mcp.toolbox_status()["backends"]["devtools"]["available"] is False

    asyncio.run(check())


def test_failed_backend_cleanup_does_not_abort_independent_backend():
    closed = []

    @asynccontextmanager
    async def fake_stdio(params):
        group = "rea" if "rea.mjs" in " ".join(params.args) else "devtools"
        try:
            yield (group, object())
        finally:
            closed.append(group)

    class FakeSession:
        def __init__(self, group):
            self.group = group

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            if self.group == "rea":
                raise RuntimeError("REA cleanup failed")

        async def initialize(self):
            if self.group == "rea":
                raise RuntimeError("REA initialization failed")

        async def list_tools(self):
            tool = SimpleNamespace(
                name="inspect_page",
                model_dump=lambda **_kwargs: {"description": "Inspect a browser page"},
            )
            return SimpleNamespace(tools=[tool])

    async def check():
        with (patch.object(toolbox_mcp, "stdio_client", fake_stdio),
              patch.object(toolbox_mcp, "ClientSession",
                           lambda reader, *_args, **_kwargs: FakeSession(reader))):
            async with toolbox_mcp.lifespan(None):
                status = toolbox_mcp.toolbox_status()
                assert status["backends"]["rea"]["available"] is False
                error = status["backends"]["rea"]["error"]
                assert "REA initialization failed" in error
                assert "REA cleanup failed" in error
                assert status["backends"]["devtools"]["available"] is True
                assert "devtools.inspect_page" in toolbox_mcp.CATALOG
                assert "rea" in closed
                assert toolbox_mcp.toolbox_schema("local.make_music")
        assert closed == ["rea", "devtools"]
        assert toolbox_mcp.SESSIONS == {}
        assert toolbox_mcp.CATALOG == {}
        assert toolbox_mcp.ERRORS == {}

    asyncio.run(check())


def test_failed_specialist_backend_keeps_other_tools_available():
    @asynccontextmanager
    async def fake_stdio(params):
        if "rea.mjs" in " ".join(params.args):
            raise OSError("REA could not start")
        yield (object(), object())

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            pass

        async def list_tools(self):
            tool = SimpleNamespace(model_dump=lambda **_kwargs: {
                "description": "Inspect a browser page"
            }, name="inspect_page")
            return SimpleNamespace(tools=[tool])

    async def check():
        with patch.object(toolbox_mcp, "stdio_client", fake_stdio), patch.object(
            toolbox_mcp, "ClientSession", lambda *_args, **_kwargs: FakeSession()
        ):
            async with toolbox_mcp.lifespan(None):
                status = toolbox_mcp.toolbox_status()
                assert status["backends"]["rea"]["available"] is False
                assert "REA could not start" in status["backends"]["rea"]["error"]
                assert status["backends"]["devtools"]["available"] is True
                assert status["backends"]["devtools"]["tools"] == 1
                assert toolbox_mcp.toolbox_search("browser")["matching"] == 1
                assert status["local_tools_available"] is True
        assert toolbox_mcp.toolbox_status()["backends"]["devtools"]["available"] is False

    asyncio.run(check())
