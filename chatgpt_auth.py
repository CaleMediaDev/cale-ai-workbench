"""OpenAI's public-client Sign in with ChatGPT flow for a local OSS app.

Tokens are protected with Windows DPAPI. Connecting and enabling plan usage are
separate choices; the latter consumes the user's shared ChatGPT/Codex allowance.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import ssl
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

import jwt
import win32crypt
import certifi

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "Cloud"
AUTH = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
LOCK = threading.RLock()


def tls_context():
    """Use the bundled, current CA roots for Python's HTTPS requests.

    Some Windows machines retain expired intermediate certificates in the OS
    store. Explicitly using certifi avoids choosing that stale trust path while
    keeping full certificate and hostname verification enabled.
    """
    return ssl.create_default_context(cafile=certifi.where())


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def load():
    """Decrypt this Windows user's saved ChatGPT connection, if present."""
    path = STATE / "chatgpt.dpapi"
    if not path.exists():
        return {}
    return json.loads(
        win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)[1]
    )


def save(value):
    """Atomically replace the DPAPI-encrypted token record."""
    atomic(
        STATE / "chatgpt.dpapi",
        win32crypt.CryptProtectData(
            json.dumps(value).encode(),
            "Cale AI ChatGPT connection",
            None,
            None,
            None,
            0,
        ),
    )


def host_id():
    """Keep a stable, installation-specific ID for public-client registration."""
    path = STATE / "chatgpt-host.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["host_id"]
    except (OSError, ValueError, KeyError):
        value = "urn:uuid:" + str(uuid.uuid4())
        atomic(path, json.dumps({"host_id": value}).encode())
        return value


def status():
    try:
        value = load()
    except (OSError, ValueError):
        value = {}
    scopes = set(value.get("scopes", []))
    sharing = bool(value.get("access_token") and "chatgpt.tokens.use.direct" in scopes)
    return {
        "connected": bool(value.get("subject")),
        "sharing": sharing,
        "enabled": sharing and bool(value.get("enabled")),
        "email": value.get("email", ""),
    }


def form_post(url, data, timeout=30):
    request = Request(
        url,
        data=urlencode(data).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout, context=tls_context()) as response:
        return json.load(response)


def verify_identity(id_token, client_id, nonce):
    header = jwt.get_unverified_header(id_token)
    if header.get("alg") not in ("RS256", "PS256", "ES256"):
        raise ValueError("Unsupported ChatGPT identity signature.")
    key = jwt.PyJWKClient(
        AUTH + "/.well-known/jwks.json", timeout=10, ssl_context=tls_context()
    ).get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        key.key,
        algorithms=[header["alg"]],
        audience=client_id,
        issuer=AUTH,
        leeway=5,
        options={"require": ["sub", "exp", "iat"]},
    )
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ValueError("ChatGPT sign-in did not match this attempt.")
    if not claims.get("sub"):
        raise ValueError("ChatGPT identity is missing.")
    return claims


