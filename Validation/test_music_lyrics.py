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


def test_save_skips_overlapping_and_backward_captions(tmp_path):
    source = tmp_path / "song.wav"
    source.write_bytes(b"original")
    vocal = tmp_path / "vocals.wav"
    vocal.write_bytes(b"isolated")
    target = tmp_path / "lyrics"
    first = {"start": 1.0, "end": 3.0, "text": "First line"}
    adjacent = {"start": 3.0, "end": 4.0, "text": "Next line"}

    result = music_lyrics.save(source, vocal, target, {"segments": [
        first,
        {"start": 2.0, "end": 5.0, "text": "Overlapping line"},
        {"start": 0.0, "end": 1.0, "text": "Backward line"},
        adjacent,
    ]})

    assert result["segments"] == 2
    assert (target / "lyrics.txt").read_text(encoding="utf-8") == (
        "First line\nNext line\n"
    )
    assert (target / "lyrics.srt").read_text(encoding="utf-8") == (
        "1\n00:00:01,000 --> 00:00:03,000\nFirst line\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nNext line\n"
    )
    assert json.loads((target / "lyrics.json").read_text(encoding="utf-8"))[
        "segments"
    ] == [first, adjacent]
    assert source.read_bytes() == b"original"
    assert vocal.read_bytes() == b"isolated"


@pytest.mark.parametrize("existing_target", [False, True])
def test_save_serialization_failure_leaves_no_partial_output(tmp_path, existing_target):
    source = tmp_path / "song.wav"
    source.write_bytes(b"original")
    vocal = tmp_path / "vocals.wav"
    vocal.write_bytes(b"isolated")
    target = tmp_path / "lyrics"
    if existing_target:
        target.mkdir()
        (target / "keep.txt").write_text("keep", encoding="utf-8")
    transcription = {
        "language": {"en"},
        "segments": [{"start": 1.0, "end": 2.0, "text": "Hello world"}],
    }

    with pytest.raises(TypeError, match="not JSON serializable"):
        music_lyrics.save(source, vocal, target, transcription)

    if existing_target:
        assert sorted(path.name for path in target.iterdir()) == ["keep.txt"]
        assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"
    else:
        assert not target.exists()
    assert source.read_bytes() == b"original"
    assert vocal.read_bytes() == b"isolated"


@pytest.mark.parametrize("existing_target", [False, True])
@pytest.mark.parametrize("field", ["text", "language"])
def test_save_encoding_failure_leaves_no_partial_output(tmp_path, existing_target, field):
    source = tmp_path / "song.wav"
    source.write_bytes(b"original")
    vocal = tmp_path / "vocals.wav"
    vocal.write_bytes(b"isolated")
    target = tmp_path / "lyrics"
    if existing_target:
        target.mkdir()
        (target / "keep.txt").write_text("keep", encoding="utf-8")
    transcription = {
        "language": "en",
        "segments": [{"start": 1.0, "end": 2.0, "text": "Hello world"}],
    }
    if field == "text":
        transcription["segments"][0]["text"] = "Hello \ud800"
    else:
        transcription["language"] = "en\ud800"

    with pytest.raises(UnicodeEncodeError):
        music_lyrics.save(source, vocal, target, transcription)

    if existing_target:
        assert sorted(path.name for path in target.iterdir()) == ["keep.txt"]
        assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"
    else:
        assert not target.exists()
    assert source.read_bytes() == b"original"
    assert vocal.read_bytes() == b"isolated"


def test_save_write_failure_rolls_back_partial_files_and_allows_retry(tmp_path):
    from contextlib import contextmanager
    from unittest.mock import Mock

    source = tmp_path / "song.wav"
    source.write_bytes(b"original")
    vocal = tmp_path / "vocals.wav"
    vocal.write_bytes(b"isolated")
    target = tmp_path / "lyrics"
    target.mkdir()
    keep = target / "keep.txt"
    keep.write_bytes(b"keep")
    transcription = {"segments": [
        {"start": 1.0, "end": 2.0, "text": "Hello world"},
    ]}
    real_open = Path.open

    @contextmanager
    def failing_open(path, *args, **kwargs):
        with real_open(path, *args, **kwargs) as output:
            if path == target / "lyrics.srt" and args == ("xb",):
                proxy = Mock(wraps=output)

                def partial_write(content):
                    output.write(content[:5])
                    raise OSError("simulated disk failure")

                proxy.write.side_effect = partial_write
                yield proxy
            else:
                yield output

    with patch.object(Path, "open", failing_open):
        with pytest.raises(OSError, match="simulated disk failure"):
            music_lyrics.save(source, vocal, target, transcription)

    assert sorted(path.name for path in target.iterdir()) == ["keep.txt"]
    assert keep.read_bytes() == b"keep"
    assert source.read_bytes() == b"original"
    assert vocal.read_bytes() == b"isolated"

    result = music_lyrics.save(source, vocal, target, transcription)
    assert result["segments"] == 1
    assert (target / "lyrics.txt").read_text(encoding="utf-8") == "Hello world\n"
    assert (target / "lyrics.srt").read_text(encoding="utf-8") == (
        "1\n00:00:01,000 --> 00:00:02,000\nHello world\n"
    )
    assert json.loads((target / "lyrics.json").read_text(encoding="utf-8"))[
        "segments"
    ] == transcription["segments"]


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


