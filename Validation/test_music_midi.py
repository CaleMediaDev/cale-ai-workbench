"""Check first-use install and non-destructive audio-to-MIDI conversion."""

import csv
import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

import music_midi


def test_staged_midi_publication_preserves_concurrent_output(tmp_path):
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"original audio")
    target = tmp_path / "notes"
    destination = target / "guitar_basic_pitch.mid"

    def render(args, **kwargs):
        staging = Path(args[-1])
        assert staging.parent == target
        (staging / destination.name).write_bytes(b"MThd generated")
        destination.write_bytes(b"another operation's MIDI")
        return type("Done", (), {"returncode": 0})()

    with patch.object(music_midi.music_backend, "_setup_lock", return_value=nullcontext()), \
         patch.object(music_midi, "install", return_value=Path("python.exe")), \
         patch.object(music_midi.subprocess, "run", side_effect=render):
        with pytest.raises((ValueError, FileExistsError)):
            music_midi.transcribe_to_midi(str(source), str(target))
    assert destination.read_bytes() == b"another operation's MIDI"
    assert list(target.iterdir()) == [destination]
    assert source.read_bytes() == b"original audio"


def test_failed_publication_preserves_replacement_symlink_to_owned_inode(tmp_path):
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(tmp_path)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable")
    probe.unlink()

    source = tmp_path / "guitar.wav"
    source.write_bytes(b"original audio")
    target = tmp_path / "notes"
    destination = target / "guitar.mid"
    original_link = music_midi.os.link
    staged_midi = []
    replacement_targets = []

    def render(args, **kwargs):
        staging = Path(args[-1])
        staged_midi.append(staging / "guitar.mid")
        staged_midi[0].write_bytes(b"MThd generated")
        (staging / "z-extra.txt").write_bytes(b"metadata")
        return type("Done", (), {"returncode": 0})()

    def replace_before_failure(staged, output):
        if output.name == "z-extra.txt":
            destination.unlink()
            destination.symlink_to(staged_midi[0])
            replacement_targets.append(destination.readlink())
            raise OSError("publication failed")
        return original_link(staged, output)

    with patch.object(music_midi.music_backend, "_setup_lock", return_value=nullcontext()), \
         patch.object(music_midi, "install", return_value=Path("python.exe")), \
         patch.object(music_midi.subprocess, "run", side_effect=render), \
         patch.object(music_midi.os, "link", side_effect=replace_before_failure):
        with pytest.raises(OSError, match="publication failed"):
            music_midi.transcribe_to_midi(str(source), str(target))

    assert destination.is_symlink()
    assert destination.readlink() == replacement_targets[0]
    assert not staged_midi[0].exists()
    assert list(target.iterdir()) == [destination]
    assert source.read_bytes() == b"original audio"


def test_failed_staged_publication_rolls_back_only_owned_midi(tmp_path):
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"original audio")
    target = tmp_path / "notes"
    original_link = music_midi.os.link
    attempts = []

    def render(args, **kwargs):
        staging = Path(args[-1])
        (staging / "guitar.mid").write_bytes(b"MThd generated")
        (staging / "z-extra.txt").write_bytes(b"generated metadata")
        return type("Done", (), {"returncode": 0})()

    def concurrent_second_publication(staged, destination):
        attempts.append(destination)
        if len(attempts) == 2:
            destination.write_bytes(b"another operation's metadata")
        return original_link(staged, destination)

    with patch.object(music_midi.music_backend, "_setup_lock", return_value=nullcontext()), \
         patch.object(music_midi, "install", return_value=Path("python.exe")), \
         patch.object(music_midi.subprocess, "run", side_effect=render), \
         patch.object(music_midi.os, "link", side_effect=concurrent_second_publication):
        with pytest.raises(FileExistsError):
            music_midi.transcribe_to_midi(str(source), str(target))
    assert not (target / "guitar.mid").exists()
    assert (target / "z-extra.txt").read_bytes() == b"another operation's metadata"
    assert len(list(target.iterdir())) == 1


