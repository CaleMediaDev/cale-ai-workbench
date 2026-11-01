"""Check first-use install and non-destructive audio-to-MIDI conversion."""

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

import music_midi


def test_transcription_rejects_missing_or_wrong_audio(tmp_path):
    with pytest.raises(FileNotFoundError):
        music_midi.transcribe_to_midi(str(tmp_path / "missing.wav"), str(tmp_path / "notes"))
    source = tmp_path / "text.txt"
    source.write_text("not audio")
    with pytest.raises(ValueError, match="audio file"):
        music_midi.transcribe_to_midi(str(source), str(tmp_path / "notes"))


def test_transcription_keeps_source_and_existing_outputs(tmp_path):
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"audio")
    with pytest.raises(ValueError, match="separate output"):
        music_midi.transcribe_to_midi(str(source), str(tmp_path))
    target = tmp_path / "notes"
    target.mkdir()
    (target / "existing.mid").write_bytes(b"keep")
    with pytest.raises(ValueError, match="empty output"):
        music_midi.transcribe_to_midi(str(source), str(target))
    assert (target / "existing.mid").read_bytes() == b"keep"


def test_transcription_reports_real_midi_and_notes(tmp_path):
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"audio")
    target = tmp_path / "notes"

    def render(args, **_kwargs):
        assert args[-2:] == [str(source), str(target)]
        (target / "guitar_basic_pitch.mid").write_bytes(b"MThd" + b"\x00" * 32)
        (target / "guitar_note_events.csv").write_text("start,end,pitch\n")
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(music_midi.music_backend, "_setup_lock", return_value=nullcontext()), patch.object(
        music_midi, "install", return_value=Path("python.exe")
    ), patch.object(music_midi.subprocess, "run", side_effect=render):
        result = music_midi.transcribe_to_midi(str(source), str(target))
    assert result["midi_path"].endswith("guitar_basic_pitch.mid")
    assert len(result["files"]) == 2
    assert source.read_bytes() == b"audio"
