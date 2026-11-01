"""Local loopback model router for Hermes task requests.

Routing chooses an explicitly enabled ChatGPT plan, connected free-plan cloud,
or local Ollama according to task size, speed, and availability. ChatGPT plan use shares the person's existing allowance; this
module does not buy credits or expose account tokens to Hermes. Credentials
are DPAPI encrypted and the loopback API has its own random bearer token.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

import win32crypt
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

import pc_offline
import cloud_providers
from auto_route import model_order, task_size
from model_identity import configured_main_alias, configured_permissive_alias
from model_library import CATALOG, candidate_approved, refresh_catalog

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "Cloud"
PORT = 11435
LOCAL_MODEL = configured_main_alias(ROOT)
PERMISSIVE_MODEL = configured_permissive_alias(ROOT)
FAST_MODEL = "cale-fast"
BALANCED_MODEL = "cale-qwen35"
API = "https://openrouter.ai/api/v1"
LOCK = threading.RLock()
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def credentials():
    """Use an OS file lock as GUI, launcher and router are separate processes."""
    import msvcrt

    STATE.mkdir(exist_ok=True)
    with (STATE / "credentials.lock").open("a+b") as lock:
        lock.seek(0)
        lock.write(b"1")
        lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            path = STATE / "credentials.dpapi"
            if path.exists():
                return json.loads(
                    win32crypt.CryptUnprotectData(
                        path.read_bytes(), None, None, None, 0
                    )[1]
                )
            result = {"router_token": secrets.token_urlsafe(32), "cloud_key": ""}
            save_credentials(result)
            return result
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def save_credentials(value):
    atomic(
        STATE / "credentials.dpapi",
        win32crypt.CryptProtectData(
            json.dumps(value).encode(),
            "Local AI account connection",
            None,
            None,
            None,
            0,
        ),
    )


def disconnect():
    # An already in-flight cloud request may finish. All following requests are local.
    value = credentials()
    value["cloud_key"] = ""
    save_credentials(value)
    record("local", "Disconnected. Future requests use local Qwen.")


def save_openrouter_key(key):
    if (
        not isinstance(key, str)
        or not key.startswith("sk-or-")
        or not 20 <= len(key) <= 300
    ):
        raise ValueError("Enter an OpenRouter API key from your account.")
    value = credentials()
    value["cloud_key"] = key
    save_credentials(value)
    record("connected", "OpenRouter key saved. Only zero-price routes are enabled.")


def save_provider_key(provider, key, confirmed_free=False):
    """Connect a person's own free-plan key; never package it with the app."""
    if provider not in cloud_providers.PROVIDERS:
        raise ValueError("Unknown provider")
    if not confirmed_free:
        raise ValueError("Confirm this account is on the provider's free plan first.")
    if not isinstance(key, str) or not 12 <= len(key.strip()) <= 500:
        raise ValueError("Paste a valid API key from your own account.")
    data = credentials()
    data.setdefault("provider_keys", {})[provider] = key.strip()
    data.setdefault("free_confirmed", {})[provider] = True
    save_credentials(data)
    record("connected", f"{cloud_providers.PROVIDERS[provider]['name']} free-plan account connected.")


def disconnect_provider(provider):
    if provider not in cloud_providers.PROVIDERS:
        raise ValueError("Unknown provider")
    data = credentials()
    data.setdefault("provider_keys", {}).pop(provider, None)
    data.setdefault("free_confirmed", {}).pop(provider, None)
    save_credentials(data)
    record("local", f"{cloud_providers.PROVIDERS[provider]['name']} disconnected.")


def record(backend, reason, model=""):
    event = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "backend": backend,
        "reason": reason,
        "model": model,
    }
    with LOCK:
        atomic(STATE / "status.json", json.dumps(event).encode())
        with (STATE / "routing.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")


def status():
    connected = bool(credentials().get("cloud_key"))
    try:
        result = json.loads((STATE / "status.json").read_text())
    except (OSError, ValueError):
        result = {}
    result["connected"] = connected
    result["local_profile"] = local_profile()
    return result


