"""Per-install eBay authorization; application secrets stay on Cale's server."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets
import time
import urllib.request
import webbrowser
from pathlib import Path

from print_farm_ebay import EbayClient
from print_farm_transport import persist

BROKER = "https://calemedia.com/wp-json/cale-print-connect/v1/"


def call(path, body):
    request = urllib.request.Request(
        BROKER + path,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "CaleAI-PrintFarm/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read(1_000_000))
    except Exception as exc:
        raise RuntimeError(
            "eBay connection service unavailable (" + type(exc).__name__ + ")"
        ) from None


def protect(data, decrypt=False):
    if os.name != "nt":
        raise RuntimeError(
            "This account connection currently requires Windows secure storage"
        )
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = Blob()
    method = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    # Scope is the current Windows user, never machine-wide.
    if not method(
        ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)
    ):
        raise RuntimeError("Windows could not secure the eBay connection")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree(output.data)


def connection(root):
    path = Path(root) / "account.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return json.loads(protect(base64.b64decode(data["protected"]), decrypt=True))


def connect(root, on_status=lambda value: None, cancelled=lambda: False):
    root = Path(root)
    verifier = secrets.token_urlsafe(48)
    reply = call("begin", {"challenge": hashlib.sha256(verifier.encode()).hexdigest()})
    url = reply.get("url", "")
    if not url.startswith("https://auth.ebay.com/oauth2/authorize?"):
        raise RuntimeError(
            reply.get("message", "eBay browser authorization is not configured yet")
        )
    webbrowser.open(url)
    on_status("Finish signing in and granting order access on eBay…")
    deadline = time.time() + 600
    while time.time() < deadline and not cancelled():
        answer = call("poll", {"id": reply["id"], "verifier": verifier})
        if answer.get("status") == "connected":
            record = {
                "id": reply["id"],
                "verifier": verifier,
                "connected_at": time.time(),
            }
            persist(
                root / "account.json",
                {
                    "protected": base64.b64encode(
                        protect(json.dumps(record).encode())
                    ).decode(),
                    "id": reply["id"],
                },
            )
            return record
        if answer.get("status") in ("denied", "expired", "failed"):
            raise RuntimeError(
                "eBay sign-in was not completed. Click Connect eBay account to try again."
            )
        time.sleep(3)
    raise RuntimeError("eBay sign-in cancelled or expired")


def disconnect(root):
    record = connection(root)
    if record:
        call("disconnect", record)
        (Path(root) / "account.json").unlink(missing_ok=True)


class ConnectedClient(EbayClient):
    def __init__(self, record):
        super().__init__("broker", "broker", "broker")
        self.record = record

    def access_token(self):
        if self.token and time.time() < self.expires:
            return self.token
        answer = call("token", self.record)
        if not answer.get("access_token"):
            raise RuntimeError("Reconnect your eBay account")
        self.token = answer["access_token"]
        self.expires = time.time() + int(answer["expires_in"]) - 60
        return self.token


def client_for(root, settings):
    if settings.get("account_mode") == "disconnected":
        return None
    record = connection(root)
    if record:
        return ConnectedClient(record)
    legacy = settings.get("legacy_runtime")
    if legacy and settings.get("account_mode") == "existing_owner":
        # This path is installed on the owner's PC only; never in a release.
        path = Path(legacy) / "ebay-oauth.json"
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return EbayClient(
            data["client_id"], data["client_secret"], data["refresh_token"]
        )
    return None
