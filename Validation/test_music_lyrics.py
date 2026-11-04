"""Check the complete lyric workflow without downloading or running music models."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import music_lyrics
import toolbox_mcp


def test_lyric_tool_uses_vocal_stem_and_writes_editable_captions(tmp_path):
    source = tmp_path / "song.mp3"
    source.write_bytes(b"original")
    target = tmp_path / "lyrics"
    seen = []

    async def separate(source_path, output_dir):
        seen.append((source_path, output_dir))
        folder = Path(output_dir)
        folder.mkdir(parents=True)
        instrumental = folder / "song_(Instrumental).wav"
        vocal = folder / "song_(Vocals).wav"
        instrumental.write_bytes(b"instruments")
        vocal.write_bytes(b"vocals")
        return {"files": [str(instrumental), str(vocal)]}

    async def recognize(action, timeout, **arguments):
        assert (action, timeout) == ("transcribe_audio", 1800)
        assert arguments["path"].endswith("song_(Vocals).wav")
        return {"language": "en", "segments": [
            {"start": 1.25, "end": 3.5, "text": "  Hello   world "},
            {"start": 3.5, "end": 5.0, "text": "Sing along"},
        ]}

    with patch.object(toolbox_mcp, "separate_music", separate), patch.object(
        toolbox_mcp, "media_run", recognize
    ):
        result = asyncio.run(toolbox_mcp.toolbox_call(
            "local.transcribe_song_lyrics",
            json.dumps({"source_path": str(source), "output_dir": str(target)}),
        ))
    assert seen == [(str(source), str(target / "stems"))]
    assert source.read_bytes() == b"original"
    assert Path(result["lyrics_path"]).read_text(encoding="utf-8") == "Hello world\nSing along\n"
    assert "00:00:01,250 --> 00:00:03,500" in Path(result["captions_path"]).read_text(encoding="utf-8")
    assert result["segments"] == 2
    assert toolbox_mcp.toolbox_schema("local.transcribe_song_lyrics")["inputSchema"]["required"] == [
        "source_path", "output_dir"
    ]


def test_save_rejects_infinite_end_without_leaving_partial_output(tmp_path):
    source = tmp_path / "song.wav"
    source.write_bytes(b"original")
    target = tmp_path / "lyrics"
    transcription = {"segments": [
        {"start": 1.0, "end": float("inf"), "text": "Hello world"},
    ]}

    with pytest.raises(RuntimeError, match="No clear lyrics"):
        music_lyrics.save(source, source, target, transcription)

    assert not target.exists()
    assert source.read_bytes() == b"original"


@pytest.mark.parametrize("start,end", [(1.0, 1e308), (1e308, 1.1e308)])
def test_save_rejects_millisecond_overflow_without_partial_output(tmp_path, start, end):
    source = tmp_path / "song.wav"
    source.write_bytes(b"original")
    target = tmp_path / "lyrics"
    transcription = {"segments": [
        {"start": start, "end": end, "text": "Hello world"},
    ]}

    with pytest.raises(RuntimeError, match="No clear lyrics"):
        music_lyrics.save(source, source, target, transcription)

    assert not target.exists()
    assert source.read_bytes() == b"original"


def test_lyric_tool_protects_existing_files_and_reports_unclear_vocals(tmp_path):
    source = tmp_path / "song.wav"
    source.write_bytes(b"original")
    target = tmp_path / "lyrics"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    with pytest.raises(ValueError, match="empty output folder"):
        music_lyrics.prepare(str(source), str(target))
    assert (target / "keep.txt").read_text() == "keep"
    with pytest.raises(RuntimeError, match="vocal track"):
        music_lyrics.vocal_file([str(source)])
    with pytest.raises(RuntimeError, match="No clear lyrics"):
        music_lyrics.save(source, source, tmp_path / "new", {"segments": []})
    assert not (tmp_path / "new").exists()
