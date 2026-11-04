"""Check first-use install and non-destructive audio-to-MIDI conversion."""

import csv
import json
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
        (target / "guitar_note_events.csv").write_text(
            "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\n\n"
            "0.0,1.0,69,85,1,2,3\n\n"
        )
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(music_midi.music_backend, "_setup_lock", return_value=nullcontext()), patch.object(
        music_midi, "install", return_value=Path("python.exe")
    ), patch.object(music_midi.subprocess, "run", side_effect=render):
        result = music_midi.transcribe_to_midi(str(source), str(target))
    assert result["midi_path"].endswith("guitar_basic_pitch.mid")
    assert len(result["files"]) == 2
    assert source.read_bytes() == b"audio"
    with (target / "guitar_note_events.csv").open(newline="") as stream:
        row = next(csv.DictReader(stream))
    assert list(row) == music_midi.NOTE_COLUMNS
    assert json.loads(row["pitch_bend"]) == [1.0, 2.0, 3.0]


@pytest.mark.parametrize("bends", ["1,2,3", "0"])
def test_note_csv_normalization_can_be_repeated(tmp_path, bends):
    path = tmp_path / "notes.csv"
    path.write_text(
        ",".join(music_midi.NOTE_COLUMNS) + "\n"
        + "0.0,1.0,69,85," + bends + "\n"
    )
    music_midi.normalize_note_events(path)
    normalized = path.read_bytes()

    music_midi.normalize_note_events(path)

    assert path.read_bytes() == normalized
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert json.loads(rows[0]["pitch_bend"]) == [
        float(value) for value in bends.split(",")
    ]
    assert not path.with_suffix(".csv.tmp").exists()


def test_note_csv_without_pitch_bends_can_be_normalized_repeatedly(tmp_path):
    path = tmp_path / "notes.csv"
    path.write_text(
        ",".join(music_midi.NOTE_COLUMNS) + "\n"
        + "0.0,1.0,69,85,\n"
    )

    music_midi.normalize_note_events(path)
    normalized = path.read_bytes()
    music_midi.normalize_note_events(path)

    assert path.read_bytes() == normalized
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert list(rows[0]) == music_midi.NOTE_COLUMNS
    assert rows[0]["pitch_bend"] == "[]"
    assert not path.with_suffix(".csv.tmp").exists()


@pytest.mark.parametrize("bends", ["[1,2,3]", "[1, 2, -3]"])
def test_unquoted_pitch_bend_arrays_are_normalized(tmp_path, bends):
    path = tmp_path / "notes.csv"
    path.write_text(
        ",".join(music_midi.NOTE_COLUMNS) + "\n"
        + "0.0,1.0,69,85," + bends + "\n"
    )

    music_midi.normalize_note_events(path)
    normalized = path.read_bytes()
    music_midi.normalize_note_events(path)

    assert path.read_bytes() == normalized
    with path.open(newline="") as stream:
        rows = list(csv.reader(stream))
    assert rows[0] == music_midi.NOTE_COLUMNS
    assert len(rows) == 2
    assert len(rows[1]) == 5
    assert rows[1][:4] == ["0.0", "1.0", "69", "85"]
    assert json.loads(rows[1][4]) == [float(value) for value in json.loads(bends)]
    assert not path.with_suffix(".csv.tmp").exists()


def test_unexpected_note_csv_is_preserved_for_diagnosis(tmp_path):
    path = tmp_path / "notes.csv"
    path.write_text("unknown,format\n1,2\n")
    with pytest.raises(RuntimeError, match="unexpected note-event CSV"):
        music_midi.normalize_note_events(path)
    assert path.read_text() == "unknown,format\n1,2\n"
    assert not path.with_suffix(".csv.tmp").exists()