def local_profile():
    """Read the model chosen for future local requests, defaulting to main."""
    try:
        profile = json.loads((STATE / "model-choice.json").read_text()).get("profile")
        return profile if profile in CATALOG else "main"
    except (OSError, ValueError):
        return "main"


def select_profile(profile):
    """Select a downloaded local model only after Ollama confirms tool support."""
    if profile not in CATALOG:
        raise ValueError("Unknown model profile")
    model = CATALOG[profile]["alias"]
    info = post("http://127.0.0.1:11434/api/show", {"model": model}, timeout=10)
    if info.get("remote_host") or info.get("remote_model"):
        raise ValueError("This profile requires local model files.")
    if "tools" not in info.get("capabilities", []):
        raise ValueError("This model does not advertise tool support.")
    atomic(STATE / "model-choice.json", json.dumps({"profile": profile}).encode())
    record("local", "Model selection applies to the next request.", model)


def post(url, payload, key="", timeout=180):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    started = time.monotonic()
    with urlopen(
        Request(url, data=json.dumps(payload).encode(), headers=headers),
        timeout=timeout,
    ) as response:
        result = json.load(response)
    if url.endswith("/chat/completions"):
        import usage_history

        usage_history.record_completion(
            result, "openrouter" if key else "ollama", time.monotonic() - started
        )
    return result


def callback_code(path, expected_path, expected_state):
    parsed = urlsplit(path)
    params = parse_qs(parsed.query)
    if parsed.path != expected_path or not secrets.compare_digest(
        params.get("state", [""])[0], expected_state
    ):
        raise ValueError(
            "Account connection callback did not match this sign-in attempt."
        )
    code = params.get("code", [""])[0]
    if not code or len(code) > 4096:
        raise ValueError("No valid authorization code received.")
    return code


def connect_account():
    """Start OpenRouter's browser callback and protect the returned key locally."""
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    callback_path = "/connect/" + secrets.token_urlsafe(24)
    done = threading.Event()
    outcome = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Callback URLs contain a single-use secret.

        def do_GET(self):
            try:
                code = callback_code(self.path, callback_path, state)
            except ValueError:
                self.send_error(400, "Invalid or expired sign-in callback")
                return
            try:
                data = post(
                    API + "/auth/keys",
                    {
                        "code": code,
                        "code_verifier": verifier,
                        "code_challenge_method": "S256",
                    },
                    timeout=30,
                )
                key = data.get("key", "")
                if not isinstance(key, str) or not key.startswith("sk-or-"):
                    raise ValueError("The account service did not return a valid key.")
                value = credentials()
                value["cloud_key"] = key
                save_credentials(value)
                record("connected", "Free cloud enabled; paid routes disabled.")
                message = "Connected. You can close this tab and return to Local AI. Only free models are enabled."
                outcome["ok"] = True
            except Exception:
                message = "Connection failed. Return to Local AI and try Connect free cloud AI again."
                outcome["ok"] = False
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(message.encode())
            done.set()

    with ThreadingHTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        callback = f"http://127.0.0.1:{server.server_port}{callback_path}"
        url = "https://openrouter.ai/auth?" + urlencode(
            {
                "callback_url": callback,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "key_label": "Local AI - free models only",
            }
        )
        if not webbrowser.open(url):
            raise RuntimeError(
                "The browser could not open. Check your default browser and try again."
            )
        deadline = time.monotonic() + 600
        while not done.is_set() and time.monotonic() < deadline:
            server.handle_request()
    if not outcome.get("ok"):
        raise RuntimeError(
            "Sign-in was not completed. You can try again; local AI still works."
        )


def cloud_payload(payload):
    """Constrain a Hermes chat request to a zero-price OpenRouter route."""
    # Build from an allowlist. Never forward paid plugins, routes, models or presets.
    allowed = (
        "messages",
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "stop",
        "seed",
        "response_format",
        "parallel_tool_calls",
    )
    result = {k: payload[k] for k in allowed if k in payload}
    result.update(
        model="openrouter/free",
        stream=False,
        max_tokens=min(
            int(
                payload.get("max_tokens")
                or payload.get("max_completion_tokens")
                or 8192
            ),
            8192,
        ),
        provider={
            "require_parameters": True,
            "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
        },
    )
    return result


