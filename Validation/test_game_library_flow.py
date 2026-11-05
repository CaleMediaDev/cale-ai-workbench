"""Exercise catalog-to-task handoff and contained previews without a cloud model."""

import hashlib
import asyncio
import io
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

import game_library_ui
import native_agent
import project_preview
import toolbox_mcp


def test_native_import_declares_actual_nested_game_and_keeps_existing_server(tmp_path):
    game = {
        "id": "example",
        "title": "Example",
        "source_url": "https://github.com/example/game",
    }

    def import_source(_game_id, destination):
        folder = Path(destination)
        folder.mkdir()
        (folder / "index.html").write_text("<h1>Playable source</h1>", encoding="utf-8")
        receipt = folder / "CALE-IMPORT.json"
        receipt.write_text("{}", encoding="utf-8")
        return {
            "folder": str(folder),
            "entrypoint": "index.html",
            "game": game,
            "receipt_path": str(receipt),
        }

    with patch("design_tools.import_game_example", side_effect=import_source):
        result = json.loads(
            native_agent.execute_tool(
                tmp_path, "import_game_example", {"game_id": "example", "path": "game"}
            )
        )
        assert result["verified_gameplay"] is False
        assert result["entrypoint"] == "game/index.html"
        assert (
            project_preview.source(tmp_path) == (tmp_path / "game/index.html").as_uri()
        )
        declaration = tmp_path / ".local-ai/preview.json"
        declaration.write_text('{"url":"http://127.0.0.1:4123/"}', encoding="utf-8")
        native_agent.execute_tool(
            tmp_path, "import_game_example", {"game_id": "example", "path": "second"}
        )
        assert project_preview.source(tmp_path) == "http://127.0.0.1:4123/"


@pytest.mark.parametrize(
    "relative", ["../outside.html", "https://example.com/index.html", "game/data.txt"]
)
def test_declared_game_preview_cannot_escape_or_show_non_html(tmp_path, relative):
    (tmp_path / ".local-ai").mkdir()
    (tmp_path / ".local-ai/preview.json").write_text(
        json.dumps({"file": relative}), encoding="utf-8"
    )
    assert project_preview.source(tmp_path) == ""


def test_native_catalog_is_paginated_valid_json_under_tool_budget(tmp_path):
    result = native_agent.execute_tool(tmp_path, "game_library_search", {"limit": 12})
    data = json.loads(result)
    assert data["total"] == 182
    assert 0 < len(data["games"]) <= 12
    assert len(result) < native_agent.MAX_TOOL_OUTPUT
    assert data["next_offset"] == len(data["games"])


def test_optional_game_tools_are_discoverable_without_crowding_small_contexts(
    tmp_path, monkeypatch
):
    # Catalog schemas belong in discovery, not every ordinary chat/function turn.
    names = {tool["function"]["name"] for tool in native_agent.TOOLS}
    assert "game_library_search" not in names and "import_game_example" not in names
    result = asyncio.run(
        toolbox_mcp.toolbox_call("local.game_library_search", '{"limit":2}')
    )
    assert len(result["games"]) == 2 and result["total"] == 182
    root = tmp_path / "app"
    (root / "LongTasks").mkdir(parents=True)
    project = tmp_path / "task"
    project.mkdir()
    with sqlite3.connect(root / "LongTasks/projects.db") as database:
        database.execute("CREATE TABLE projects(folder TEXT)")
        database.execute("INSERT INTO projects VALUES(?)", (str(project),))
    monkeypatch.setattr(toolbox_mcp, "ROOT", root)
    with patch.object(
        native_agent, "execute_tool", return_value='{"verified_gameplay":false}'
    ) as execute:
        result = asyncio.run(
            toolbox_mcp.toolbox_call(
                "local.import_game_example",
                json.dumps(
                    {
                        "project_folder": str(project),
                        "game_id": "example",
                        "path": "game",
                    }
                ),
            )
        )
        assert result["verified_gameplay"] is False
        execute.assert_called_once_with(
            project.resolve(),
            "import_game_example",
            {"game_id": "example", "path": "game"},
        )
        with pytest.raises(ValueError):
            asyncio.run(
                toolbox_mcp.import_game_example(str(tmp_path), "example", "game")
            )


def test_pinned_preview_offline_cache_verifies_git_blob_and_never_calls_network(
    tmp_path,
):
    stream = io.BytesIO()
    Image.new("RGB", (800, 600), "cyan").save(stream, format="PNG")
    raw = stream.getvalue()
    digest = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    cache = tmp_path / "Toolbox/game-previews" / (digest + ".image")
    cache.parent.mkdir(parents=True)
    cache.write_bytes(raw)
    game = {
        "preview_url": "https://raw.githubusercontent.com/MartinDelophy/awesome-gpt-6-astra/"
        + "a" * 40
        + "/assets/example.png",
        "preview_blob_sha": digest,
        "preview_bytes": len(raw),
    }
    with (
        patch.object(game_library_ui, "ROOT", tmp_path),
        patch("pc_offline.offline", return_value=True),
        patch("urllib.request.urlopen") as network,
    ):
        image = game_library_ui.load_catalog_preview(game)
        assert image.width <= 280 and image.height <= 190
        cache.write_bytes(b"x" * len(raw))
        assert game_library_ui.load_catalog_preview(game) is None
        network.assert_not_called()
