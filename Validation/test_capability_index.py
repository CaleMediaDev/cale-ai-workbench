"""The local skill and tool database stays searchable and rebuildable."""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import capability_index


def fixture(root):
    skill = root / "vendor/agent-skills/frontend-ui-engineering/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\ndescription: Build accessible websites with clear layouts.\n---\n\nUse semantic HTML.")
    tools = [{"name": "make_image", "description": "Generate a local image",
              "schema": {"type": "object", "properties": {"prompt": {"type": "string"}}}}]
    return skill, tools


def test_skills_and_tools_are_indexed_searched_and_refreshed():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        skill, tools = fixture(root)
        with patch.object(capability_index, "_registered_tools", return_value=tools):
            first = capability_index.sync(root)
            assert first["skills"] == 1 and first["tools"] == 1
            assert capability_index.health(root)["ok"]
            result = capability_index.search(root, "accessible website", kind="skill")
            assert result[0]["id"] == "skill:frontend-ui-engineering"
            assert "semantic HTML" in capability_index.read_skill(root, result[0]["id"])["content"]
            assert capability_index.search(root, "image", kind="tool")[0]["name"] == "make_image"
            assert capability_index.sync(root)["updated"] == 0
            skill.write_text(skill.read_text() + "\nCheck contrast.")
            assert capability_index.sync(root)["updated"] == 1
            assert "Check contrast" in capability_index.read_skill(root, result[0]["id"])["content"]


def test_identical_installed_skill_replaces_bundled_path():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        bundled, tools = fixture(root)
        content = bundled.read_bytes()
        expected_text = bundled.read_text(encoding="utf-8-sig")
        installed = root / "data/skills" / bundled.parent.name / "SKILL.md"
        with patch.object(capability_index, "_registered_tools", return_value=tools):
            capability_index.sync(root)
            installed.parent.mkdir(parents=True)
            installed.write_bytes(content)
            assert capability_index.sync(root)["updated"] == 1
            result = capability_index.search(root, "accessible", kind="skill")[0]
            assert result["path"] == installed.relative_to(root).as_posix()
            bundled.unlink()
            live = capability_index.read_skill(root, result["id"])
            assert live["path"] == str(installed.resolve())
            assert installed.read_bytes() == content
            assert live["content"] == expected_text
            assert capability_index.sync(root)["updated"] == 0


def test_missing_fts_table_is_repopulated_for_unchanged_capabilities():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        skill, tools = fixture(root)
        original = skill.read_bytes()
        with patch.object(capability_index, "_registered_tools", return_value=tools):
            capability_index.sync(root)
            with capability_index._database(root) as conn:
                conn.execute("DROP TABLE capabilities_fts")
            result = capability_index.sync(root)
            assert result["skills"] == 1 and result["tools"] == 1
            assert result["updated"] == 2
            assert capability_index.search(root, "accessible", kind="skill")[0]["id"] == "skill:frontend-ui-engineering"
            assert capability_index.search(root, "image", kind="tool")[0]["name"] == "make_image"
            assert capability_index.health(root)["ok"]
            assert skill.read_bytes() == original
            assert capability_index.sync(root)["updated"] == 0


def test_damaged_index_is_preserved_and_rebuilt():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _, tools = fixture(root)
        path = capability_index.database_path(root)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not a SQLite database")
        with patch.object(capability_index, "_registered_tools", return_value=tools):
            result = capability_index.sync(root)
        assert result["skills"] == 1 and result["tools"] == 1
        assert capability_index.health(root)["ok"]
        assert list(path.parent.glob("capabilities.sqlite3.corrupt-*"))


def test_busy_index_is_not_moved_or_rebuilt():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _, tools = fixture(root)
        with patch.object(capability_index, "_registered_tools", return_value=tools):
            capability_index.sync(root)
        path = capability_index.database_path(root)
        original = path.read_bytes()
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with patch.object(capability_index, "_sync_once", side_effect=error) as refresh:
            try:
                capability_index.sync(root)
            except sqlite3.OperationalError as caught:
                assert caught is error
            else:
                raise AssertionError("Expected the busy database error to propagate")
            refresh.assert_called_once_with(root)
        assert path.read_bytes() == original
        assert not list(path.parent.glob("capabilities.sqlite3.corrupt-*"))
        assert capability_index.health(root)["ok"]


def test_non_object_specialist_snapshot_does_not_block_local_index():
    from types import SimpleNamespace

    async def list_tools():
        return []

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _, tools = fixture(root)
        tool = tools[0]
        modules = {
            "native_agent": SimpleNamespace(TOOLS=[]),
            "toolbox_mcp": SimpleNamespace(
                server=SimpleNamespace(list_tools=list_tools),
                LOCAL_CATALOG={tool["name"]: {
                    "description": tool["description"],
                    "inputSchema": tool["schema"],
                }}),
        }
        snapshot = capability_index.specialist_snapshot_path(root)
        snapshot.parent.mkdir(parents=True)
        with patch.dict("sys.modules", modules):
            for value in (None, [], "invalid snapshot", 42):
                snapshot.write_text(json.dumps(value), encoding="utf-8")
                result = capability_index.sync(root)
                assert result["skills"] == 1 and result["tools"] == 1
                assert capability_index.search(root, "accessible", kind="skill")[0]["id"] == "skill:frontend-ui-engineering"
                assert capability_index.search(root, "image", kind="tool")[0]["name"] == "make_image"


def test_malformed_specialist_descriptions_do_not_block_index():
    from types import SimpleNamespace

    async def list_tools():
        return []

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _, tools = fixture(root)
        tool = tools[0]
        modules = {
            "native_agent": SimpleNamespace(TOOLS=[]),
            "toolbox_mcp": SimpleNamespace(
                server=SimpleNamespace(list_tools=list_tools),
                LOCAL_CATALOG={tool["name"]: {
                    "description": tool["description"],
                    "inputSchema": tool["schema"],
                }}),
        }
        snapshot = capability_index.specialist_snapshot_path(root)
        snapshot.parent.mkdir(parents=True)
        valid = {"name": "devtools.list_pages", "description": "List browser pages",
                 "schema": {"type": "object"}}
        with patch.dict("sys.modules", modules):
            for fields in ({}, {"description": None}, {"description": 42}):
                malformed = {"name": "rea.invalid", "schema": {"type": "object"},
                             **fields}
                snapshot.write_text(json.dumps({"tools": [malformed, valid]}),
                                    encoding="utf-8")
                result = capability_index.sync(root)
                assert result["skills"] == 1 and result["tools"] == 2
                assert capability_index.health(root)["ok"]
                assert capability_index.search(root, "accessible", kind="skill")[0]["id"] == "skill:frontend-ui-engineering"
                assert capability_index.search(root, "image", kind="tool")[0]["name"] == "make_image"
                assert capability_index.search(root, "browser pages", kind="tool")[0]["name"] == "devtools.list_pages"
                assert {row["name"] for row in capability_index.search(root, "", kind="tool")} == {"make_image", "devtools.list_pages"}


def test_specialist_snapshot_joins_fast_tool_index():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture(root)
        snapshot = capability_index.specialist_snapshot_path(root)
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(json.dumps({"tools": [{"name": "devtools.list_pages",
            "description": "List browser pages", "schema": {"type": "object"}},
            {"name": "rea.list_strings", "description": "Inspect binary strings",
             "schema": {"type": "object"}}]}), encoding="utf-8")
        capability_index.sync(root)
        assert capability_index.search(root, "browser pages", kind="tool")[0]["name"] == "devtools.list_pages"
        assert capability_index.search(root, "binary strings", kind="tool")[0]["name"] == "rea.list_strings"