def valid_completion(result, request=None):
    """Validate model output before a worker can act on its tool calls."""
    if not isinstance(result, dict) or result.get("error") or not result.get("choices"):
        raise ValueError("Invalid inference response")
    message = result["choices"][0].get("message", {})
    if (not message.get("content") and not message.get("tool_calls")
            and not message.get("refusal")
            and result["choices"][0].get("finish_reason") != "content_filter"):
        raise ValueError("Empty inference response")
    offered = {
        tool["function"].get("name"): tool["function"].get("parameters")
        for tool in (request or {}).get("tools", [])
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }
    calls = message.get("tool_calls") or []
    for call in calls:
        if not isinstance(call, dict):
            raise ValueError("Invalid tool response")
        function = call.get("function", {})
        name = function.get("name") if isinstance(function, dict) else None
        if not call.get("id") or not name:
            raise ValueError("Invalid tool response")
        if request is not None and name not in offered:
            raise ValueError("Model called a tool that was not offered")
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            parsed = arguments
            function["arguments"] = json.dumps(arguments, ensure_ascii=False)
        elif isinstance(arguments, str):
            parsed = json.loads(arguments)
        else:
            parsed = None
        if not isinstance(parsed, dict):
            raise ValueError("Invalid tool arguments")
        schema = offered.get(name)
        if isinstance(schema, dict):
            validator = validator_for(schema)
            try:
                validator.check_schema(schema)
            except SchemaError:
                # A broken offered schema is an integration issue, not a
                # reason to reject every model's otherwise usable call.
                continue
            error = next(validator(schema).iter_errors(parsed), None)
            if error:
                raise ValueError(f"Invalid arguments for tool {name}: {error.message}")
    choice = (request or {}).get("tool_choice")
    named_choice = choice.get("function") if isinstance(choice, dict) else None
    required_name = (
        named_choice.get("name")
        if isinstance(named_choice, dict) and choice.get("type") == "function" else None
    )
    if (not is_refusal(result) and request is not None
            and (choice == "required" or required_name)
            and (not calls or (required_name and not any(
                call["function"]["name"] == required_name for call in calls
            )))):
        raise ValueError("Required tool call was missing")
    return result


REFUSAL_TEXT = re.compile(
    r"\b(?:I\s+(?:can(?:not|'t|’t)|won't|will not|must decline|have to decline|am unable|"
    r"am not able)|I'm\s+unable|I’m\s+unable|I\s+can't comply|I\s+cannot comply)\b",
    re.IGNORECASE,
)
DERESTRICTED_PROFILES = (
    "permissive_small", "permissive", "llama", "glm", "gptoss"
)


def is_refusal(result):
    """Recognize a model's rejection, independent of the request's subject."""
    try:
        choice = result["choices"][0]
        message = choice["message"]
        if message.get("tool_calls"):
            return False
        return bool(
            message.get("refusal")
            or choice.get("finish_reason") == "content_filter"
            or REFUSAL_TEXT.search(str(message.get("content") or "").replace("’", "'"))
        )
    except (KeyError, IndexError, TypeError):
        return False


def retry_derestricted(payload, result, original_model=""):
    """Retry any rejected completion with installed local derestricted models.

    Models without tool capability are only used for text requests. The
    original rejection remains available if every local retry fails.
    """
    if not is_refusal(result):
        return result
    profiles = (
        ("permissive", "llama", "glm", "permissive_small", "gptoss")
        if task_size(payload.get("messages", [])) == "deep"
        else DERESTRICTED_PROFILES
    )
    for profile in profiles:
        item = CATALOG.get(profile)
        if not item or not candidate_approved(item):
            continue
        model = item["alias"]
        if model == original_model:
            continue
        try:
            info = post("http://127.0.0.1:11434/api/show", {"model": model}, timeout=10)
            if info.get("remote_host") or info.get("remote_model"):
                continue
            if payload.get("tools") and "tools" not in info.get("capabilities", []):
                continue
            local = dict(payload, model=model, stream=False)
            local.pop("stream_options", None)
            if item.get("tool_reasoning_effort"):
                local["reasoning_effort"] = item["tool_reasoning_effort"]
            attempt = valid_completion(
                post("http://127.0.0.1:11434/v1/chat/completions", local, timeout=1800), payload
            )
            if not is_refusal(attempt):
                record("local", "A model refused; an installed derestricted model answered.", model)
                return attempt
        except Exception:
            continue
    record("local", "A model refused; no installed derestricted model answered.", original_model)
    return result


