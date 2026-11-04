"""Every accepted registered camera name must select its saved stream."""

import json
import socket

import pytest

import camera_network


@pytest.mark.parametrize("name", ["0", "01", "123"])
def test_registered_numeric_name_selects_network_stream(tmp_path, monkeypatch, name):
    registry = tmp_path / "Media/network-cameras.json"
    monkeypatch.setattr(camera_network, "SOURCES", registry)
    monkeypatch.setattr(camera_network, "GREETING_SOURCE", tmp_path / "Media/greeting-camera.json")
    monkeypatch.setattr(camera_network.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.168.1.20", 554))])
    stream = "rtsp://192.168.1.20/live"
    assert camera_network.add_camera(name, stream) == {"camera": name, "added": True}
    assert json.loads(registry.read_text(encoding="utf-8"))[name] == stream
    assert camera_network.camera_source(name) == stream
    camera_network.set_greeting_camera(name)
    assert camera_network.camera_source(camera_network.greeting_camera()) == stream
    assert camera_network.camera_source(int(name)) == int(name)
    assert camera_network.camera_source("9") == 9
    camera_network.set_greeting_camera("usb:" + name)
    assert camera_network.camera_source(camera_network.greeting_camera()) == int(name)


def test_registered_numeric_stream_is_revalidated_before_use(tmp_path, monkeypatch):
    registry = tmp_path / "network-cameras.json"
    registry.write_text(json.dumps({"01": "rtsp://camera.example/live"}), encoding="utf-8")
    monkeypatch.setattr(camera_network, "SOURCES", registry)
    monkeypatch.setattr(camera_network.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 554))])
    with pytest.raises(ValueError, match="private local address"):
        camera_network.camera_source("01")


def test_default_greeting_still_selects_usb_when_numeric_name_exists(tmp_path, monkeypatch):
    registry = tmp_path / "network-cameras.json"
    registry.write_text(json.dumps({"0": "rtsp://192.168.1.20/live"}), encoding="utf-8")
    monkeypatch.setattr(camera_network, "SOURCES", registry)
    monkeypatch.setattr(camera_network, "GREETING_SOURCE", tmp_path / "missing.json")
    assert camera_network.camera_source(camera_network.greeting_camera()) == 0


@pytest.mark.parametrize("tool", ["camera_snapshot", "watch_camera"])
def test_camera_tools_keep_usb_default_and_allow_numeric_name(tmp_path, monkeypatch, tool):
    import asyncio

    import toolbox_mcp

    stream = "rtsp://192.168.1.20/live"
    registry = tmp_path / "network-cameras.json"
    registry.write_text(json.dumps({"0": stream}), encoding="utf-8")
    monkeypatch.setattr(camera_network, "SOURCES", registry)
    monkeypatch.setattr(camera_network.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.168.1.20", 554))])

    async def resolve_only(action, timeout, **options):
        return {"source": camera_network.camera_source(options["device"])}

    monkeypatch.setattr(toolbox_mcp, "media_run", resolve_only)
    function = getattr(toolbox_mcp, tool)
    arguments = {"output_path": "unused.png"} if tool == "camera_snapshot" else {}
    assert asyncio.run(function(**arguments)) == {"source": 0}
    assert asyncio.run(function(**arguments, device="0")) == {"source": stream}
