"""Update path validation must respect Windows destination aliases."""

import io
import json
import os
import subprocess
from unittest.mock import patch
from zipfile import ZipFile

import pytest

import self_update


def archive_for(paths):
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("install-files.json", json.dumps(paths))
        for path in paths:
            archive.writestr(path, "synthetic content")
    stream.seek(0)
    return ZipFile(stream)


@pytest.mark.parametrize("folder", ["cloud", "CLOUD", "pRoJeCtS", "longtasks", "DATA", "media", "toolbox", "updates", "temp", "downloads", "Cloud.", "Projects "])
def test_protected_windows_folder_aliases_are_rejected(folder):
    with archive_for([f"{folder}/example.txt"]) as archive:
        with pytest.raises(ValueError, match="Unsafe"):
            self_update.validated_manifest(archive)


def test_case_only_duplicate_destinations_are_rejected():
    with archive_for(["README.md", "readme.md"]) as archive:
        with pytest.raises(ValueError, match="Duplicate"):
            self_update.validated_manifest(archive)


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_update_rejects_junction_destination_before_any_changes(tmp_path):
    root, outside = tmp_path / "app", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    marker = root / ".cale-ai-install"
    marker.write_text("Cale AI Workbench 0.5.0", encoding="utf-8")
    source = root / "long_tasks.py"
    source.write_bytes(b"old source")
    external = outside / "component.py"
    external.write_bytes(b"outside content")
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(root / "vendor"), str(outside)],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    release = tmp_path / "release.zip"
    with ZipFile(release, "w") as archive:
        archive.writestr("install-files.json", json.dumps(["long_tasks.py", "vendor/component.py"]))
        archive.writestr("long_tasks.py", "new source")
        archive.writestr("vendor/component.py", "new component")
    with patch.object(self_update, "ROOT", root), \
         patch.object(self_update, "active_tasks", return_value=False), \
         patch.object(self_update, "sync_new_media_requirements") as dependencies:
        with pytest.raises(ValueError, match="redirected"):
            self_update.apply(release, (0, 5, 1))
    dependencies.assert_not_called()
    assert source.read_bytes() == b"old source"
    assert external.read_bytes() == b"outside content"
    assert marker.read_text(encoding="utf-8") == "Cale AI Workbench 0.5.0"
    assert not (root / "Updates").exists()

@pytest.mark.parametrize("scratch_name", ["long_tasks.py.updating", ".cale-ai-install.updating"])
def test_update_preserves_existing_scratch_paths(tmp_path, scratch_name):
    root = tmp_path / "app"
    root.mkdir()
    source = root / "long_tasks.py"
    source.write_bytes(b"old source")
    marker = root / ".cale-ai-install"
    marker.write_text("Cale AI Workbench 0.5.0", encoding="utf-8")
    outside = tmp_path / "unrelated.txt"
    outside.write_bytes(b"unrelated content")
    scratch = root / scratch_name
    os.link(outside, scratch)
    release = tmp_path / "release.zip"
    with ZipFile(release, "w") as archive:
        archive.writestr("install-files.json", json.dumps(["long_tasks.py"]))
        archive.writestr("long_tasks.py", "new source")
    with patch.object(self_update, "ROOT", root), \
         patch.object(self_update, "active_tasks", return_value=False), \
         patch.object(self_update, "sync_new_media_requirements"), \
         patch.object(self_update, "sync_new_toolbox_requirements"):
        self_update.apply(release, (0, 5, 1))
    assert outside.read_bytes() == b"unrelated content"
    assert scratch.read_bytes() == b"unrelated content"
    assert source.read_bytes() == b"new source"
    assert marker.read_text(encoding="utf-8") == "Cale AI Workbench 0.5.1"


def test_private_update_file_is_removed_when_publication_fails(tmp_path):
    target = tmp_path / "source.py"
    target.write_bytes(b"original")
    with patch.object(self_update.os, "replace", side_effect=OSError("publication failed")):
        with pytest.raises(OSError, match="publication failed"):
            self_update._replace_update_file(target, b"replacement")
    assert target.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [target]
