"""A failed extraction must not erase a newer selected-file memory record."""

import pytest

import user_file_memory as memory


@pytest.mark.parametrize("outcome", ["error", "empty"])
@pytest.mark.parametrize("previously_indexed", [False, True])
def test_failed_extraction_preserves_competing_success(tmp_path, monkeypatch, previously_indexed, outcome):
    state = tmp_path / "state"
    source = tmp_path / "notes.txt"
    source.write_text("old notes", encoding="utf-8")
    if previously_indexed:
        assert memory.ingest(state, [str(source)])["indexed"] == 1
    source.write_text("new zebra evidence", encoding="utf-8")
    original_text = memory._text
    published = False

    def concurrent_extract(path):
        nonlocal published
        if not published:
            published = True
            # Another scanner finishes while this extraction is still pending.
            assert memory.ingest(state, [str(source)])["indexed"] == 1
            if outcome == "empty":
                return ""
            raise RuntimeError("older extraction failed")
        return original_text(path)

    monkeypatch.setattr(memory, "_text", concurrent_extract)
    result = memory.ingest(state, [str(source)])
    assert result == {"indexed": 0, "unchanged": 0, "skipped": 1}
    with memory._connection(state) as conn:
        row = conn.execute("SELECT content,version FROM files WHERE path=?", (str(source),)).fetchone()
    assert row["content"] == "new zebra evidence"
    assert row["version"] == memory._file_version(source)
    assert memory.search(state, "zebra", scan_on_miss=False)[0]["path"] == str(source)