@pytest.mark.parametrize("copy_fails", [False, True])
@pytest.mark.parametrize("winerror", [None, 1, 50])
def test_publication_without_hard_links_supports_copy_and_rollback(tmp_path, copy_fails, winerror):
    import errno

    link_error = OSError(errno.EOPNOTSUPP, "no hard links")
    if winerror is not None:
        link_error = OSError(errno.EINVAL, "Windows filesystem does not support hard links")
        link_error.winerror = winerror

    source = tmp_path / "guitar.wav"
    source.write_bytes(b"original audio")
    target = tmp_path / "notes"
    payload = b"MThd generated"
    original_copy = music_midi.shutil.copyfileobj

    def render(args, **kwargs):
        staging = Path(args[-1])
        (staging / "guitar.mid").write_bytes(payload)
        return type("Done", (), {"returncode": 0})()

    def copy(source_file, output):
        if copy_fails:
            output.write(b"partial")
            raise OSError("copy failed")
        return original_copy(source_file, output)

    with patch.object(music_midi.music_backend, "_setup_lock", return_value=nullcontext()), \
         patch.object(music_midi, "install", return_value=Path("python.exe")), \
         patch.object(music_midi.subprocess, "run", side_effect=render), \
         patch.object(music_midi.os, "link", side_effect=link_error), \
         patch.object(music_midi.shutil, "copyfileobj", side_effect=copy):
        if copy_fails:
            with pytest.raises(OSError, match="copy failed"):
                music_midi.transcribe_to_midi(str(source), str(target))
            assert list(target.iterdir()) == []
        else:
            result = music_midi.transcribe_to_midi(str(source), str(target))
            destination = target / "guitar.mid"
            assert destination.read_bytes() == payload
            assert result["midi_path"] == str(destination)
            assert result["files"] == [str(destination)]
            assert list(target.iterdir()) == [destination]

    assert source.read_bytes() == b"original audio"


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


def test_transcription_rejects_existing_file_as_output_directory(tmp_path):
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"audio")
    target = tmp_path / "notes"
    target.write_bytes(b"keep existing file")

    with patch.object(music_midi, "install") as install:
        with pytest.raises(ValueError, match="output folder, not an existing file"):
            music_midi.transcribe_to_midi(str(source), str(target))

    install.assert_not_called()
    assert source.read_bytes() == b"audio"
    assert target.read_bytes() == b"keep existing file"


def test_transcription_reports_real_midi_and_notes(tmp_path):
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"audio")
    target = tmp_path / "notes"

    def render(args, **_kwargs):
        assert args[-2] == str(source)
        staging = Path(args[-1])
        assert staging.parent == target
        (staging / "guitar_basic_pitch.mid").write_bytes(b"MThd" + b"\x00" * 32)
        (staging / "guitar_note_events.csv").write_text(
            "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\n\n"
            "0.0,1.0,69,85,1,2,3\n"
            "0.2,0.8,72,80\n\n"
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
        rows = list(csv.DictReader(stream))
    assert [list(row) for row in rows] == [music_midi.NOTE_COLUMNS] * 2
    assert [json.loads(row["pitch_bend"]) for row in rows] == [[1.0, 2.0, 3.0], []]


@pytest.mark.parametrize("has_midi_file", [False, True])
def test_transcription_does_not_treat_directory_as_midi(tmp_path, has_midi_file):
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"audio")
    target = tmp_path / "notes"

    def render(args, **_kwargs):
        staging = Path(args[-1])
        directory = staging / "a.mid"
        directory.mkdir()
        (directory / "payload").write_bytes(b"not a MIDI file")
        if has_midi_file:
            (staging / "z.mid").write_bytes(b"MThd")
        return type("Done", (), {"returncode": 0})()

    with patch.object(music_midi.music_backend, "_setup_lock", return_value=nullcontext()), \
         patch.object(music_midi, "install", return_value=Path("python.exe")), \
         patch.object(music_midi.subprocess, "run", side_effect=render):
        if has_midi_file:
            result = music_midi.transcribe_to_midi(str(source), str(target))
            assert Path(result["midi_path"]) == target / "z.mid"
            assert result["files"] == [str(target / "z.mid")]
            assert (target / "z.mid").read_bytes() == b"MThd"
        else:
            with pytest.raises(RuntimeError, match="without a MIDI file"):
                music_midi.transcribe_to_midi(str(source), str(target))
            assert list(target.iterdir()) == []

    assert not (target / "a.mid").exists()
    assert source.read_bytes() == b"audio"


