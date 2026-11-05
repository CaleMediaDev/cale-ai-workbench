"""A broken optional backend must not disable independent local tools."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import toolbox_mcp


def github_dataset_fixture(monkeypatch):
    import hashlib
    import pc_offline
    import small_training
    payloads = {"LICENSE": b"Fixture license attribution", "data/train.json": b'[["example", "label"]]'}
    entry = {"repository": "example/dataset", "commit": "a" * 40,
             "purpose": "intent classification", "license": "fixture", "license_path": "LICENSE",
             "attribution": "Synthetic fixture, not an approved public training source",
             "files": {path: hashlib.sha256(data).hexdigest() for path, data in payloads.items()}}
    monkeypatch.setattr(small_training, "GITHUB_DATASETS", {"fixture": entry})
    monkeypatch.setattr(pc_offline, "offline", lambda: False)
    return small_training, entry, payloads


def test_github_training_data_is_pinned_attributed_and_reusable_offline(tmp_path, monkeypatch):
    training, entry, payloads = github_dataset_fixture(monkeypatch)
    calls = []
    def fetch(url, limit):
        assert url.startswith("https://raw.githubusercontent.com/example/dataset/" + "a" * 40 + "/")
        calls.append(url)
        return payloads[url.split("a" * 40 + "/", 1)[1]]
    monkeypatch.setattr(training, "_github_dataset_bytes", fetch)
    folder = tmp_path / "dataset"
    result = training.download_training_dataset("fixture", folder)
    assert len(calls) == 2 and not result["cached"]
    assert result["commit"] == "a" * 40 and result["license"] == "fixture"
    assert not result["trained"] and not result["code_executed"]
    assert (folder / "files/LICENSE").read_bytes() == payloads["LICENSE"]
    assert json.loads((folder / "source.json").read_text())["files"] == entry["files"]
    import pc_offline
    monkeypatch.setattr(pc_offline, "offline", lambda: True)
    monkeypatch.setattr(training, "_github_dataset_bytes", lambda *args: pytest.fail("Cache used the internet"))
    assert training.download_training_dataset("fixture", folder)["cached"]


def test_github_training_data_corruption_never_publishes_or_overwrites(tmp_path, monkeypatch):
    training, entry, payloads = github_dataset_fixture(monkeypatch)
    monkeypatch.setattr(training, "_github_dataset_bytes", lambda url, limit: b"changed")
    output = tmp_path / "bad"
    with pytest.raises(ValueError, match="checksum"):
        training.download_training_dataset("fixture", output)
    assert not output.exists()
    output.mkdir()
    (output / "mine.txt").write_text("Keep my data")
    monkeypatch.setattr(training, "_github_dataset_bytes", lambda *args: pytest.fail("Overwrite attempted"))
    with pytest.raises(ValueError, match="preserve"):
        training.download_training_dataset("fixture", output)
    assert (output / "mine.txt").read_text() == "Keep my data"


@pytest.mark.parametrize("path", ["../escape.json", "/absolute.json", "C:/bad.json", "data\\escape.json", "CON.json"])
def test_github_training_data_rejects_unsafe_catalog_paths_before_download(tmp_path, monkeypatch, path):
    training, entry, payloads = github_dataset_fixture(monkeypatch)
    entry["files"][path] = "a" * 64
    monkeypatch.setattr(training, "_github_dataset_bytes", lambda *args: pytest.fail("Unsafe path downloaded"))
    with pytest.raises(ValueError, match="unsafe"):
        training.download_training_dataset("fixture", tmp_path / "out")


def test_github_training_http_reads_are_bounded_and_redirects_stay_on_github(monkeypatch):
    import small_training
    reads = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            reads.append(size)
            return b"x" * size
    class Opener:
        def open(self, request, timeout): return Response()
    monkeypatch.setattr(small_training, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="budget"):
        small_training._github_dataset_bytes("https://raw.githubusercontent.com/a/b/commit/data.json", 16)
    assert reads == [17]
    with pytest.raises(ValueError, match="redirect"):
        small_training._GitHubDataRedirect().redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1/private")


def test_github_training_tool_uses_registered_project_and_deterministic_cache(tmp_path, monkeypatch):
    training, entry, payloads = github_dataset_fixture(monkeypatch)
    monkeypatch.setattr(toolbox_mcp, "registered_project", lambda path: tmp_path)
    monkeypatch.setattr(training, "_github_dataset_bytes", lambda url, limit: payloads[url.split("a" * 40 + "/", 1)[1]])
    result = asyncio.run(toolbox_mcp.acquire_training_dataset("registered", "fixture"))
    assert result["dataset_folder"] == str(tmp_path / ".local-ai/training-datasets/fixture-aaaaaaaaaaaa")
    assert asyncio.run(toolbox_mcp.acquire_training_dataset("registered", "fixture"))["cached"]
    assert toolbox_mcp.find_training_datasets("intent")["datasets"][0]["id"] == "fixture"


@pytest.mark.parametrize("already_exited", [False, True])
def test_media_communication_error_stops_and_reaps_worker(tmp_path, already_exited):
    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    cleanup = []
    failure = OSError("Worker pipe failed")

    class FakeProcess:
        returncode = 1 if already_exited else None

        async def communicate(self, data):
            raise failure

        def kill(self):
            cleanup.append("kill")

        async def wait(self):
            cleanup.append("wait")
            self.returncode = -1
            return self.returncode

    async def fake_spawn(*args, **kwargs):
        return FakeProcess()

    with (patch.object(toolbox_mcp, "ROOT", tmp_path),
          patch.object(toolbox_mcp.asyncio, "create_subprocess_exec", fake_spawn)):
        with pytest.raises(OSError) as caught:
            asyncio.run(toolbox_mcp.media_run("generate_image"))

    assert caught.value is failure
    assert cleanup == (["wait"] if already_exited else ["kill", "wait"])


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


@pytest.mark.parametrize("response", [[], None, 42, "done", True, {"path": "image.png"}])
def test_media_worker_response_must_be_a_json_object(tmp_path, response):
    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()

    class FakeProcess:
        returncode = 0

        async def communicate(self, data):
            return json.dumps(response).encode("utf-8"), b""

    async def fake_spawn(*args, **kwargs):
        return FakeProcess()

    with (patch.object(toolbox_mcp, "ROOT", tmp_path),
          patch.object(toolbox_mcp.asyncio, "create_subprocess_exec", fake_spawn)):
        if isinstance(response, dict):
            assert asyncio.run(toolbox_mcp.media_run("generate_image")) == response
        else:
            with pytest.raises(RuntimeError, match="Local generate_image failed: worker response must be a JSON object"):
                asyncio.run(toolbox_mcp.media_run("generate_image"))


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


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_browser_capture_removes_temporary_image(tmp_path, cancelled):
    image = tmp_path / ".local-ai/browser.png"
    image.parent.mkdir()
    image.write_bytes(b"previous preview")
    captures = []

    class FakeSession:
        async def call_tool(self, name, arguments):
            if name == "list_pages":
                return SimpleNamespace(
                    is_error=False,
                    content=[SimpleNamespace(text="1: https://example.com [selected]")],
                )
            assert name == "take_screenshot"
            temporary = image.parent / arguments["filePath"]
            captures.append(temporary)
            temporary.write_bytes(b"incomplete capture")
            if cancelled:
                raise asyncio.CancelledError()
            raise OSError("Screenshot transport failed after writing")

    async def check():
        with (patch.dict(toolbox_mcp.SESSIONS, {"devtools": FakeSession()}, clear=True),
              patch.object(toolbox_mcp, "registered_project", return_value=tmp_path)):
            if cancelled:
                with pytest.raises(asyncio.CancelledError):
                    await toolbox_mcp.mirror_chrome_page(str(tmp_path))
            else:
                await toolbox_mcp.mirror_chrome_page(str(tmp_path))
        assert len(captures) == 1
        assert not captures[0].exists()
        assert image.read_bytes() == b"previous preview"

    asyncio.run(check())


def test_concurrent_browser_previews_publish_their_own_captures(tmp_path):
    async def check():
        first_written = asyncio.Event()
        second_written = asyncio.Event()
        release_second = asyncio.Event()
        paths = []

        class FakeSession:
            async def call_tool(self, name, arguments):
                if name == "list_pages":
                    return SimpleNamespace(
                        is_error=False,
                        content=[SimpleNamespace(text="1: https://example.com [selected]")],
                    )
                assert name == "take_screenshot"
                path = tmp_path / arguments["filePath"]
                paths.append(path)
                if len(paths) == 1:
                    path.write_bytes(b"first capture")
                    first_written.set()
                    await second_written.wait()
                else:
                    path.write_bytes(b"second capture")
                    second_written.set()
                    await release_second.wait()
                return SimpleNamespace(is_error=False)

        with (patch.dict(toolbox_mcp.SESSIONS, {"devtools": FakeSession()}, clear=True),
              patch.object(toolbox_mcp, "registered_project", return_value=tmp_path)):
            first = asyncio.create_task(toolbox_mcp.mirror_chrome_page(str(tmp_path)))
            await first_written.wait()
            second = asyncio.create_task(toolbox_mcp.mirror_chrome_page(str(tmp_path)))
            try:
                await first
                assert (tmp_path / ".local-ai/browser.png").read_bytes() == b"first capture"
            finally:
                release_second.set()
                await second
            assert (tmp_path / ".local-ai/browser.png").read_bytes() == b"second capture"
            assert paths[0] != paths[1]
            assert all(not path.exists() for path in paths)

    asyncio.run(asyncio.wait_for(check(), timeout=2))


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


def test_cancelled_second_backend_startup_clears_first_backend_registries():
    async def check():
        initializing = asyncio.Event()
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
                pass

            async def initialize(self):
                if self.group == "devtools":
                    initializing.set()
                    await asyncio.Future()

            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(
                    name="inspect",
                    model_dump=lambda **_kwargs: {"description": "Inspect an artifact"},
                )])

        async def consume():
            async with toolbox_mcp.lifespan(None):
                raise AssertionError("Cancelled startup must not yield")

        with (patch.object(toolbox_mcp, "stdio_client", fake_stdio),
              patch.object(toolbox_mcp, "ClientSession",
                           lambda reader, *_args, **_kwargs: FakeSession(reader)),
              patch.dict(toolbox_mcp.SESSIONS, {}, clear=True),
              patch.dict(toolbox_mcp.CATALOG, {}, clear=True),
              patch.dict(toolbox_mcp.ERRORS, {}, clear=True)):
            task = asyncio.create_task(consume())
            await initializing.wait()
            assert "rea" in toolbox_mcp.SESSIONS
            assert "rea.inspect" in toolbox_mcp.CATALOG
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed == ["devtools", "rea"]
            assert toolbox_mcp.SESSIONS == {}
            assert toolbox_mcp.CATALOG == {}
            assert toolbox_mcp.ERRORS == {}
            assert toolbox_mcp.toolbox_status()["backends"]["rea"]["available"] is False

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


def test_backend_discovery_includes_tools_from_later_pages():
    discovery_calls = []
    tool_calls = []

    @asynccontextmanager
    async def fake_stdio(params):
        if "rea.mjs" not in " ".join(params.args):
            raise OSError("DevTools unavailable")
        yield (object(), object())

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def initialize(self):
            pass

        async def list_tools(self, cursor=None):
            discovery_calls.append(cursor)
            assert cursor in (None, "page-2")
            name = "first_tool" if cursor is None else "later_tool"
            tool = SimpleNamespace(
                name=name,
                model_dump=lambda **_kwargs: {
                    "name": name, "description": "Paginated specialist tool"
                },
            )
            return SimpleNamespace(
                tools=[tool], next_cursor="page-2" if cursor is None else None
            )

        async def call_tool(self, name, arguments):
            tool_calls.append((name, arguments))
            return SimpleNamespace(model_dump=lambda **_kwargs: {"content": []})

    async def check():
        with (patch.object(toolbox_mcp, "stdio_client", fake_stdio),
              patch.object(toolbox_mcp, "ClientSession",
                           lambda *_args, **_kwargs: FakeSession()),
              patch.dict(toolbox_mcp.SESSIONS, {}, clear=True),
              patch.dict(toolbox_mcp.CATALOG, {}, clear=True),
              patch.dict(toolbox_mcp.ERRORS, {}, clear=True)):
            async with toolbox_mcp.lifespan(None):
                assert discovery_calls == [None, "page-2"]
                assert toolbox_mcp.toolbox_status()["backends"]["rea"]["tools"] == 2
                names = {item["name"] for item in
                         toolbox_mcp.toolbox_search("later_tool")["tools"]}
                assert "rea.later_tool" in names
                assert toolbox_mcp.toolbox_schema("rea.later_tool")["name"] == "later_tool"
                result = await toolbox_mcp.toolbox_call("rea.later_tool", '{"value": 7}')
                assert result == {"content": []}
                assert tool_calls == [("later_tool", {"value": 7})]

    asyncio.run(check())


def test_stalled_backend_initialization_times_out_and_preserves_next_backend():
    closed = []

    @asynccontextmanager
    async def fake_stdio(params):
        group = "rea" if "rea.mjs" in " ".join(params.args) else "devtools"
        try:
            yield group, object()
        finally:
            closed.append((group, "stdio"))

    class FakeSession:
        def __init__(self, group):
            self.group = group

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed.append((self.group, "session"))

        async def initialize(self):
            if self.group == "rea":
                await asyncio.Future()
            else:
                assert closed == [("rea", "session"), ("rea", "stdio")]

        async def list_tools(self):
            assert self.group == "devtools"
            tool = SimpleNamespace(
                name="healthy",
                model_dump=lambda **_kwargs: {"name": "healthy"},
            )
            return SimpleNamespace(tools=[tool])

    async def check():
        with (patch.object(toolbox_mcp, "DISCOVERY_SECONDS", 0.01),
              patch.object(toolbox_mcp, "stdio_client", fake_stdio),
              patch.object(toolbox_mcp, "ClientSession",
                           lambda group, *_args, **_kwargs: FakeSession(group)),
              patch.dict(toolbox_mcp.SESSIONS, {}, clear=True),
              patch.dict(toolbox_mcp.CATALOG, {}, clear=True),
              patch.dict(toolbox_mcp.ERRORS, {}, clear=True)):
            async with toolbox_mcp.lifespan(None):
                status = toolbox_mcp.toolbox_status()
                assert status["backends"]["rea"]["available"] is False
                assert "TimeoutError" in status["backends"]["rea"]["error"]
                assert status["backends"]["devtools"]["available"] is True
                assert set(toolbox_mcp.CATALOG) == {"devtools.healthy"}
                assert toolbox_mcp.toolbox_schema("local.make_music")
            assert closed == [
                ("rea", "session"), ("rea", "stdio"),
                ("devtools", "session"), ("devtools", "stdio"),
            ]

    asyncio.run(asyncio.wait_for(check(), timeout=1))


@pytest.mark.parametrize("stalled_stage", ["stdio", "session"])
def test_stalled_backend_entry_times_out_and_preserves_next_backend(stalled_stage):
    closed = []

    @asynccontextmanager
    async def fake_stdio(params):
        group = "rea" if "rea.mjs" in " ".join(params.args) else "devtools"
        try:
            if group == "rea" and stalled_stage == "stdio":
                await asyncio.Future()
            yield group, object()
        finally:
            closed.append((group, "stdio"))

    class FakeSession:
        def __init__(self, group):
            self.group = group

        async def __aenter__(self):
            if self.group == "rea" and stalled_stage == "session":
                await asyncio.Future()
            return self

        async def __aexit__(self, *_args):
            closed.append((self.group, "session"))

        async def initialize(self):
            assert self.group == "devtools"
            assert ("rea", "stdio") in closed

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(
                name="healthy",
                model_dump=lambda **_kwargs: {"name": "healthy"},
            )])

    async def check():
        with (patch.object(toolbox_mcp, "DISCOVERY_SECONDS", 0.01),
              patch.object(toolbox_mcp, "stdio_client", fake_stdio),
              patch.object(toolbox_mcp, "ClientSession",
                           lambda group, *_args, **_kwargs: FakeSession(group)),
              patch.dict(toolbox_mcp.SESSIONS, {}, clear=True),
              patch.dict(toolbox_mcp.CATALOG, {}, clear=True),
              patch.dict(toolbox_mcp.ERRORS, {}, clear=True)):
            async with toolbox_mcp.lifespan(None):
                assert "TimeoutError" in toolbox_mcp.ERRORS["rea"]
                assert set(toolbox_mcp.SESSIONS) == {"devtools"}
                assert set(toolbox_mcp.CATALOG) == {"devtools.healthy"}
                assert toolbox_mcp.toolbox_schema("local.make_music")
            assert closed == [
                ("rea", "stdio"),
                ("devtools", "session"),
                ("devtools", "stdio"),
            ]

    asyncio.run(asyncio.wait_for(check(), timeout=1))


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


@pytest.mark.parametrize("mode,detail", [
    ("cycle", "repeated cursor"), ("pages", "page limit"),
    ("tools", "tool limit"), ("timeout", "TimeoutError"),
])
def test_review_discovery_budget_failure_closes_backend_and_preserves_next(monkeypatch, mode, detail):
    cleanup = []
    calls = []
    monkeypatch.setattr(toolbox_mcp, "DISCOVERY_MAX_PAGES", 3, raising=False)
    monkeypatch.setattr(toolbox_mcp, "DISCOVERY_MAX_TOOLS", 2, raising=False)
    monkeypatch.setattr(toolbox_mcp, "DISCOVERY_SECONDS", .01, raising=False)
    @asynccontextmanager
    async def fake_stdio(params):
        group = "rea" if "rea.mjs" in " ".join(params.args) else "devtools"
        try:
            yield group, object()
        finally:
            cleanup.append((group, "stdio"))
    class FakeSession:
        def __init__(self, group):
            self.group = group
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            cleanup.append((self.group, "session"))
        async def initialize(self):
            pass
        async def list_tools(self, cursor=None):
            calls.append((self.group, cursor))
            if self.group == "devtools":
                assert ("rea", "stdio") in cleanup and ("rea", "session") in cleanup
                tool = SimpleNamespace(name="healthy", model_dump=lambda **_kwargs: {"name": "healthy"})
                return SimpleNamespace(tools=[tool], next_cursor=None)
            if mode == "timeout":
                try:
                    await asyncio.Future()
                finally:
                    cleanup.append((self.group, "cancelled-list"))
            count = sum(group == "rea" for group, _cursor in calls)
            if count > 8:
                raise RuntimeError("External test guard stopped unbounded discovery")
            tools = [object()] * 3 if mode == "tools" else []
            next_cursor = "same" if mode == "cycle" else str(count)
            return SimpleNamespace(tools=tools, next_cursor=next_cursor)
    async def check():
        with (patch.object(toolbox_mcp, "stdio_client", fake_stdio),
              patch.object(toolbox_mcp, "ClientSession", lambda group, *_args, **_kw: FakeSession(group)),
              patch.dict(toolbox_mcp.SESSIONS, {}, clear=True),
              patch.dict(toolbox_mcp.CATALOG, {}, clear=True),
              patch.dict(toolbox_mcp.ERRORS, {}, clear=True)):
            async with toolbox_mcp.lifespan(None):
                assert detail in toolbox_mcp.ERRORS["rea"]
                assert set(toolbox_mcp.CATALOG) == {"devtools.healthy"}
                assert "rea" not in toolbox_mcp.SESSIONS
                assert len([group for group, _cursor in calls if group == "rea"]) <= 3
                if mode == "timeout":
                    assert ("rea", "cancelled-list") in cleanup
            assert ("devtools", "session") in cleanup and ("devtools", "stdio") in cleanup
    asyncio.run(asyncio.wait_for(check(), timeout=.25))


def clinc_normalization_fixture(tmp_path,monkeypatch):
    import hashlib
    import small_training as training
    official={"train":[["shared TEST","a"],["shared val","a"],["unique train","a"],
                       [" UNIQUE   TRAIN ","a"],["conflict","a"],["CONFLICT","b"]],
              "oos_train":[["oos training","oos"]],
              "val":[["shared val","a"],[" SHARED test ","b"]],"oos_val":[["oos val","oos"]],
              "test":[["shared test","b"],["official duplicate","a"],["official duplicate","b"]],
              "oos_test":[["oos test","oos"]]}
    payloads={"LICENSE":b"Synthetic fixture license", "data/data_full.json":json.dumps(official).encode()}
    entry={"repository":"example/clinc-fixture","commit":"a"*40,"purpose":"test fixture", "license":"fixture",
           "license_path":"LICENSE","attribution":"Synthetic fixture, not a public benchmark result",
           "files":{name:hashlib.sha256(data).hexdigest() for name,data in payloads.items()}}
    monkeypatch.setattr(training,"GITHUB_DATASETS",{"clinc150":entry})
    monkeypatch.setattr(training,"_github_dataset_bytes",lambda url,limit:payloads[url.split("a"*40+"/",1)[1]])
    monkeypatch.setattr("pc_offline.offline",lambda:False)
    source=tmp_path/"downloaded"
    training.download_training_dataset("clinc150",source)
    return training,source,official,payloads


def test_clinc_normalization_preserves_official_test_and_reports_exact_leak_filtering(tmp_path,monkeypatch):
    import csv,hashlib
    training,source,official,payloads=clinc_normalization_fixture(tmp_path,monkeypatch)
    output=tmp_path/"prepared"
    result=training.normalize_training_dataset("clinc150",source,output)
    with (output/"classifier.csv").open(newline="",encoding="utf-8") as stream:rows=list(csv.DictReader(stream))
    test=[(row["text"],row["label"]) for row in rows if row["split"]=="test"]
    assert test==[tuple(pair) for pair in official["test"]+official["oos_test"]]
    assert result["normalized_counts"]=={"train":2,"validation":2,"test":4}
    assert result["filtered_rows"]=={"train_overlap_test":1,"train_overlap_validation":1,
        "train_conflicting_labels":2,"train_duplicate":1,"validation_overlap_test":1}
    assert (source/"files/data/data_full.json").read_bytes()==payloads["data/data_full.json"]
    assert result["raw_sha256"]==hashlib.sha256(payloads["data/data_full.json"]).hexdigest()
    assert result["official_test_untouched"] and not result["trained"] and not result["activated"]
    assert "dataset_folder" not in result["source"]
    assert training.normalize_training_dataset("clinc150",source,output)["cached"]


def test_clinc_normalization_rejects_changed_raw_and_derived_data_without_overwrite(tmp_path,monkeypatch):
    training,source,official,payloads=clinc_normalization_fixture(tmp_path,monkeypatch)
    output=tmp_path/"prepared";training.normalize_training_dataset("clinc150",source,output)
    target=output/"test.csv";target.write_text("my changed heldout")
    with pytest.raises(ValueError,match="preserve"):
        training.normalize_training_dataset("clinc150",source,output)
    assert target.read_text()=="my changed heldout"
    (source/"files/data/data_full.json").write_bytes(b'{}')
    with pytest.raises(ValueError,match="modified|incomplete"):
        training.normalize_training_dataset("clinc150",source,tmp_path/"new")
    assert not (tmp_path/"new").exists()


def test_clinc_preparation_tool_keeps_reviewed_raw_and_prepares_splits_in_project(tmp_path,monkeypatch):
    training,source,official,payloads=clinc_normalization_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(toolbox_mcp,"registered_project",lambda path:tmp_path)
    result=asyncio.run(toolbox_mcp.acquire_training_dataset("registered","clinc150"))
    assert result["normalized"]["dataset_path"].startswith(str(tmp_path))
    assert not result["normalized"]["trained"]


def classifier_reference_fixture(folder,class_count=3):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    texts=[];labels=[]
    themes=[("billing","invoice payment money"),("music","song melody rhythm"),("travel","flight ticket airport")]
    for label,theme in themes[:class_count]:
        for index in range(8):texts.append(theme+" request "+str(index));labels.append(label)
    vectorizer=TfidfVectorizer(ngram_range=(1,2),max_features=20000,sublinear_tf=True)
    classifier=LogisticRegression(max_iter=1000,class_weight="balanced",random_state=42)
    classifier.fit(vectorizer.fit_transform(texts),labels)
    artifact={"kind":"text_classifier","vocabulary":{k:int(v) for k,v in vectorizer.vocabulary_.items()},
              "idf":vectorizer.idf_.tolist(),"coef":classifier.coef_.tolist(),
              "intercept":classifier.intercept_.tolist(),"classes":classifier.classes_.tolist()}
    folder.mkdir(exist_ok=True);(folder/"model.json").write_text(json.dumps(artifact),encoding="utf-8")
    return vectorizer,classifier,artifact


@pytest.mark.parametrize("class_count",[2,3])
def test_portable_classifier_matches_real_sklearn_probabilities_without_runtime_imports(tmp_path,monkeypatch,class_count):
    import builtins
    import small_training as training
    vectorizer,classifier,artifact=classifier_reference_fixture(tmp_path,class_count)
    samples=["invoice invoice PAYMENT money","song melody","airport flight","","x unknownword", "CAFÉ naïve invoice", "invoice-payment\nMONEY"]
    expected=classifier.predict_proba(vectorizer.transform(samples))
    original=builtins.__import__
    def no_runtime_ml(name,*args,**kwargs):
        if name.split('.')[0] in {"sklearn","numpy"}:raise AssertionError("Portable inference imported ML runtime")
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,"__import__",no_runtime_ml)
    for text,reference in zip(samples,expected):
        result=training.predict_classifier(tmp_path,text)
        assert list(result["probabilities"])==classifier.classes_.tolist()
        assert all(abs(actual-float(wanted))<=1e-12 for actual,wanted in zip(result["probabilities"].values(),reference))
        assert result["label"]==classifier.classes_[reference.argmax()]
        assert len(result["model_sha256"])==64


def test_classifier_cache_is_bounded_reuses_json_and_invalidates_modified_artifacts(tmp_path,monkeypatch):
    import small_training as training
    training._CLASSIFIER_CACHE.clear()
    folders=[tmp_path/str(i) for i in range(3)]
    for folder in folders:classifier_reference_fixture(folder,2)
    original=training._bounded_bytes;reads=[]
    def counted(path,limit):reads.append(str(path));return original(path,limit)
    monkeypatch.setattr(training,"_bounded_bytes",counted)
    first=training.predict_classifier(folders[0],"invoice")
    training.predict_classifier(folders[0],"song")
    assert len(reads)==1
    for folder in folders[1:]:training.predict_classifier(folder,"invoice")
    assert len(training._CLASSIFIER_CACHE)==2
    training.predict_classifier(folders[0],"invoice")
    assert len(reads)==4
    artifact=json.loads((folders[0]/"model.json").read_text())
    artifact["intercept"][0]+=3
    (folders[0]/"model.json").write_text(json.dumps(artifact)+" ")
    changed=training.predict_classifier(folders[0],"invoice")
    assert changed["model_sha256"]!=first["model_sha256"] and len(reads)==5


@pytest.mark.parametrize("mutation",["index","shape","nan","boolean","classes","feature_config"])
def test_portable_classifier_rejects_malformed_artifacts(tmp_path,mutation):
    import small_training as training
    _,_,artifact=classifier_reference_fixture(tmp_path,2)
    if mutation=="index":artifact["vocabulary"][next(iter(artifact["vocabulary"]))]=True
    elif mutation=="shape":artifact["coef"][0].pop()
    elif mutation=="nan":artifact["idf"][0]=float('nan')
    elif mutation=="boolean":artifact["intercept"][0]=True
    elif mutation=="classes":artifact["classes"][1]=artifact["classes"][0]
    elif mutation=="feature_config":artifact["feature_config"]={"ngram_range":[1,3]}
    (tmp_path/"model.json").write_text(json.dumps(artifact))
    with pytest.raises(ValueError):training.predict_classifier(tmp_path,"invoice")


def test_classify_tool_uses_cpu_thread_not_media_worker(tmp_path,monkeypatch):
    import small_training as training
    classifier_reference_fixture(tmp_path,2)
    async def no_media(*args,**kwargs):pytest.fail("Classifier started media subprocess")
    monkeypatch.setattr(toolbox_mcp,"media_run",no_media)
    result=asyncio.run(toolbox_mcp.classify_text(str(tmp_path),"invoice payment"))
    assert result["label"]=="billing"


def split_classifier_csv(path):
    import csv
    with path.open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=['text','label','split']);writer.writeheader()
        for split,count,token in [('train',8,'trainingonly'),('validation',2,'validationonly'),('test',3,'heldoutonly')]:
            for label,theme in [('billing','invoice payment'),('music','song melody'),('travel','flight airport')]:
                for index in range(count):writer.writerow({'text':theme+' '+token+' '+str(index),'label':label,'split':split})


def test_classifier_training_uses_only_train_and_keeps_candidate_with_baseline_and_latency(tmp_path):
    import hashlib
    import small_training as training
    dataset=tmp_path/'examples.csv';split_classifier_csv(dataset)
    report=training.train_classifier(dataset,tmp_path/'model')
    artifact=json.loads((tmp_path/'model/model.json').read_text())
    assert 'trainingonly' in artifact['vocabulary']
    assert 'heldoutonly' not in artifact['vocabulary'] and 'validationonly' not in artifact['vocabulary']
    assert report['training_examples']==24 and report['held_out_examples']==9 and report['validation_examples']==6
    assert report['evaluation_split']=='provided_test'
    assert report['accuracy']>=report['majority_baseline_accuracy']
    assert report['portable_latency']['device']=='cpu' and report['portable_latency']['samples']==9
    registry=json.loads((tmp_path/'model/candidate.json').read_text())
    assert registry['status']=='candidate' and not registry['activated']
    assert registry['evaluation_sha256']==hashlib.sha256((tmp_path/'model/evaluation.json').read_bytes()).hexdigest()
    assert registry['model_sha256']==hashlib.sha256((tmp_path/'model/model.json').read_bytes()).hexdigest()
    assert training.predict_classifier(tmp_path/'model','invoice payment')['label']=='billing'


def test_classifier_rejects_heldout_text_overlap_before_fitting(tmp_path,monkeypatch):
    import csv
    import small_training as training
    from sklearn.linear_model import LogisticRegression
    dataset=tmp_path/'examples.csv';split_classifier_csv(dataset)
    with dataset.open('a',newline='') as stream:
        csv.writer(stream).writerow([' INVOICE   payment trainingonly 0 ','billing','test'])
    monkeypatch.setattr(LogisticRegression,'fit',lambda *args,**kwargs:pytest.fail('Leaked data was fitted'))
    with pytest.raises(ValueError,match='overlaps held-out'):
        training.train_classifier(dataset,tmp_path/'model')
    assert not (tmp_path/'model').exists()


def test_classifier_plain_csv_legacy_holdout_still_roundtrips_without_activation(tmp_path):
    import csv
    import small_training as training
    dataset=tmp_path/'plain.csv'
    with dataset.open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['text','label'])
        for label,word in [('a','apples'),('b','buses')]:
            for index in range(10):writer.writerow([word+' example '+str(index),label])
    result=training.train_classifier(dataset,tmp_path/'model')
    assert result['evaluation_split']=='random_stratified_holdout'
    assert result['training_examples']==16 and result['held_out_examples']==4 and result['status']=='candidate'
    assert training.predict_classifier(tmp_path/'model','apples')['label']=='a'


def test_classifier_input_and_artifact_budgets_reject_before_loading(tmp_path,monkeypatch):
    import small_training as training
    with pytest.raises(ValueError,match="16384"):
        training.predict_classifier(tmp_path,"x"*16385)
    (tmp_path/"model.json").write_bytes(b'x'*65)
    monkeypatch.setattr(training,"CLASSIFIER_MAX_BYTES",64)
    monkeypatch.setattr(training.json,"loads",lambda *args,**kwargs:pytest.fail("Oversized artifact was decoded"))
    with pytest.raises(ValueError,match="read budget"):
        training.predict_classifier(tmp_path,"small")


@pytest.mark.parametrize('bad_split',[None,[["text",False]],[["",'intent']]])
def test_clinc_malformed_reviewed_bytes_do_not_publish_normalized_output(tmp_path,monkeypatch,bad_split):
    import hashlib
    training,source,official,payloads=clinc_normalization_fixture(tmp_path,monkeypatch)
    official['train']=bad_split
    raw=json.dumps(official).encode();(source/'files/data/data_full.json').write_bytes(raw)
    entry=training.GITHUB_DATASETS['clinc150'];entry['files']['data/data_full.json']=hashlib.sha256(raw).hexdigest()
    (source/'source.json').write_text(json.dumps(training._dataset_manifest('clinc150',entry,sum(map(len,payloads.values())))))
    with pytest.raises(ValueError):training.normalize_training_dataset('clinc150',source,tmp_path/'prepared')
    assert not (tmp_path/'prepared').exists()


def test_classifier_subset_filters_each_official_split_before_fitting_and_hashes_policy(tmp_path):
    import hashlib
    import small_training as training
    dataset=tmp_path/'split.csv';split_classifier_csv(dataset)
    result=training.train_classifier(dataset,tmp_path/'subset',labels=['music','billing'])
    artifact=json.loads((tmp_path/'subset/model.json').read_text())
    assert artifact['classes']==['billing','music'] and 'flight' not in artifact['vocabulary']
    assert result['training_examples']==16 and result['held_out_examples']==6 and result['validation_examples']==4
    policy=result['subset_policy']
    assert policy['selected_labels']==['billing','music'] and policy['applied_before_fit']
    assert policy['retained_split_counts']=={'train':16,'validation':4,'test':6}
    assert result['subset_policy_sha256']==hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest()
    assert not result['activated']



def game_library_fixture(tmp_path,monkeypatch):
    """Tiny local fixture exercises publication; real engine smoke is separate."""
    import hashlib
    root=tmp_path/'runtime';root.mkdir();(root/'LongTasks').mkdir()
    project=tmp_path/'registered-project';project.mkdir()
    with sqlite3.connect(root/'LongTasks/projects.db') as db:
        db.execute('CREATE TABLE projects(folder TEXT)');db.execute('INSERT INTO projects VALUES(?)',(str(project),))
    payloads={};pins={}
    for name in ('phaser','yuka'):
        payloads[name+'/engine.js']=('/* fixture '+name+' */').encode()
        payloads[name+'/LICENSE']=b'MIT fixture attribution'
        files=[]
        for filename,key in ((name+'.min.js',name+'/engine.js'),('LICENSE',name+'/LICENSE')):
            body=payloads[key];url='https://raw.githubusercontent.com/fixture/'+name+'/'+'a'*40+'/'+filename
            files.append({'source_path':filename,'url':url,'sha256':hashlib.sha256(body).hexdigest(),'bytes':len(body)})
        pins[name]={'repo':'fixture/'+name,'commit':'a'*40,'version':'fixture','files':files}
    monkeypatch.setattr(toolbox_mcp,'ROOT',root);monkeypatch.setattr(toolbox_mcp,'GAME_LIBRARIES',pins)
    downloads=[]
    def fetch(url,size):
        downloads.append(url);name=url.split('/')[4]
        return payloads[name+('/LICENSE' if url.endswith('/LICENSE') else '/engine.js')]
    monkeypatch.setattr(toolbox_mcp,'_game_library_download',fetch)
    return root,project,pins,downloads


def test_install_game_libraries_is_discoverable_guarded_and_idempotently_offline(tmp_path,monkeypatch):
    root,project,pins,downloads=game_library_fixture(tmp_path,monkeypatch)
    result=asyncio.run(toolbox_mcp.toolbox_call('local.install_game_libraries',json.dumps({'project_folder':str(project)})))
    assert result['installed'] and result['written']==8 and not result['executed']
    assert len(downloads)==4
    assert {row['name'] for row in result['libraries']}=={'phaser','yuka'}
    assert (project/'vendor/cale-game/phaser/LICENSE').read_bytes()==b'MIT fixture attribution'
    assert 'Phaser.CANVAS' in (project/'vendor/cale-game/phaser/API.md').read_text()
    assert 'FollowPathBehavior' in (project/'vendor/cale-game/yuka/API.md').read_text()
    monkeypatch.setattr(toolbox_mcp,'_game_library_download',lambda *_:pytest.fail('Verified cache must not download'))
    again=asyncio.run(toolbox_mcp.install_game_libraries(str(project)))
    assert again['written']==0 and again['reused']==8 and again['files']==result['files']
    assert 'local.install_game_libraries' in toolbox_mcp.LOCAL_CATALOG


@pytest.mark.parametrize('libraries',[[],['unknown'],['phaser','phaser'],['phaser','yuka','phaser'],'phaser',None,[True]])
def test_game_libraries_reject_invalid_requests_without_download_or_project_writes(tmp_path,monkeypatch,libraries):
    root,project,pins,downloads=game_library_fixture(tmp_path,monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(toolbox_mcp.install_game_libraries(str(project),libraries))
    assert not downloads and list(project.iterdir())==[]


def test_game_libraries_unregistered_folder_cannot_download_or_install(tmp_path,monkeypatch):
    root,project,pins,downloads=game_library_fixture(tmp_path,monkeypatch)
    other=tmp_path/'not-a-task';other.mkdir()
    with pytest.raises(ValueError,match='exact folder'):
        asyncio.run(toolbox_mcp.install_game_libraries(str(other)))
    assert not downloads and list(other.iterdir())==[]


@pytest.mark.parametrize('body',[b'wrong content',b'X'*2_000_001])
def test_game_libraries_hash_size_failure_never_publishes_project_code(tmp_path,monkeypatch,body):
    root,project,pins,downloads=game_library_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(toolbox_mcp,'_game_library_download',lambda *_:body)
    with pytest.raises(ValueError,match='hash/size'):
        asyncio.run(toolbox_mcp.install_game_libraries(str(project)))
    assert list(project.iterdir())==[]


def test_game_libraries_preserve_every_existing_file_on_conflict(tmp_path,monkeypatch):
    root,project,pins,downloads=game_library_fixture(tmp_path,monkeypatch)
    directory=project/'vendor/cale-game/yuka';directory.mkdir(parents=True)
    target=directory/'yuka.min.js';target.write_bytes(b'user authored library')
    with pytest.raises(ValueError,match='preserved'):
        asyncio.run(toolbox_mcp.install_game_libraries(str(project)))
    assert target.read_bytes()==b'user authored library'
    assert not (project/'vendor/cale-game/phaser').exists()  # Whole project preflight first.


def test_game_libraries_concurrent_creator_is_never_overwritten_or_rolled_back(tmp_path,monkeypatch):
    root,project,pins,downloads=game_library_fixture(tmp_path,monkeypatch)
    original=toolbox_mcp._game_library_write_new
    target=project/'vendor/cale-game/phaser/LICENSE'
    def race(path,value):
        if path==target:
            path.write_bytes(b'concurrent user file')
        return original(path,value)
    monkeypatch.setattr(toolbox_mcp,'_game_library_write_new',race)
    with pytest.raises(ValueError,match='preserved'):
        asyncio.run(toolbox_mcp.install_game_libraries(str(project)))
    assert target.read_bytes()==b'concurrent user file'
    assert (target.parent/'phaser.min.js').read_bytes()==b'/* fixture phaser */'


def test_game_libraries_corrupt_cache_fails_without_network_or_project_changes(tmp_path,monkeypatch):
    root,project,pins,downloads=game_library_fixture(tmp_path,monkeypatch)
    cache=root/'Toolbox/game-libraries/phaser'/('a'*40);cache.mkdir(parents=True)
    (cache/'phaser.min.js').write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='hash/size'):
        asyncio.run(toolbox_mcp.install_game_libraries(str(project)))
    assert not downloads and list(project.iterdir())==[]


def test_game_libraries_missing_offline_cache_has_clear_failure(tmp_path,monkeypatch):
    import pc_offline
    root,project,pins,downloads=game_library_fixture(tmp_path,monkeypatch)
    monkeypatch.undo()
    # Exercise real downloader's offline gate; no socket may be opened.
    monkeypatch.setattr(pc_offline,'offline',lambda:True)
    with pytest.raises(ValueError,match='Offline'):
        toolbox_mcp._game_library_download('https://raw.githubusercontent.com/approved/pin/file',10)
    assert list(project.iterdir())==[]


def test_game_libraries_reject_redirected_vendor_directory(tmp_path,monkeypatch):
    root,project,pins,downloads=game_library_fixture(tmp_path,monkeypatch)
    outside=tmp_path/'outside';outside.mkdir()
    try:
        (project/'vendor').symlink_to(outside,target_is_directory=True)
    except OSError as error:
        pytest.skip('Symlinks unavailable: '+str(error))
    with pytest.raises(ValueError,match='symlinks or junctions'):
        asyncio.run(toolbox_mcp.install_game_libraries(str(project)))
    assert list(outside.iterdir())==[]



def test_game_library_install_keeps_async_event_loop_responsive(tmp_path,monkeypatch):
    import threading
    root,project,pins,downloads=game_library_fixture(tmp_path,monkeypatch)
    original=toolbox_mcp._game_library_download;started=threading.Event();release=threading.Event()
    def blocking_fetch(*args):
        started.set()
        assert release.wait(3),'Installer must yield to the caller while downloading'
        return original(*args)
    monkeypatch.setattr(toolbox_mcp,'_game_library_download',blocking_fetch)
    async def scenario():
        task=asyncio.create_task(toolbox_mcp.install_game_libraries(str(project)))
        try:
            assert await asyncio.to_thread(started.wait,2)
            await asyncio.sleep(.01)
            assert not task.done()
        finally:
            release.set()
        assert (await task)['installed']
    asyncio.run(scenario())


def test_game_library_download_has_bounded_read_and_rejects_redirects(monkeypatch):
    import urllib.request,pc_offline
    monkeypatch.setattr(pc_offline,'offline',lambda:False)
    handlers=[];sizes=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*_args):pass
        def read(self,size):sizes.append(size);return b'fixture'
    class Opener:
        def open(self,request,timeout):assert timeout==20;return Response()
    def opener(handler):handlers.append(handler() if isinstance(handler,type) else handler);return Opener()
    monkeypatch.setattr(urllib.request,'build_opener',opener)
    assert toolbox_mcp._game_library_download('https://raw.githubusercontent.com/approved/pin/library.js',100)==b'fixture'
    assert sizes==[101]
    with pytest.raises(ValueError,match='cannot redirect'):
        handlers[0].redirect_request(None,None,302,'redirect',{},'https://unapproved.example/payload')


def test_game_library_vendor_junction_is_rejected_before_download_on_windows(tmp_path,monkeypatch):
    import os,subprocess
    if os.name!='nt':pytest.skip('Windows junction-specific coverage')
    root,project,pins,downloads=game_library_fixture(tmp_path,monkeypatch)
    outside=tmp_path/'outside-junction';outside.mkdir();link=project/'vendor'
    process=subprocess.run(['cmd','/d','/c','mklink','/J',str(link),str(outside)],capture_output=True,text=True,
                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert process.returncode==0,process.stderr
    try:
        assert link.is_junction()
        with pytest.raises(ValueError,match='symlinks or junctions'):
            asyncio.run(toolbox_mcp.install_game_libraries(str(project)))
        assert not downloads and list(outside.iterdir())==[]
    finally:
        os.rmdir(link)  # Remove the junction entry only, never its target tree.



def test_game_library_removed_task_during_download_prevents_project_publication(tmp_path,monkeypatch):
    root,project,pins,downloads=game_library_fixture(tmp_path,monkeypatch)
    original=toolbox_mcp._game_library_download
    def removed(*args):
        with sqlite3.connect(root/'LongTasks/projects.db') as db:
            db.execute('DELETE FROM projects')
        return original(*args)
    monkeypatch.setattr(toolbox_mcp,'_game_library_download',removed)
    with pytest.raises(ValueError,match='exact folder'):
        asyncio.run(toolbox_mcp.install_game_libraries(str(project)))
    assert list(project.iterdir())==[]
