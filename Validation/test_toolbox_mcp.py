"""A broken optional backend must not disable independent local tools."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import toolbox_mcp


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