@pytest.mark.parametrize("has_midi_file", [False, True])
def test_transcription_does_not_publish_staged_symlinks(tmp_path, has_midi_file):
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"original audio")
    target = tmp_path / "notes"

    def render(args, **_kwargs):
        staging = Path(args[-1])
        data = staging / "data"
        data.mkdir()
        payload = data / "payload"
        payload.write_bytes(b"MThd generated")
        try:
            (staging / "a.mid").symlink_to(payload)
            (staging / "extra.csv").symlink_to(payload)
            (staging / "extra.txt").symlink_to(payload)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation is unavailable")
        if has_midi_file:
            (staging / "z.mid").write_bytes(b"MThd genuine")
        return type("Done", (), {"returncode": 0})()

    with patch.object(music_midi.music_backend, "_setup_lock", return_value=nullcontext()), \
         patch.object(music_midi, "install", return_value=Path("python.exe")), \
         patch.object(music_midi.subprocess, "run", side_effect=render):
        if has_midi_file:
            result = music_midi.transcribe_to_midi(str(source), str(target))
            destination = target / "z.mid"
            assert result["midi_path"] == str(destination)
            assert result["files"] == [str(destination)]
            assert destination.read_bytes() == b"MThd genuine"
            assert list(target.iterdir()) == [destination]
        else:
            with pytest.raises(RuntimeError, match="without a MIDI file"):
                music_midi.transcribe_to_midi(str(source), str(target))
            assert list(target.iterdir()) == []

    assert source.read_bytes() == b"original audio"


def test_transcription_ignores_directory_matching_note_csv(tmp_path):
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"audio")
    target = tmp_path / "notes"

    def render(args, **_kwargs):
        staging = Path(args[-1])
        directory = staging / "extra.csv"
        directory.mkdir()
        (directory / "payload").write_bytes(b"not a note-event CSV")
        (staging / "guitar.mid").write_bytes(b"MThd")
        (staging / "guitar.csv").write_text(
            ",".join(music_midi.NOTE_COLUMNS) + "\n0.0,1.0,69,85,1,2\n"
        )
        return type("Done", (), {"returncode": 0})()

    with patch.object(music_midi.music_backend, "_setup_lock", return_value=nullcontext()), \
         patch.object(music_midi, "install", return_value=Path("python.exe")), \
         patch.object(music_midi.subprocess, "run", side_effect=render):
        result = music_midi.transcribe_to_midi(str(source), str(target))

    assert result["midi_path"] == str(target / "guitar.mid")
    assert set(result["files"]) == {
        str(target / "guitar.mid"), str(target / "guitar.csv")
    }
    assert set(target.iterdir()) == {target / "guitar.mid", target / "guitar.csv"}
    assert (target / "guitar.mid").read_bytes() == b"MThd"
    with (target / "guitar.csv").open(newline="") as stream:
        row = next(csv.DictReader(stream))
    assert json.loads(row["pitch_bend"]) == [1.0, 2.0]
    assert source.read_bytes() == b"audio"


def test_inference_output_decoding_matches_child_utf8_mode(tmp_path):
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"audio")
    target = tmp_path / "notes"
    diagnostic = "Cannot transcribe: 音楽.wav"

    def render(args, **kwargs):
        assert kwargs["env"]["PYTHONUTF8"] == "1"
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("errors") == "replace"
        return type("Done", (), {
            "returncode": 1, "stdout": "", "stderr": diagnostic,
        })()

    with patch.object(music_midi.music_backend, "_setup_lock", return_value=nullcontext()), \
         patch.object(music_midi, "install", return_value=Path("python.exe")), \
         patch.object(music_midi.subprocess, "run", side_effect=render):
        with pytest.raises(RuntimeError) as error:
            music_midi.transcribe_to_midi(str(source), str(target))

    assert str(error.value) == diagnostic
    assert list(target.iterdir()) == []
    assert source.read_bytes() == b"audio"


