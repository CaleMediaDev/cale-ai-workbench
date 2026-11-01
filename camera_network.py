"""Discover ONVIF cameras and keep explicitly supplied local stream URLs on this PC."""

import ipaddress
import json
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "Media/network-cameras.json"
GREETING_SOURCE = ROOT / "Media/camera-greeting-source.json"


def discover_onvif(seconds=3):
    """Use standard WS-Discovery multicast; no password guessing or subnet sweep."""
    seconds = min(max(float(seconds), 1), 8)
    message_id = uuid.uuid4()
    probe = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        '<s:Header><a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>'
        f'<a:MessageID>uuid:{message_id}</a:MessageID>'
        '<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To></s:Header>'
        '<s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body></s:Envelope>'
    ).encode("utf-8")
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind(("", 0))
        sock.settimeout(0.25)
        sock.sendto(probe, ("239.255.255.250", 3702))
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                data, source = sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                continue
            matches = [node for node in root.iter() if node.tag.endswith("ProbeMatch")]
            if not matches:
                continue
            kinds = [node.text or "" for node in root.iter()
                     if node.tag.endswith("Types")]
            if not any("NetworkVideoTransmitter" in kind for kind in kinds):
                continue
            addresses = [node.text.strip() for node in root.iter()
                         if node.tag.endswith("XAddrs") and node.text]
            found[source[0]] = {"ip": source[0], "services": addresses[:3]}
    return {"cameras": list(found.values()),
            "note": "Discovery finds ONVIF devices only. A camera may require its own RTSP stream address and credentials."}


def _local_stream(url):
    parts = urlsplit(url)
    if parts.scheme not in ("rtsp", "rtsps", "http", "https") or not parts.hostname:
        raise ValueError("Use an RTSP or HTTP camera stream URL.")
    # Resolve hostnames before accepting them; no public network streams are
    # exposed through this local-camera tool.
    addresses = socket.getaddrinfo(parts.hostname, parts.port or 554, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_private for item in addresses):
        raise ValueError("The camera stream must resolve only to a private local address.")
    return url


def _saved():
    try:
        value = json.loads(SOURCES.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def add_camera(name, stream_url):
    """Register an owner-provided stream without returning its credential-bearing URL."""
    name = str(name).strip()[:60]
    if not name or not name.replace("-", "").replace("_", "").replace(" ", "").isalnum():
        raise ValueError("Give the camera a short name using letters and numbers.")
    _local_stream(stream_url)
    cameras = _saved()
    cameras[name] = stream_url
    SOURCES.parent.mkdir(exist_ok=True)
    SOURCES.write_text(json.dumps(cameras, indent=2), encoding="utf-8")
    return {"camera": name, "added": True}


def list_cameras():
    return {"cameras": [{"name": name, "source": "local network stream"}
                        for name in sorted(_saved())]}


def remove_camera(name):
    cameras = _saved()
    removed = cameras.pop(str(name), None) is not None
    SOURCES.write_text(json.dumps(cameras, indent=2), encoding="utf-8")
    return {"camera": name, "removed": removed}


def camera_source(device):
    if isinstance(device, int) or str(device).isdigit():
        return int(device)
    cameras = _saved()
    if str(device) not in cameras:
        raise ValueError("Camera not registered. Discover it and add its stream URL first.")
    return _local_stream(cameras[str(device)])


def greeting_camera():
    try:
        return str(json.loads(GREETING_SOURCE.read_text(encoding="utf-8"))["name"])
    except (OSError, ValueError, KeyError, TypeError):
        return "0"


def set_greeting_camera(name):
    """Choose a registered camera or USB index for the next greeting session."""
    camera_source(name)
    GREETING_SOURCE.parent.mkdir(exist_ok=True)
    GREETING_SOURCE.write_text(json.dumps({"name": str(name)}), encoding="utf-8")
    return {"camera": str(name), "selected_for_greetings": True,
            "note": "Turn Camera Greeting off and on to switch a running session."}