def estimated_context(payload):
    """Reserve room for the input, tool schemas and the next model response.

    Character count is an estimate rather than a tokenizer guarantee. The
    generous reserve prevents small routes from receiving near-limit prompts;
    the main model remains available when a request grows beyond them.
    """
    input_bytes = len(json.dumps({
        "messages": payload.get("messages", []),
        "tools": payload.get("tools", []),
    }, ensure_ascii=True).encode("utf-8"))
    return max(8192, input_bytes // 3 + 8192)


class Router:
    """Choose a provider for one request while preserving local fallback."""

    def __init__(self):
        self.cooldown = 0
        self.chatgpt_cooldown = 0
        self.last_attempt = 0
        self.lock = threading.Lock()
        self.model_cache = {}
        self.model_contexts = {}
        self.model_cache_until = 0
        self.provider_cooldowns = {}
        self.provider_last_attempts = {}

    def reserve_provider_request(self, provider):
        """Keep a conservative daily and per-minute budget per connected account."""
        spec = cloud_providers.PROVIDERS[provider]
        with self.lock:
            now = time.time()
            if now < self.provider_cooldowns.get(provider, 0):
                return False
            if now - self.provider_last_attempts.get(provider, 0) < 60 / spec["rpm"]:
                return False
            today = time.strftime("%Y-%m-%d", time.gmtime())
            path = STATE / "provider-quota.json"
            try:
                usage = json.loads(path.read_text())
            except (OSError, ValueError):
                usage = {}
            if usage.get("day") != today:
                usage = {"day": today, "attempts": {}, "monthly": usage.get("monthly", {})}
            attempts = usage.setdefault("attempts", {})
            if attempts.get(provider, 0) >= spec["daily"]:
                return False
            if spec.get("monthly"):
                month = today[:7]
                monthly = usage.setdefault("monthly", {})
                if monthly.get("month") != month:
                    monthly.clear()
                    monthly.update(month=month, attempts={})
                if monthly["attempts"].get(provider, 0) >= spec["monthly"]:
                    return False
                monthly["attempts"][provider] = monthly["attempts"].get(provider, 0) + 1
            attempts[provider] = attempts.get(provider, 0) + 1
            atomic(path, json.dumps(usage).encode())
            self.provider_last_attempts[provider] = now
            return True

    def local_candidates(self, size, required_context=65536):
        """Rank installed models that can hold this request, not the global maximum."""
        if time.monotonic() >= self.model_cache_until:
            refresh_catalog(online=False)
            available = {}
            contexts = {}
            aliases = dict.fromkeys(
                [FAST_MODEL, *(item["alias"] for item in CATALOG.values())]
            )
            for name in aliases:
                item = next(
                    (item for item in CATALOG.values() if item["alias"] == name), None
                )
                if item and (item.get("fallback_only") or not candidate_approved(item)):
                    continue
                try:
                    info = post(
                        "http://127.0.0.1:11434/api/show",
                        {"model": name},
                        timeout=10,
                    )
                    if info.get("remote_host") or info.get("remote_model"):
                        continue
                    if "tools" not in info.get("capabilities", []):
                        continue
                    configured = re.search(
                        r"(?m)^\s*num_ctx\s+(\d+)\s*$",
                        info.get("parameters", ""),
                    )
                    if not configured or int(configured.group(1)) < 64000:
                        continue
                    label = str(info.get("details", {}).get("parameter_size", ""))
                    match = re.search(r"(\d+(?:\.\d+)?)B", label, re.IGNORECASE)
                    available[name] = float(match.group(1)) if match else 1.0
                    contexts[name] = int(configured.group(1))
                except Exception:
                    continue
            self.model_cache = available
            self.model_contexts = contexts
            self.model_cache_until = time.monotonic() + 60
        balanced_preferred = BALANCED_MODEL
        if size == "balanced":
            # A newly recommended model must pass the on-device tool probe
            # before replacing the older 35B preference. Keep the old model
            # as a fallback when the recommendation is absent or unsuitable.
            for item in reversed(tuple(CATALOG.values())):
                alias = item["alias"]
                parameters = self.model_cache.get(alias, 0)
                if (
                    item.get("candidate")
                    and candidate_approved(item)
                    and 30 <= parameters <= 40
                    and self.model_contexts.get(alias, 0) >= required_context
                ):
                    balanced_preferred = alias
                    break
        ordered = model_order(
            size,
            {name: size_gb for name, size_gb in self.model_cache.items()
             if self.model_contexts.get(name, 0) >= required_context},
            LOCAL_MODEL, FAST_MODEL, balanced_preferred
        )
        # Keep the main alias as a fallback when Ollama metadata is missing,
        # but do not send a prompt to a model known to be too small for it.
        return [
            name for name in ordered
            if name != LOCAL_MODEL
            or name not in self.model_contexts
            or self.model_contexts[name] >= required_context
        ]

    def reserve_free_request(self):
        with self.lock:
            now = time.time()
            if now < self.cooldown or now - self.last_attempt < 3.1:
                return False
            today = time.strftime("%Y-%m-%d", time.gmtime())
            try:
                usage = json.loads((STATE / "quota.json").read_text())
            except (OSError, ValueError):
                usage = {}
            count = usage.get("attempts", 0) if usage.get("day") == today else 0
            if count >= 50:
                return False
            atomic(
                STATE / "quota.json",
                json.dumps({"day": today, "attempts": count + 1}).encode(),
            )
            self.last_attempt = now
            return True

    def complete(self, payload):
        """Return a validated completion from ChatGPT, free cloud, or Ollama."""
        if payload.get("model") not in (LOCAL_MODEL, LOCAL_MODEL + ":latest"):
            raise ValueError(
                "Only the configured local model and automatic free routing are supported."
            )
        profile = local_profile()
        size = task_size(payload.get("messages", [])) if profile == "main" else "manual"
        offline_mode = pc_offline.offline()
        key = credentials().get("cloud_key") if profile == "main" and not offline_mode else ""
        reason = "Cloud is not connected."
        # Explicit plan usage can supercharge demanding work. Quick requests
        # stay local so they do not spend the person's shared allowance.
        if (
            profile == "main"
            and size == "deep"
            and not offline_mode
            and time.time() >= self.chatgpt_cooldown
        ):
            try:
                import chatgpt_auth

                if chatgpt_auth.status()["enabled"]:
                    started = time.monotonic()
                    result = valid_completion(chatgpt_auth.complete(payload), payload)
                    import usage_history

                    usage_history.record_completion(result, "chatgpt", time.monotonic() - started)
                    record(
                        "ChatGPT plan",
                        "User-enabled ChatGPT plan response received.",
                        result.get("model", ""),
                    )
                    return retry_derestricted(payload, result, result.get("model", ""))
            except Exception:
                self.chatgpt_cooldown = time.time() + 120
                reason = "ChatGPT plan unavailable; trying free cloud or local AI."
        required_context = estimated_context(payload)
        openrouter_attempted = False
        if key and size == "deep" and self.reserve_free_request():
            openrouter_attempted = True
            try:
                result = valid_completion(post(
                    API + "/chat/completions", cloud_payload(payload), key, timeout=120,
                ), payload)
                record("free cloud", "Free route accelerated demanding work.",
                       result.get("model", "openrouter/free"))
                return retry_derestricted(payload, result, result.get("model", ""))
            except HTTPError as exc:
                self.cooldown = time.time() + (3600 if exc.code in (401, 402, 403, 429) else 120)
            except Exception:
                self.cooldown = time.time() + 120
        # Free-plan connections are optional. A fast connected provider gets
        # the first shot when it can hold the whole request. Errors, quotas,
        # and offline mode leave the local route intact.
        if profile == "main" and not offline_mode:
            account = credentials()
            keys = account.get("provider_keys") or {}
            confirmed = account.get("free_confirmed") or {}
            import usage_history
            local_speed = usage_history.recent_local_speed(
                70 if size == "quick" else 35 if size == "balanced" else 15)
            expected_output = min(8192, int(payload.get("max_tokens") or
                                            payload.get("max_completion_tokens") or 4096))
            prompt_estimate = max(1, len(json.dumps({
                "messages": payload.get("messages", []),
                "tools": payload.get("tools", []),
            }, ensure_ascii=True).encode("utf-8")) // 3)
            for provider, spec in sorted(cloud_providers.PROVIDERS.items(),
                                         key=lambda pair: pair[1]["speed"], reverse=True):
                provider_key = keys.get(provider)
                provider_context = prompt_estimate + min(expected_output, spec.get("max_tokens", 8192)) + 1024
                if (not provider_key or not confirmed.get(provider)
                        or provider_context > spec["context"]
                        or not cloud_providers.likely_faster(size, spec["speed"], local_speed)
                        or not self.reserve_provider_request(provider)):
                    continue
                try:
                    result, elapsed = cloud_providers.complete(provider, provider_key, payload)
                    result = valid_completion(result, payload)
                    import usage_history
                    usage_history.record_completion(result, provider, elapsed)
                    record(provider, "Connected free-plan cloud response received.",
                           result.get("model", spec["model"]))
                    return retry_derestricted(payload, result, result.get("model", ""))
                except HTTPError as exc:
                    self.provider_cooldowns[provider] = time.time() + (
                        3600 if exc.code in (401, 402, 403, 429) else 120)
                except Exception:
                    self.provider_cooldowns[provider] = time.time() + 120
        selected = (
            self.local_candidates(size, required_context)
            if profile == "main"
            else [CATALOG[profile]["alias"]]
        )
        last_error = None
        for selected_model in selected:
            local = dict(payload)
            local.update(model=selected_model, stream=False)
            local.pop("stream_options", None)
            if local.get("tools"):
                item = next(
                    (
                        entry
                        for entry in CATALOG.values()
                        if entry["alias"] == selected_model
                    ),
                    None,
                )
                if item and item.get("tool_reasoning_effort"):
                    local["reasoning_effort"] = item["tool_reasoning_effort"]
            try:
                result = valid_completion(
                    post(
                        "http://127.0.0.1:11434/v1/chat/completions",
                        local,
                        timeout=1800,
                    ), payload
                )
                record(
                    "local",
                    f"Auto scale: {size}; local model responded.",
                    selected_model,
                )
                return retry_derestricted(payload, result, selected_model)
            except Exception as exc:
                last_error = exc
        # Free cloud is a fallback after installed local models fail. It never
        # uses paid OpenRouter models and has a persisted daily request cap.
        if key and not openrouter_attempted and self.reserve_free_request():
            try:
                result = valid_completion(
                    post(
                        API + "/chat/completions",
                        cloud_payload(payload),
                        key,
                        timeout=120,
                    ), payload
                )
                record(
                    "free cloud",
                    "Free cloud response received.",
                    result.get("model", "openrouter/free"),
                )
                return retry_derestricted(payload, result, result.get("model", ""))
            except HTTPError as exc:
                # No response body, keys or project text is written into logs.
                self.cooldown = time.time() + (
                    3600 if exc.code in (401, 402, 403, 429) else 120
                )
                reason = f"Free cloud HTTP {exc.code}; no model responded."
            except Exception:
                self.cooldown = time.time() + 120
                reason = "Free cloud unavailable or incompatible."
        elif key:
            reason = "Free quota, rate limit or cooldown reached."
        record("unavailable", reason, selected[-1] if selected else "")
        if last_error:
            raise last_error
        if not selected and profile == "main":
            raise RuntimeError(
                f"This request needs about {required_context:,} tokens of context, "
                "but the installed local models have smaller windows. "
                "Use a capable connected model or shorten the input."
            )
        raise RuntimeError("No configured local or free cloud model responded.")


def stream_chunks(result):
    common = {k: result[k] for k in ("id", "created", "model") if k in result}
    common["object"] = "chat.completion.chunk"
    choices = []
    for i, choice in enumerate(result["choices"]):
        delta = dict(choice["message"])
        if delta.get("tool_calls"):
            delta["tool_calls"] = [
                dict(call, index=j) for j, call in enumerate(delta["tool_calls"])
            ]
        choices.append(
            {"index": i, "delta": delta, "finish_reason": choice.get("finish_reason")}
        )
    yield {**common, "choices": choices}
    if result.get("usage"):
        yield {**common, "choices": [], "usage": result["usage"]}


def serve():
    """Serve the loopback chat-completion API used by the Hermes configuration."""
    router = Router()
    token = credentials()["router_token"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def authorized(self):
            return secrets.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            )

        def send_json(self, value, code=200):
            data = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.authorized():
                self.send_json({"error": "Authentication required"}, 401)
                return
            if self.path == "/health":
                self.send_json({"service": "local-ai-free-router", **status()})
            elif self.path == "/v1/models":
                self.send_json(
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": LOCAL_MODEL,
                                "object": "model",
                                "owned_by": "local-ai",
                            }
                        ],
                    }
                )
            else:
                self.send_json({"error": "Not found"}, 404)

        def do_POST(self):
            if not self.authorized():
                self.send_json({"error": "Authentication required"}, 401)
                return
            if self.path != "/v1/chat/completions":
                self.send_json({"error": "Not found"}, 404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16_000_000:
                    raise ValueError()
                payload = json.loads(self.rfile.read(length))
                if payload.get("model") not in (LOCAL_MODEL, LOCAL_MODEL + ":latest"):
                    raise ValueError()
                if not isinstance(payload.get("messages"), list):
                    raise ValueError()
            except (ValueError, AttributeError):
                self.send_json({"error": "Invalid request or unsupported model"}, 400)
                return
            future = POOL.submit(router.complete, payload)
            if not payload.get("stream"):
                try:
                    self.send_json(future.result())
                except Exception:
                    self.send_json(
                        {
                            "error": {
                                "message": "Free cloud and/or local inference unavailable. Check Ollama.",
                                "type": "inference_error",
                            }
                        },
                        502,
                    )
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                # Buffer upstream completion so fallback cannot replay partially emitted tools.
                while not future.done():
                    try:
                        future.result(timeout=10)
                    except concurrent.futures.TimeoutError:
                        self.wfile.write(b": working\n\n")
                        self.wfile.flush()
                for chunk in stream_chunks(future.result()):
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.wfile.write(
                    b'data: {"error":{"message":"Inference unavailable. Check Ollama.","type":"inference_error","code":502}}\n\ndata: [DONE]\n\n'
                )

    with ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as server:
        atomic(STATE / "router-pid.txt", str(os.getpid()).encode())
        server.serve_forever()


def ensure_router():
    token = credentials()["router_token"]

    def health():
        with urlopen(
            Request(
                f"http://127.0.0.1:{PORT}/health",
                headers={"Authorization": "Bearer " + token},
            ),
            timeout=2,
        ) as response:
            return json.load(response).get("service") == "local-ai-free-router"

    try:
        if health():
            return token
    except OSError:
        pass
    pythonw = ROOT / "Toolbox/python/Scripts/pythonw.exe"
    subprocess.Popen(
        [str(pythonw), str(Path(__file__).resolve()), "--serve"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for _ in range(30):
        time.sleep(0.2)
        try:
            if health():
                return token
        except OSError:
            pass
    raise RuntimeError("The local AI router could not start on port 11435.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--ensure-token", action="store_true")
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.serve:
        serve()
    elif args.ensure_token:
        print(ensure_router())  # Launchers capture; never log this output.
    elif args.connect:
        connect_account()
    elif args.status:
        print(json.dumps(status()))