def test_failed_transcription_with_partial_output_can_be_retried(tmp_path):
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"audio")
    target = tmp_path / "notes"
    attempts = []

    def render(args, **_kwargs):
        staging = Path(args[-1])
        attempts.append(staging)
        (staging / "guitar_basic_pitch.mid").write_bytes(b"MThd")
        failed = len(attempts) == 1
        return type("Done", (), {
            "returncode": 1 if failed else 0,
            "stdout": "", "stderr": "inference failed" if failed else "",
        })()

    with patch.object(music_midi.music_backend, "_setup_lock", return_value=nullcontext()), patch.object(
        music_midi, "install", return_value=Path("python.exe")
    ), patch.object(music_midi.subprocess, "run", side_effect=render):
        with pytest.raises(RuntimeError, match="inference failed"):
            music_midi.transcribe_to_midi(str(source), str(target))
        assert list(target.iterdir()) == []
        result = music_midi.transcribe_to_midi(str(source), str(target))

    assert len(attempts) == 2
    assert all(not path.exists() for path in attempts)
    assert Path(result["midi_path"]) == target / "guitar_basic_pitch.mid"
    assert Path(result["midi_path"]).read_bytes() == b"MThd"
    assert result["files"] == [result["midi_path"]]
    assert source.read_bytes() == b"audio"


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


@pytest.mark.parametrize("suffix", ["", ",", ", ,\t,"])
def test_note_csv_without_pitch_bends_can_be_normalized_repeatedly(tmp_path, suffix):
    path = tmp_path / "notes.csv"
    path.write_text(
        ",".join(music_midi.NOTE_COLUMNS) + "\n"
        + "0.0,1.0,69,85" + suffix + "\n"
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


@pytest.mark.parametrize("valid", [True, False])
def test_note_csv_normalization_preserves_existing_temporary_file(tmp_path, valid):
    path = tmp_path / "notes.csv"
    original = (
        ",".join(music_midi.NOTE_COLUMNS) + "\n0.0,1.0,69,85,1,2\n"
        if valid else "unknown,format\n1,2\n"
    )
    path.write_text(original)
    existing = path.with_suffix(".csv.tmp")
    existing.write_bytes(b"unrelated file")

    if valid:
        music_midi.normalize_note_events(path)
        with path.open(newline="") as stream:
            row = next(csv.DictReader(stream))
        assert json.loads(row["pitch_bend"]) == [1.0, 2.0]
    else:
        with pytest.raises(RuntimeError, match="unexpected note-event CSV"):
            music_midi.normalize_note_events(path)
        assert path.read_text() == original

    assert existing.read_bytes() == b"unrelated file"
    assert set(tmp_path.iterdir()) == {path, existing}


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



@pytest.mark.parametrize("bends", ["NaN", "Infinity", "-Infinity", "1e999", "[NaN]", "[1e999]"])
def test_non_finite_pitch_bends_preserve_original_csv(tmp_path, bends):
    path = tmp_path / "notes.csv"
    original = (
        ",".join(music_midi.NOTE_COLUMNS) + "\n"
        + "0.0,1.0,69,85," + bends + "\n"
    ).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(ValueError):
        music_midi.normalize_note_events(path)

    assert path.read_bytes() == original
    assert set(tmp_path.iterdir()) == {path}


def test_unexpected_note_csv_is_preserved_for_diagnosis(tmp_path):
    path = tmp_path / "notes.csv"
    path.write_text("unknown,format\n1,2\n")
    with pytest.raises(RuntimeError, match="unexpected note-event CSV"):
        music_midi.normalize_note_events(path)
    assert path.read_text() == "unknown,format\n1,2\n"
    assert not path.with_suffix(".csv.tmp").exists()


def test_note_csv_missing_velocity_is_preserved(tmp_path):
    path = tmp_path / "notes.csv"
    path.write_text(",".join(music_midi.NOTE_COLUMNS) + "\n0.0,1.0,69\n")
    original = path.read_bytes()

    with pytest.raises(RuntimeError, match="incomplete note event"):
        music_midi.normalize_note_events(path)

    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
