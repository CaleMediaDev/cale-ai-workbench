"""Windows junctions must not redirect managed skill updates into other folders."""

import hashlib
import json
import os
import subprocess

import pytest

import skill_catalog


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
@pytest.mark.parametrize("relative", ["", "software-development", "software-development/context-engineering"])
def test_skill_update_preserves_junction_destination(tmp_path, monkeypatch, relative):
    root = tmp_path / "app"
    destination = root / "data/skills"
    outside = tmp_path / "outside"
    outside.mkdir()
    selected = {"context-engineering": "software-development", "frontend-ui-engineering": "creative"}
    monkeypatch.setattr(skill_catalog, "SKILLS", selected)
    monkeypatch.setattr(skill_catalog, "SHARED_REFERENCES", ())
    for name in selected:
        source = root / "vendor/agent-skills" / name / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"new bundled content")
    key = "software-development/context-engineering/SKILL.md"
    link = destination / relative
    external = outside / (destination / key).relative_to(link)
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_bytes(b"previous content")
    link.parent.mkdir(parents=True, exist_ok=True)
    marker = root / "data/.cale-skill-files.json"
    marker.write_text(json.dumps({key: hashlib.sha256(b"previous content").hexdigest()}), encoding="utf-8")
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    result = skill_catalog.sync(root)
    assert external.read_bytes() == b"previous content"
    assert link.is_junction()
    if relative:
        assert result == {"installed": 1, "updated": 0, "preserved": 1}
        assert (destination / "creative/frontend-ui-engineering/SKILL.md").read_bytes() == b"new bundled content"
    else:
        assert result == {"installed": 0, "updated": 0, "preserved": 2}
        assert not (outside / "creative").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_skill_sync_preserves_dangling_root_junction(tmp_path, monkeypatch):
    root = tmp_path / "app"
    source = root / "vendor/agent-skills/context-engineering/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"bundled content")
    monkeypatch.setattr(skill_catalog, "SKILLS", {"context-engineering": "software-development"})
    monkeypatch.setattr(skill_catalog, "SHARED_REFERENCES", ())
    link = root / "data/skills"
    link.parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    outside.rmdir()
    assert skill_catalog.sync(root) == {"installed": 0, "updated": 0, "preserved": 1}
    assert link.is_junction()
    assert not outside.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_skill_sync_preserves_redirected_data_folder_and_ownership_record(tmp_path, monkeypatch):
    root = tmp_path / "app"
    source = root / "vendor/agent-skills/context-engineering/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"new bundled content")
    monkeypatch.setattr(skill_catalog, "SKILLS", {"context-engineering": "software-development"})
    monkeypatch.setattr(skill_catalog, "SHARED_REFERENCES", ())
    outside = tmp_path / "outside"
    key = "software-development/context-engineering/SKILL.md"
    external = outside / "skills" / key
    external.parent.mkdir(parents=True)
    external.write_bytes(b"previous content")
    marker = outside / ".cale-skill-files.json"
    original_marker = json.dumps({key: hashlib.sha256(b"previous content").hexdigest()}).encode("utf-8")
    marker.write_bytes(original_marker)
    link = root / "data"
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    result = skill_catalog.sync(root)
    assert external.read_bytes() == b"previous content"
    assert marker.read_bytes() == original_marker
    assert link.is_junction()
    assert result == {"installed": 0, "updated": 0, "preserved": 1}
