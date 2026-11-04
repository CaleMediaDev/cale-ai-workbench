"""Update path validation must respect Windows destination aliases."""

import io
import json
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