def connect():
    """Open official browser sign-in and validate the loopback OAuth callback."""
    with LOCK:
        previous = load()
    known_client = previous.get("client_id", "")
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    done = threading.Event()
    outcome = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != "/auth/callback" or not secrets.compare_digest(
                query.get("state", [""])[0], state
            ):
                self.send_error(400, "Invalid sign-in callback")
                return
            try:
                if query.get("error"):
                    raise ValueError("ChatGPT sign-in was not approved.")
                code = query.get("code", [""])[0]
                client_id = query.get("client_id", [known_client])[0]
                if not code or len(code) > 4096 or not client_id.startswith("oaiapp_"):
                    raise ValueError("ChatGPT did not return a valid authorization.")
                if known_client and client_id != known_client:
                    raise ValueError("ChatGPT returned a different app registration.")
                tokens = form_post(
                    AUTH + "/api/accounts/oauth/token",
                    {
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "code": code,
                        "code_verifier": verifier,
                        "redirect_uri": redirect_uri,
                        "resource": RESOURCE,
                    },
                )
                claims = verify_identity(tokens["id_token"], client_id, nonce)
                if previous.get("subject") and claims["sub"] != previous["subject"]:
                    raise ValueError(
                        "Choose the previously connected account, or disconnect it first."
                    )
                scopes = tokens.get("scope", "").split()
                record = {
                    "client_id": client_id,
                    "host_id": host_id(),
                    "subject": claims["sub"],
                    "email": claims.get("email", ""),
                    "id_token": tokens["id_token"],
                    "access_token": tokens["access_token"],
                    "refresh_token": tokens.get("refresh_token", ""),
                    "scopes": scopes,
                    "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
                    "enabled": False,
                }
                with LOCK:
                    save(record)
                outcome["ok"] = True
                message = "ChatGPT connected. Return to Cale AI Workbench to choose whether to use your plan."
            except Exception as exc:
                outcome["error"] = str(exc)
                message = "ChatGPT connection did not finish. Return to Cale AI Workbench and try again."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(message.encode())
            done.set()

    with ThreadingHTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
        params = {
            "client_id": known_client or "dynamic_agent_client",
            "ext_agent_host_id": host_id(),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if known_client:
            if previous.get("id_token"):
                params["id_token_hint"] = previous["id_token"]
            if previous.get("email"):
                params["login_hint"] = previous["email"]
        else:
            params["agent_name_hint"] = "Cale AI Workbench"
        if not webbrowser.open(AUTH + "/api/accounts/authorize?" + urlencode(params)):
            raise RuntimeError("Could not open your browser for ChatGPT sign-in.")
        deadline = time.monotonic() + 600
        while not done.is_set() and time.monotonic() < deadline:
            server.handle_request()
    if not outcome.get("ok"):
        raise RuntimeError(outcome.get("error", "ChatGPT sign-in timed out."))
    return status()


def set_enabled(enabled):
    """Keep plan usage separate from merely connecting an account."""
    with LOCK:
        value = load()
        if enabled and "chatgpt.tokens.use.direct" not in value.get("scopes", []):
            raise ValueError("ChatGPT plan use was not approved during sign-in.")
        value["enabled"] = bool(enabled)
        save(value)


def disconnect():
    """Disable local use immediately, then attempt remote token revocation."""
    with LOCK:
        value = load()
        if not value:
            return
        value["enabled"] = False
        save(value)
        token = value.get("refresh_token", "")
        if token:
            try:
                with urlopen(
                    Request(AUTH + "/.well-known/openid-configuration"),
                    timeout=10,
                    context=tls_context(),
                ) as response:
                    endpoint = json.load(response)["revocation_endpoint"]
                request = Request(
                    endpoint,
                    data=urlencode(
                        {
                            "token": token,
                            "token_type_hint": "refresh_token",
                            "client_id": value["client_id"],
                        }
                    ).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with urlopen(request, timeout=10, context=tls_context()) as response:
                    if response.status != 200:
                        raise RuntimeError("Revocation failed.")
            except Exception:
                save({})
                raise RuntimeError(
                    "Disconnected locally. Remote revocation could not be confirmed; review connected apps in ChatGPT settings."
                )
        save({})


def access_token():
    """Refresh an opted-in token shortly before expiry."""
    with LOCK:
        value = load()
        if not value.get("enabled") or "chatgpt.tokens.use.direct" not in value.get(
            "scopes", []
        ):
            raise ValueError("ChatGPT plan use is not enabled.")
        if time.time() < value.get("expires_at", 0) - 120:
            return value["access_token"]
        if not value.get("refresh_token"):
            raise ValueError("ChatGPT sign-in needs to be renewed.")
        tokens = form_post(
            AUTH + "/api/accounts/oauth/token",
            {
                "grant_type": "refresh_token",
                "client_id": value["client_id"],
                "refresh_token": value["refresh_token"],
                "resource": RESOURCE,
            },
        )
        value.update(
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token", value["refresh_token"]),
            expires_at=time.time() + int(tokens.get("expires_in", 3600)),
            scopes=tokens.get("scope", " ".join(value["scopes"])).split(),
        )
        save(value)
        return value["access_token"]


def available_models(token):
    request = Request(
        RESOURCE + "/models", headers={"Authorization": "Bearer " + token}
    )
    with urlopen(request, timeout=15, context=tls_context()) as response:
        catalog = json.load(response)
    models = [
        item.get("slug")
        for item in catalog.get("models", [])
        if item.get("visibility") == "list" and item.get("slug")
    ]
    if not models:
        raise RuntimeError("No ChatGPT plan models are available for this account.")
    return models


def response_payload(completion, model):
    """Translate Hermes chat messages and tool calls to Responses API input."""
    instructions = []
    items = []
    for message in completion.get("messages", []):
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if isinstance(content, str):
                instructions.append(content)
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": content
                    if isinstance(content, str)
                    else json.dumps(content),
                }
            )
            continue
        if role == "assistant" and message.get("tool_calls"):
            if content:
                items.append({"role": "assistant", "content": content})
            for call in message["tool_calls"]:
                function = call["function"]
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": function["name"],
                        "arguments": function["arguments"],
                    }
                )
            continue
        if role in ("user", "assistant", "developer"):
            items.append({"role": role, "content": content or ""})
    tools = []
    for tool in completion.get("tools", []):
        if tool.get("type") != "function":
            continue
        function = tool["function"]
        tools.append(
            {
                "type": "function",
                "name": function["name"],
                "description": function.get("description", ""),
                "parameters": function.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
                "strict": False,
            }
        )
    payload = {"model": model, "input": items, "store": False, "stream": True}
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    if tools:
        payload["tools"] = tools
    return payload


