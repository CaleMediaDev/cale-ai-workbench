"""Local design helpers keep source findings and color math inspectable."""

import json

import pytest

from design_tools import add_astro_ai_ui, check_color_contrast, luminance


def test_black_and_white_have_maximum_contrast():
    result = check_color_contrast("#000000", "#FFFFFF")
    assert result["ratio"] == 21
    assert result["normal_text_AAA"] is True


def test_same_color_fails_text_contrast():
    result = check_color_contrast("#444444", "#444444")
    assert result["ratio"] == 1
    assert result["normal_text_AA"] is False


def test_rejects_non_hex_color():
    with pytest.raises(ValueError):
        luminance("red")


def test_installs_local_ai_components_into_astro_project(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"astro": "^5"}}), encoding="utf-8")
    first = add_astro_ai_ui(str(tmp_path))
    assert first["files_added"] > 15
    assert first["components"] >= 15
    assert (tmp_path / "src/vendor/ai-ui/components/ai-ui/Message.astro").is_file()
    assert (tmp_path / "src/vendor/ai-ui/LICENSE").is_file()
    assert add_astro_ai_ui(str(tmp_path))["files_added"] == 0


def test_rejects_non_astro_project(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "*"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Astro"):
        add_astro_ai_ui(str(tmp_path))


def test_conflict_does_not_copy_other_files(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"astro": "^5"}}), encoding="utf-8")
    existing = tmp_path / "src/vendor/ai-ui/components/ai-ui/Message.astro"
    existing.parent.mkdir(parents=True)
    existing.write_text("custom component", encoding="utf-8")
    with pytest.raises(FileExistsError):
        add_astro_ai_ui(str(tmp_path))
    assert existing.read_text(encoding="utf-8") == "custom component"
    assert not (tmp_path / "src/vendor/ai-ui/LICENSE").exists()
