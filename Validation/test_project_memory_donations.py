"""Compact project-memory checks and regression space for donated fixes.

Keep this file small enough to review alongside project_memory.py in ordinary
donation lanes. The complete memory suite remains in test_project_memory.py.
"""

import project_memory as memory


def test_saved_memory_remains_searchable_after_reopening(tmp_path):
    saved = memory.save(tmp_path, "decision", "Storage choice", "Use SQLite for offline lookup")
    memory.initialize(tmp_path)
    found = memory.search(tmp_path, "SQLite", kind="decision")
    assert [row["id"] for row in found] == [saved["id"]]
    assert memory.get(tmp_path, saved["id"])["body"] == "Use SQLite for offline lookup"


def test_recalled_file_does_not_keep_changed_or_deleted_content(tmp_path):
    source = tmp_path / "README.md"
    source.write_text("obsoletezebra", encoding="utf-8")
    memory.index_file(tmp_path, source)
    assert memory.search(tmp_path, "obsoletezebra")
    source.write_text("replacementgiraffe", encoding="utf-8")
    assert not memory.search(tmp_path, "obsoletezebra")
    assert memory.search(tmp_path, "replacementgiraffe")
    source.unlink()
    assert not memory.search(tmp_path, "replacementgiraffe")
