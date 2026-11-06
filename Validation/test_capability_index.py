"""The local skill and tool database stays searchable and rebuildable."""

import json
import sqlite3
import tempfile
import pytest
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
            # Change tool description without changing schema hash
            tools_updated = [{'name': 'make_image', 'description': 'Generate a local image with improved quality.', 'schema': {'type': 'object', 'properties': {'prompt': {'type': 'string'}}}}] 
            with patch.object(capability_index, "_registered_tools", return_value=tools_updated):
                assert capability_index.sync(root)["updated"] == 1
            skill.write_text(skill.read_text() + "\nCheck contrast.")
            assert capability_index.sync(root)["updated"] == 1
            assert "Check contrast" in capability_index.read_skill(root, result[0]["id"])["content"]


def test_front_matter_description_supports_lf_and_crlf():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        skill, tools = fixture(root)
        with patch.object(capability_index, "_registered_tools", return_value=tools):
            for newline in ("\n", "\r\n"):
                content = newline.join([
                    "---",
                    "description: Build accessible websites with clear layouts.",
                    "---",
                    "",
                    "Use semantic HTML.",
                    "",
                ])
                skill.write_bytes(content.encode("utf-8"))
                capability_index.sync(root)
                result = capability_index.search(root, "accessible", kind="skill")
                assert len(result) == 1
                assert result[0]["description"] == "Build accessible websites with clear layouts."
                assert skill.read_bytes() == content.encode("utf-8")


def test_empty_front_matter_description_uses_first_sentence():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        skill, tools = fixture(root)
        with patch.object(capability_index, "_registered_tools", return_value=tools):
            for whitespace in ("", "   ", "\t"):
                skill.write_text(
                    "---\ndescription:" + whitespace + "\n"
                    "name: frontend-ui-engineering\n---\n\n"
                    "# Frontend engineering\n\nBuild accessible websites.\n",
                    encoding="utf-8")
                capability_index.sync(root)
                result = capability_index.search(root, "accessible", kind="skill")
                assert len(result) == 1
                assert result[0]["description"] == "Build accessible websites."


def test_quoted_empty_front_matter_description_uses_first_sentence():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        skill, tools = fixture(root)
        with patch.object(capability_index, "_registered_tools", return_value=tools):
            for value in ('""', "''", '"   "', "'   '"):
                skill.write_text(
                    "---\ndescription: " + value + "\n"
                    "name: frontend-ui-engineering\n---\n\n"
                    "# Frontend engineering\n\nBuild accessible websites.\n",
                    encoding="utf-8")
                capability_index.sync(root)
                result = capability_index.search(root, "accessible", kind="skill")
                assert len(result) == 1
                assert result[0]["description"] == "Build accessible websites."


def test_self_referential_skill_symlink_does_not_block_index():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        skill, tools = fixture(root)
        invalid = root / "vendor/agent-skills/cyclic/SKILL.md"
        invalid.parent.mkdir(parents=True)
        try:
            invalid.symlink_to("SKILL.md")
        except OSError as error:
            if getattr(error, "winerror", None) != 1314:
                raise
            pytest.skip("Windows account cannot create symbolic links")
        with patch.object(capability_index, "_registered_tools", return_value=tools):
            result = capability_index.sync(root)
            assert result["skills"] == 1 and result["tools"] == 1
            assert capability_index.health(root)["ok"]
            assert capability_index.search(root, "accessible", kind="skill")[0]["id"] == "skill:" + skill.parent.name
            assert capability_index.search(root, "image", kind="tool")[0]["name"] == "make_image"
            assert capability_index.sync(root)["updated"] == 0
        assert invalid.is_symlink()
        assert invalid.readlink() == Path("SKILL.md")


def test_skill_link_is_skipped_before_resolving_its_target(monkeypatch, tmp_path):
    skill, tools = fixture(tmp_path)
    invalid = tmp_path / "vendor/agent-skills/cyclic/SKILL.md"
    invalid.parent.mkdir(parents=True)
    invalid.write_text("unreviewed link target", encoding="utf-8")
    is_link, resolve = Path.is_symlink, Path.resolve

    def classified_link(path):
        return path == invalid or is_link(path)

    def guarded_resolve(path, *args, **kwargs):
        assert path != invalid, "A known symbolic link must not be resolved"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_symlink", classified_link)
    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    with patch.object(capability_index, "_registered_tools", return_value=tools):
        result = capability_index.sync(tmp_path)
        assert result["skills"] == 1 and result["tools"] == 1
        assert capability_index.search(tmp_path, "accessible", kind="skill")
    assert invalid.read_text(encoding="utf-8") == "unreviewed link target"


def test_directory_named_skill_md_does_not_block_index():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        skill, tools = fixture(root)
        invalid = root / "vendor/agent-skills/not-a-skill/SKILL.md"
        invalid.mkdir(parents=True)
        with patch.object(capability_index, "_registered_tools", return_value=tools):
            result = capability_index.sync(root)
            assert result["skills"] == 1 and result["tools"] == 1
            assert capability_index.health(root)["ok"]
            assert capability_index.search(root, "accessible", kind="skill")[0]["id"] == "skill:" + skill.parent.name
            assert capability_index.search(root, "image", kind="tool")[0]["name"] == "make_image"
            assert capability_index.sync(root)["updated"] == 0
        assert invalid.is_dir()


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


def test_repeated_recovery_in_one_second_preserves_both_damaged_indexes():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        skill, tools = fixture(root)
        original_skill = skill.read_bytes()
        path = capability_index.database_path(root)
        path.parent.mkdir(parents=True)
        damaged_copies = (b"first damaged database", b"second damaged database")
        with patch.object(capability_index, "_registered_tools", return_value=tools), \
                patch.object(capability_index.time, "time", return_value=1700000000):
            for damaged in damaged_copies:
                path.write_bytes(damaged)
                result = capability_index.sync(root)
                assert result["skills"] == 1 and result["tools"] == 1
                assert capability_index.health(root)["ok"]
                assert capability_index.search(root, "image", kind="tool")[0]["name"] == "make_image"
        backups = list(path.parent.glob("capabilities.sqlite3.corrupt-*"))
        assert len(backups) == 2
        assert {backup.read_bytes() for backup in backups} == set(damaged_copies)
        assert skill.read_bytes() == original_skill


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