def response_to_completion(response):
    """Translate a completed Responses event to Hermes' chat shape."""
    content = []
    calls = []
    for item in response.get("output", []):
        if item.get("type") == "function_call":
            calls.append(
                {
                    "id": item["call_id"],
                    "type": "function",
                    "function": {"name": item["name"], "arguments": item["arguments"]},
                }
            )
        elif item.get("type") == "message":
            content.extend(
                part.get("text", "")
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            )
    message = {"role": "assistant", "content": "".join(content) or None}
    if calls:
        message["tool_calls"] = calls
    if not calls and not message["content"]:
        raise RuntimeError("ChatGPT returned an empty response.")
    usage = response.get("usage") or {}
    return {
        "id": response.get("id", ""),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response.get("model", "chatgpt-plan"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        } if usage else {},
    }


def complete(completion):
    """Stream a plan request and return only after response.completed arrives."""
    token = access_token()
    models = available_models(token)
    model = "gpt-6.1-sol" if "gpt-6.1-sol" in models else models[0]
    payload = response_payload(completion, model)
    request = Request(
        RESOURCE + "/responses",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    final = None
    streamed_output = {}
    with urlopen(request, timeout=180, context=tls_context()) as response:
        for raw in response:
            if not raw.startswith(b"data: "):
                continue
            body = raw[6:].strip()
            if body == b"[DONE]":
                break
            event = json.loads(body)
            if event.get("type") == "response.completed":
                final = event.get("response")
            elif event.get("type") == "response.output_item.done":
                # ChatGPT plan streaming can leave response.completed.output empty.
                # Completed items carry both answer text and tool calls.
                item = event.get("item")
                if item is not None:
                    streamed_output[event.get("output_index", len(streamed_output))] = item
            elif event.get("type") in (
                "response.failed",
                "response.incomplete",
                "error",
            ):
                error = (
                    event.get("response", {}).get("error") or event.get("error") or {}
                )
                raise RuntimeError(
                    "ChatGPT response failed: " + str(error.get("code", "unknown"))
                )
    if not final:
        raise RuntimeError("ChatGPT stream ended before completion.")
    if not final.get("output") and streamed_output:
        final["output"] = [streamed_output[index] for index in sorted(streamed_output)]
    return response_to_completion(final)