@pytest.mark.parametrize("invalid", [None, "not a timestamp", {}, 10 ** 400])
@pytest.mark.parametrize("field", ["start", "end"])
@pytest.mark.parametrize("include_valid", [False, True])
def test_save_skips_unconvertible_timestamps(tmp_path, invalid, field, include_valid):
    source = tmp_path / "song.wav"
    source.write_bytes(b"original")
    vocal = tmp_path / "vocals.wav"
    vocal.write_bytes(b"isolated")
    target = tmp_path / "lyrics"
    malformed = {"start": 0.0, "end": 1.0, "text": "Invalid timing"}
    malformed[field] = invalid
    valid = {"start": 2.0, "end": 3.0, "text": "Keep this"}
    segments = [malformed]

    if include_valid:
        segments.append(valid)
        result = music_lyrics.save(source, vocal, target, {"segments": segments})
        assert result["segments"] == 1
        assert (target / "lyrics.txt").read_text(encoding="utf-8") == "Keep this\n"
        assert (target / "lyrics.srt").read_text(encoding="utf-8") == (
            "1\n00:00:02,000 --> 00:00:03,000\nKeep this\n"
        )
        assert json.loads((target / "lyrics.json").read_text(encoding="utf-8"))[
            "segments"
        ] == [valid]
    else:
        with pytest.raises(RuntimeError, match="No clear lyrics"):
            music_lyrics.save(source, vocal, target, {"segments": segments})
        assert not target.exists()

    assert source.read_bytes() == b"original"
    assert vocal.read_bytes() == b"isolated"


@pytest.mark.parametrize("missing_field", ["start", "end"])
@pytest.mark.parametrize("include_valid", [False, True])
def test_save_skips_segments_with_missing_timestamps(tmp_path, missing_field, include_valid):
    source = tmp_path / "song.wav"
    source.write_bytes(b"original")
    vocal = tmp_path / "vocals.wav"
    vocal.write_bytes(b"isolated")
    target = tmp_path / "lyrics"
    malformed = {"start": 0.0, "end": 5.0, "text": "Missing timestamp"}
    del malformed[missing_field]
    valid = {"start": 2.0, "end": 3.0, "text": "Keep this"}
    segments = [malformed]

    if include_valid:
        segments.append(valid)
        result = music_lyrics.save(source, vocal, target, {"segments": segments})
        assert result["segments"] == 1
        assert (target / "lyrics.txt").read_text(encoding="utf-8") == "Keep this\n"
        assert (target / "lyrics.srt").read_text(encoding="utf-8") == (
            "1\n00:00:02,000 --> 00:00:03,000\nKeep this\n"
        )
        assert json.loads((target / "lyrics.json").read_text(encoding="utf-8"))[
            "segments"
        ] == [valid]
    else:
        with pytest.raises(RuntimeError, match="No clear lyrics"):
            music_lyrics.save(source, vocal, target, {"segments": segments})
        assert not target.exists()

    assert source.read_bytes() == b"original"
    assert vocal.read_bytes() == b"isolated"


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


@pytest.mark.parametrize("start,end", [
    (0.0, 0.0004),
    (1.0001, 1.0004),
    (1.0006, 1.0014),
])
@pytest.mark.parametrize("include_valid", [False, True])
def test_save_rejects_captions_that_collapse_at_millisecond_precision(
    tmp_path, start, end, include_valid
):
    source = tmp_path / "song.wav"
    source.write_bytes(b"original")
    vocal = tmp_path / "vocals.wav"
    vocal.write_bytes(b"isolated")
    target = tmp_path / "lyrics"
    valid = {"start": 2.0, "end": 3.0, "text": "Keep this"}
    segments = [{"start": start, "end": end, "text": "Collapsed caption"}]
    if include_valid:
        segments.append(valid)
        result = music_lyrics.save(source, vocal, target, {"segments": segments})
        assert result["segments"] == 1
        assert (target / "lyrics.txt").read_text(encoding="utf-8") == "Keep this\n"
        assert (target / "lyrics.srt").read_text(encoding="utf-8") == (
            "1\n00:00:02,000 --> 00:00:03,000\nKeep this\n"
        )
        assert json.loads((target / "lyrics.json").read_text(encoding="utf-8"))[
            "segments"
        ] == [valid]
    else:
        with pytest.raises(RuntimeError, match="No clear lyrics"):
            music_lyrics.save(source, vocal, target, {"segments": segments})
        assert not target.exists()
    assert source.read_bytes() == b"original"
    assert vocal.read_bytes() == b"isolated"
