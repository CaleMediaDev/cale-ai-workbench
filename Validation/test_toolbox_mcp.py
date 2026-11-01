"""A broken optional backend must not disable independent local tools."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import toolbox_mcp


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
