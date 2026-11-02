"""The local skill and tool database stays searchable and rebuildable."""

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
