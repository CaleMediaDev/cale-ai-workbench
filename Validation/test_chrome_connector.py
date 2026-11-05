"""A local, matching Chrome companion is required for pairing."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import chrome_connector


@pytest.mark.parametrize("version_line", [
    "version: 0.2.6\n",
    "version: 0.2.6 # local companion\n",
])
def test_pairing_uses_matching_local_companion(tmp_path, monkeypatch, version_line):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("name: hermes-connector\n" + version_line, encoding="utf-8")
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)
    monkeypatch.setattr(chrome_connector, "ROOT", tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="secret-code\n")

    monkeypatch.setattr(chrome_connector.subprocess, "run", run)
    assert chrome_connector.pairing_code() == "secret-code"
    assert Path(calls[0][0][1]) == plugin / "broker.py"
    assert calls[0][1]["capture_output"]


@pytest.mark.parametrize("version_line", [
    'version: "0.2.6"\n',
    "version: '0.2.6'\n",
    'version: "0.2.6" # local companion\n',
])
def test_installed_version_accepts_quoted_yaml(tmp_path, monkeypatch, version_line):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(version_line, encoding="utf-8")
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)

    assert chrome_connector.installed_version() == "0.2.6"


@pytest.mark.parametrize("separator", [" ", "\t", " \t "])
def test_installed_version_accepts_whitespace_before_colon(tmp_path, monkeypatch, separator):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        f"version{separator}: 0.2.6\n", encoding="utf-8"
    )
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)

    assert chrome_connector.installed_version() == "0.2.6"


@pytest.mark.parametrize("version_key", ["'version'", '"version"'])
def test_installed_version_accepts_quoted_yaml_keys(tmp_path, monkeypatch, version_key):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        f"name: hermes-connector\n{version_key}: 0.2.6\n", encoding="utf-8"
    )
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)

    assert chrome_connector.installed_version() == "0.2.6"


@pytest.mark.parametrize("indent", ["  ", "    "])
def test_installed_version_accepts_indented_root_mapping(tmp_path, monkeypatch, indent):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        f"{indent}name: hermes-connector\n{indent}version: 0.2.6\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)

    assert chrome_connector.installed_version() == "0.2.6"


@pytest.mark.parametrize("comment_first", [True, False])
def test_installed_version_accepts_indented_mapping_with_unindented_comment(
    tmp_path, monkeypatch, comment_first
):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    mapping = "  name: hermes-connector\n  version: 0.2.6\n"
    comment = "# Local companion manifest\n"
    contents = comment + mapping if comment_first else mapping + comment
    (plugin / "plugin.yaml").write_text(contents, encoding="utf-8")
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)

    assert chrome_connector.installed_version() == "0.2.6"


@pytest.mark.parametrize("start,end", [
    ("---\n", ""),
    ("---\n", "...\n"),
    ("--- # document start\n", "... # document end\n"),
])
def test_installed_version_accepts_indented_mapping_with_document_markers(
    tmp_path, monkeypatch, start, end
):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        start + "  name: hermes-connector\n  version: 0.2.6\n" + end,
        encoding="utf-8",
    )
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)

    assert chrome_connector.installed_version() == "0.2.6"


@pytest.mark.parametrize("directive", [
    "%YAML 1.1\n",
    "%YAML 1.2\n",
    "%YAML 1.2 # manifest format\n",
])
def test_installed_version_accepts_directive_before_indented_mapping(
    tmp_path, monkeypatch, directive
):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        directive + "---\n  name: hermes-connector\n  version: 0.2.6\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)

    assert chrome_connector.installed_version() == "0.2.6"


def test_installed_version_accepts_tag_directive_before_indented_mapping(
    tmp_path, monkeypatch
):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        "%TAG !local! tag:example.org,2026:\n"
        "---\n  name: hermes-connector\n  version: 0.2.6\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)

    assert chrome_connector.installed_version() == "0.2.6"


def test_installed_version_accepts_utf8_bom(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_bytes(b"\xef\xbb\xbfversion: 0.2.6\n")
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)

    assert chrome_connector.installed_version() == "0.2.6"


def test_corrupt_manifest_reports_repair_without_launching_broker(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_bytes(b"version: 0.2.6\n\xff")
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)

    def unexpected_run(*args, **kwargs):
        pytest.fail("The broker must not run with an unreadable manifest")

    monkeypatch.setattr(chrome_connector.subprocess, "run", unexpected_run)

    assert chrome_connector.installed_version() == ""
    with pytest.raises(RuntimeError, match="Run the installer again to repair it"):
        chrome_connector.pairing_code()


def test_manifest_probe_error_reports_repair_without_launching_broker(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    manifest = plugin / "plugin.yaml"
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)
    original_is_file = Path.is_file

    def inaccessible_is_file(path):
        if path == manifest:
            raise PermissionError("Manifest directory is inaccessible")
        return original_is_file(path)

    def unexpected_run(*args, **kwargs):
        pytest.fail("The broker must not run with an inaccessible manifest")

    monkeypatch.setattr(Path, "is_file", inaccessible_is_file)
    monkeypatch.setattr(chrome_connector.subprocess, "run", unexpected_run)

    assert chrome_connector.installed_version() == ""
    with pytest.raises(RuntimeError, match="Run the installer again to repair it"):
        chrome_connector.pairing_code()


def test_installed_version_does_not_strip_attached_hash(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("version: 0.2.6#custom\n", encoding="utf-8")
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)

    # Without separating whitespace, the hash belongs to the YAML scalar.
    assert chrome_connector.installed_version() == ""


@pytest.mark.parametrize("contents", [
    "version:0.2.6\n",
    'version:"0.2.6"\n',
    "version:'0.2.6'\n",
])
def test_installed_version_rejects_scalar_without_mapping_separator(
    tmp_path, monkeypatch, contents
):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(contents, encoding="utf-8")
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)

    # Without whitespace after the colon, this is a scalar, not a mapping.
    assert chrome_connector.installed_version() == ""


def test_wrong_companion_version_cannot_pair(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("version: 0.2.4\n", encoding="utf-8")
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)
    with pytest.raises(RuntimeError, match="0.2.6"):
        chrome_connector.pairing_code()
