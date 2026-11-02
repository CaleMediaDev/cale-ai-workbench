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
import sqlite3
import subprocess
import threading
import time
import webbrowser
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

import win32crypt
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

import cloud_providers
import pc_offline
import routing_memory
import routing_mode
from auto_route import model_order, repair_messages, task_size
from model_identity import configured_main_alias, configured_permissive_alias
from model_library import (
    CATALOG,
    STANDARD_MAIN_ALIAS,
    candidate_approved,
    refresh_catalog,
)

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


def task_route(task_id: str) -> str:
    """Read a task's provider choice at request time, so a running task can switch."""
    if not re.fullmatch(r"[a-f0-9]{12}", task_id or ""):
        return "automatic"
    try:
        with closing(sqlite3.connect(f"file:{ROOT / 'LongTasks/projects.db'}?mode=ro",
                                     uri=True, timeout=2)) as conn:
            row = conn.execute("SELECT ai_route FROM projects WHERE id=?", (task_id,)).fetchone()
        return "local_only" if row and row[0] == "local_only" else "automatic"
    except sqlite3.Error:
        # An attributed task with an unreadable policy must never unexpectedly
        # spend a connected account's allowance.
        return "local_only"


def local_speed_models(size):
    """Compare cloud speed with the local model tier this task would use."""
    refresh_catalog(online=False)
    if size == "quick":
        return (FAST_MODEL,)
    if size == "balanced":
        aliases = [BALANCED_MODEL]
        aliases.extend(
            item["alias"] for item in CATALOG.values()
            if (not item.get("fallback_only") and candidate_approved(item)
                and 12 <= item.get("download_gb", 0) < 35)
        )
    else:
        aliases = [LOCAL_MODEL, STANDARD_MAIN_ALIAS]
        aliases.extend(
            item["alias"] for item in CATALOG.values()
            if (not item.get("fallback_only") and candidate_approved(item)
                and item.get("download_gb", 0) >= 35)
        )
    return tuple(dict.fromkeys(aliases))


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


def save_provider_key(provider, key, confirmed_free=False, account_id=""):
    """Connect a person's own free-plan key; never package it with the app."""
    if provider not in cloud_providers.PROVIDERS:
        raise ValueError("Unknown provider")
    if not confirmed_free:
        raise ValueError("Confirm this account is on the provider's free plan first.")
    if not isinstance(key, str) or not 12 <= len(key.strip()) <= 500:
        raise ValueError("Paste a valid API key from your own account.")
    if provider == "cloudflare" and not re.fullmatch(r"[a-fA-F0-9]{32}", account_id):
        raise ValueError("Paste your 32-character Cloudflare account ID.")
    data = credentials()
    data.setdefault("provider_keys", {})[provider] = key.strip()
    data.setdefault("free_confirmed", {})[provider] = True
    if provider == "cloudflare":
        data.setdefault("provider_accounts", {})[provider] = account_id.lower()
    save_credentials(data)
    record("connected", f"{cloud_providers.PROVIDERS[provider]['name']} free-plan account connected.")


def disconnect_provider(provider):
    if provider not in cloud_providers.PROVIDERS:
        raise ValueError("Unknown provider")
    data = credentials()
    data.setdefault("provider_keys", {}).pop(provider, None)
    data.setdefault("free_confirmed", {}).pop(provider, None)
    data.setdefault("provider_accounts", {}).pop(provider, None)
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


def retry_derestricted(payload, result, original_model="", deadline=None):
    """Retry any rejected completion with installed local derestricted models.

    Models without tool capability are only used for text requests. The
    original rejection remains available if every local retry fails.
    """
    if not is_refusal(result):
        return result
    if routing_mode.load_scope(STATE) == "cloud_only":
        return result
    import model_fitness
    import model_library

    ram = model_library.total_ram_gb()
    vram = model_fitness.gpu_vram_gb()
    profiles = (
        ("permissive", "llama", "glm", "permissive_small", "gptoss")
        if task_size(payload.get("messages", [])) == "deep"
        else DERESTRICTED_PROFILES
    )
    for profile in profiles:
        if deadline is not None and time.monotonic() >= deadline:
            break
        item = CATALOG.get(profile)
        if not item or not candidate_approved(item):
            continue
        model = item["alias"]
        if not model_fitness.suitable(model, item.get("download_gb", 0), ram, vram):
            continue
        if model == original_model:
            continue
        try:
            probe_timeout = min(10, max(0.1, deadline - time.monotonic())) if deadline else 10
            info = post("http://127.0.0.1:11434/api/show", {"model": model}, timeout=probe_timeout)
            if info.get("remote_host") or info.get("remote_model"):
                continue
            if payload.get("tools") and "tools" not in info.get("capabilities", []):
                continue
            local = dict(payload, model=model, stream=False)
            local.pop("stream_options", None)
            if item.get("tool_reasoning_effort"):
                local["reasoning_effort"] = item["tool_reasoning_effort"]
            if deadline is not None and time.monotonic() >= deadline:
                break
            response_timeout = min(1800, max(0.1, deadline - time.monotonic())) if deadline else 1800
            attempt = valid_completion(
                post("http://127.0.0.1:11434/v1/chat/completions", local,
                     timeout=response_timeout), payload
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


def loaded_local_models():
    """Return active Ollama aliases, excluding models already unloading."""
    try:
        with urlopen("http://127.0.0.1:11434/api/ps", timeout=2) as response:
            entries = json.load(response).get("models", [])
        now = datetime.now(timezone.utc)
        loaded = []
        for item in entries:
            expiry = datetime.fromisoformat(str(item.get("expires_at", "")))
            name = str(item.get("name") or item.get("model") or "")
            if expiry.tzinfo and expiry > now and name:
                loaded.append(name.removesuffix(":latest"))
        return loaded
    except (OSError, ValueError, TypeError, KeyError):
        return []


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

    def reserve_provider_request(self, provider, estimated_cost=0, estimated_tokens=0):
        """Keep a conservative daily and per-minute budget per connected account."""
        return self.provider_request_available(
            provider, estimated_cost, estimated_tokens, reserve=True
        )

    def provider_request_available(self, provider, estimated_cost=0,
                                   estimated_tokens=0, reserve=False):
        """Check account limits without spending them during route ranking."""
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
                if reserve:
                    routing_memory.record_budget_limit(provider)
                return False
            if spec.get("daily_neurons"):
                spent = usage.setdefault("neurons", {}).get(provider, 0)
                if estimated_cost <= 0 or spent + estimated_cost > spec["daily_neurons"]:
                    if reserve:
                        routing_memory.record_budget_limit(provider)
                    return False
                if reserve:
                    usage["neurons"][provider] = spent + estimated_cost
            if spec.get("daily_tokens"):
                spent = usage.setdefault("tokens", {}).get(provider, 0)
                if estimated_tokens <= 0 or spent + estimated_tokens > spec["daily_tokens"]:
                    if reserve:
                        routing_memory.record_budget_limit(provider)
                    return False
                if reserve:
                    usage["tokens"][provider] = spent + estimated_tokens
            if spec.get("monthly"):
                month = today[:7]
                monthly = usage.setdefault("monthly", {})
                if monthly.get("month") != month:
                    monthly.clear()
                    monthly.update(month=month, attempts={})
                if monthly["attempts"].get(provider, 0) >= spec["monthly"]:
                    if reserve:
                        routing_memory.record_budget_limit(provider)
                    return False
                if reserve:
                    monthly["attempts"][provider] = monthly["attempts"].get(provider, 0) + 1
            if reserve:
                attempts[provider] = attempts.get(provider, 0) + 1
                atomic(path, json.dumps(usage).encode())
                self.provider_last_attempts[provider] = now
            return True

    def local_candidates(self, size, required_context=65536):
        """Rank installed models that can hold this request, not the global maximum."""
        if time.monotonic() >= self.model_cache_until:
            refresh_catalog(online=False)
            import model_fitness
            import model_library

            ram = model_library.total_ram_gb()
            vram = model_fitness.gpu_vram_gb()
            try:
                with urlopen("http://127.0.0.1:11434/api/tags", timeout=10) as response:
                    tags = json.load(response)
                weights = {entry["name"]: entry["size"] / 2**30
                           for entry in tags.get("models", [])}
            except Exception:
                weights = {}
            available = {}
            contexts = {}
            aliases = dict.fromkeys(
                [FAST_MODEL, STANDARD_MAIN_ALIAS,
                 *(item["alias"] for item in CATALOG.values())]
            )
            for name in aliases:
                item = next(
                    (item for item in CATALOG.values() if item["alias"] == name), None
                )
                if item and (item.get("fallback_only") or not candidate_approved(item)):
                    continue
                weight = weights.get(name, weights.get(name + ":latest"))
                if weight is None or not model_fitness.suitable(name, weight, ram, vram):
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
        if (size == "deep" and required_context <= 65536 and
                STANDARD_MAIN_ALIAS in ordered):
            ordered.remove(STANDARD_MAIN_ALIAS)
            ordered.insert(0, STANDARD_MAIN_ALIAS)
        elif size in ("quick", "balanced"):
            for loaded in loaded_local_models():
                parameters = self.model_cache.get(loaded, 0)
                same_tier = (
                    0 < parameters <= 12 if size == "quick"
                    else 20 <= parameters <= 40
                )
                # Reusing loaded weights saves startup time only when the
                # model stays within the fast or middle-size range. A loaded
                # 79B model can make every quick or balanced turn crawl.
                if loaded in ordered and same_tier:
                    ordered.remove(loaded)
                    ordered.insert(0, loaded)
                    break
        return ordered

    def reserve_free_request(self):
        return self.free_request_available(reserve=True)

    def free_request_available(self, reserve=False):
        """Check OpenRouter's shared cap without consuming it for ranking."""
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
                if reserve:
                    routing_memory.record_budget_limit("openrouter")
                return False
            if reserve:
                atomic(
                    STATE / "quota.json",
                    json.dumps({"day": today, "attempts": count + 1}).encode(),
                )
                self.last_attempt = now
            return True

    def complete(self, payload, task_id=""):
        """Attribute this router request before any provider records its usage."""
        import usage_history

        token = usage_history.CURRENT_TASK.set(task_id)
        try:
            return self._complete(payload)
        finally:
            usage_history.CURRENT_TASK.reset(token)

    def _complete(self, payload):
        """Return a validated completion from ChatGPT, free cloud, or Ollama."""
        import usage_history

        if payload.get("model") not in (LOCAL_MODEL, LOCAL_MODEL + ":latest"):
            raise ValueError(
                "Only the configured local model and automatic free routing are supported."
            )
        profile = local_profile()
        messages = payload.get("messages", [])
        size = task_size(messages) if profile == "main" else "manual"
        repaired = repair_messages(messages)
        if repaired is not messages:
            payload = {**payload, "messages": repaired}
        scope = routing_mode.load_scope(STATE) if profile == "main" else "both"
        cloud_only = profile == "main" and scope == "cloud_only"
        offline_mode = (pc_offline.offline() or scope == "local_only" or
                        (scope == "both" and task_route(
                            usage_history.CURRENT_TASK.get()) == "local_only"))
        key = credentials().get("cloud_key") if profile == "main" and not offline_mode else ""
        mode = routing_mode.load(STATE) if profile == "main" else routing_mode.DEFAULT
        reason = "Cloud is not connected."
        local_speed = usage_history.recent_local_speed(
            70 if size == "quick" else 35 if size == "balanced" else 15,
            models=local_speed_models(size),
        )
        paid_pressure = routing_memory.pressure("chatgpt")
        account = credentials() if profile == "main" and not offline_mode else {}
        keys = account.get("provider_keys") or {}
        confirmed = account.get("free_confirmed") or {}
        connected = [(provider, spec) for provider, spec in cloud_providers.PROVIDERS.items()
                     if keys.get(provider) and confirmed.get(provider)]
        measured_speeds = {
            provider: usage_history.recent_provider_speed(provider, spec["speed"])
            for provider, spec in connected
        }
        required_context = estimated_context(payload)
        expected_output = min(8192, int(payload.get("max_tokens") or
                                        payload.get("max_completion_tokens") or 4096))
        prompt_estimate = max(1, len(json.dumps({
            "messages": payload.get("messages", []),
            "tools": payload.get("tools", []),
        }, ensure_ascii=True).encode("utf-8")) // 3)
        paid_speed = (
            usage_history.recent_provider_speed("chatgpt", 140)
            if mode == "floor_it" else 140
        )
        openrouter_speed = (
            usage_history.recent_provider_speed("openrouter", 120)
            if key and self.free_request_available() else 0
        )
        eligible_free_speeds = [
            measured_speeds[provider] for provider, spec in connected
            if (prompt_estimate + min(expected_output, spec.get("max_tokens", 8192)) + 1024
                <= spec["context"]
                and time.time() >= self.provider_cooldowns.get(provider, 0)
                and time.time() - routing_memory.pressure(provider)["last_limit"] >= 3600
                and self.provider_request_available(
                    provider,
                    cloud_providers.estimated_neurons(provider, payload),
                    cloud_providers.estimated_tokens(provider, payload)))
        ]
        fastest_free_speed = max([openrouter_speed, *eligible_free_speeds], default=0)
        paid_attempted = False

        def try_paid():
            nonlocal paid_attempted, reason
            if (profile != "main" or offline_mode or paid_attempted
                    or time.time() < self.chatgpt_cooldown
                    or time.time() - paid_pressure["last_limit"] < 3600):
                return None
            try:
                import chatgpt_auth

                if not chatgpt_auth.status()["enabled"]:
                    return None
                paid_attempted = True
                started = time.monotonic()
                result = valid_completion(chatgpt_auth.complete(payload, size=size), payload)
                routing_memory.record("chatgpt", size, "success")
                usage_history.record_completion(result, "chatgpt", time.monotonic() - started)
                record("ChatGPT plan", "User-enabled ChatGPT plan response received.",
                       result.get("model", ""))
                return retry_derestricted(payload, result, result.get("model", ""))
            except Exception as exc:
                paid_attempted = True
                kind = routing_memory.failure_kind(exc)
                routing_memory.record("chatgpt", size, kind)
                self.chatgpt_cooldown = time.time() + (3600 if kind == "limit" else 120)
                reason = "ChatGPT plan unavailable; trying free cloud or local AI."
                return None

        # Learn from observed limits without pretending to know the account's
        # remaining allowance. Quick work is kept local; repeated plan limits
        # reserve paid access for deep work after its cooldown expires.
        if profile == "main" and routing_mode.paid_first(
            mode, size, local_speed, paid_speed, fastest_free_speed,
            routing_memory.use_paid(size, local_speed, paid_pressure),
        ):
            paid_result = try_paid()
            if paid_result is not None:
                return paid_result
        openrouter_attempted = False
        # Free-plan connections are optional. A fast connected provider gets
        # the first shot when it can hold the whole request. Errors, quotas,
        # and offline mode leave the local route intact.
        if profile == "main" and not offline_mode:
            for provider, spec in sorted(connected,
                                         key=lambda pair: measured_speeds[pair[0]], reverse=True):
                provider_key = keys.get(provider)
                provider_context = prompt_estimate + min(expected_output, spec.get("max_tokens", 8192)) + 1024
                estimated_cost = cloud_providers.estimated_neurons(provider, payload)
                estimated_tokens = cloud_providers.estimated_tokens(provider, payload)
                provider_pressure = routing_memory.pressure(provider)
                if (not provider_key or not confirmed.get(provider)
                        or provider_context > spec["context"]
                        or (time.time() - provider_pressure["last_limit"] < 3600)
                        or not (cloud_only or routing_mode.free_first(
                            mode,
                            routing_memory.use_free(size, local_speed, provider_pressure),
                            cloud_providers.likely_faster(size, measured_speeds[provider], local_speed),
                        ))
                        or not self.reserve_provider_request(provider, estimated_cost, estimated_tokens)):
                    continue
                try:
                    result, elapsed = cloud_providers.complete(
                        provider, provider_key, payload,
                        account_id=(account.get("provider_accounts") or {}).get(provider, ""))
                    result = valid_completion(result, payload)
                    routing_memory.record(provider, size, "success")
                    usage_history.record_completion(result, provider, elapsed)
                    record(provider, "Connected free-plan cloud response received.",
                           result.get("model", spec["model"]))
                    return retry_derestricted(payload, result, result.get("model", ""))
                except HTTPError as exc:
                    routing_memory.record(provider, size, routing_memory.failure_kind(exc))
                    self.provider_cooldowns[provider] = time.time() + (
                        3600 if exc.code in (401, 402, 403, 429) else 120)
                except Exception as exc:
                    routing_memory.record(provider, size, routing_memory.failure_kind(exc))
                    self.provider_cooldowns[provider] = time.time() + 120
        # A long balanced task can be slower than a deep task when its local
        # model repeatedly prefills a large context. Use the connected free
        # route when measured local throughput is poor, then fall back locally.
        slow_long_context = (
            size == "balanced"
            and (prompt_estimate >= 24_000 or
                 (prompt_estimate >= 16_000 and local_speed < 20))
        ) if profile == "main" and not offline_mode else False
        # Prefer an eligible direct free-plan account before spending the
        # smaller shared OpenRouter free allowance on demanding work.
        if (key and (cloud_only or routing_mode.free_first(
                    mode,
                    routing_memory.use_free(
                        size, local_speed, routing_memory.pressure("openrouter")),
                    (size == "deep" or slow_long_context) if mode == "medium" else
                    cloud_providers.likely_faster(size, openrouter_speed, local_speed),
                ))
                and self.reserve_free_request()):
            openrouter_attempted = True
            try:
                result = valid_completion(post(
                    API + "/chat/completions", cloud_payload(payload), key, timeout=120,
                ), payload)
                routing_memory.record("openrouter", size, "success")
                record("free cloud", "Free route accelerated demanding work.",
                       result.get("model", "openrouter/free"))
                return retry_derestricted(payload, result, result.get("model", ""))
            except HTTPError as exc:
                routing_memory.record("openrouter", size, routing_memory.failure_kind(exc))
                self.cooldown = time.time() + (3600 if exc.code in (401, 402, 403, 429) else 120)
            except Exception as exc:
                routing_memory.record("openrouter", size, routing_memory.failure_kind(exc))
                self.cooldown = time.time() + 120
        if (mode == "floor_it" and profile == "main" and not offline_mode
                and cloud_providers.likely_faster(size, paid_speed, local_speed)):
            paid_result = try_paid()
            if paid_result is not None:
                return paid_result
        selected = (
            [] if profile == "main" and scope == "cloud_only" else
            self.local_candidates(size, required_context) if profile == "main" else
            [CATALOG[profile]["alias"]]
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
                routing_memory.record("openrouter", size, "success")
                record(
                    "free cloud",
                    "Free cloud response received.",
                    result.get("model", "openrouter/free"),
                )
                return retry_derestricted(payload, result, result.get("model", ""))
            except HTTPError as exc:
                routing_memory.record("openrouter", size, routing_memory.failure_kind(exc))
                # No response body, keys or project text is written into logs.
                self.cooldown = time.time() + (
                    3600 if exc.code in (401, 402, 403, 429) else 120
                )
                reason = f"Free cloud HTTP {exc.code}; no model responded."
            except Exception as exc:
                routing_memory.record("openrouter", size, routing_memory.failure_kind(exc))
                self.cooldown = time.time() + 120
                reason = "Free cloud unavailable or incompatible."
        elif key:
            reason = "Free quota, rate limit or cooldown reached."
        if (mode in ("paid_saver", "floor_it") or cloud_only) and profile == "main" and not offline_mode:
            paid_result = try_paid()
            if paid_result is not None:
                return paid_result
        record("unavailable", reason, selected[-1] if selected else "")
        if last_error:
            raise last_error
        if not selected and scope == "cloud_only" and profile == "main":
            raise RuntimeError("Cloud only is selected, but no connected cloud model is available. Connect one in Supercharge or select Both.")
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


def task_for_client(client_port):
    """Attribute a loopback caller only when it is a managed task worker.

    The socket's owning PID identifies the worker without trusting a request
    body or caller-supplied task header. Ordinary chat remains unattributed.
    """
    import psutil

    project_root = (ROOT / "Projects").resolve()
    try:
        connections = psutil.net_connections(kind="tcp")
    except (OSError, psutil.Error):
        return ""
    for connection in connections:
        try:
            if (not connection.pid or not connection.raddr
                    or connection.laddr.port != client_port
                    or connection.raddr.port != PORT):
                continue
            process = psutil.Process(connection.pid)
            for ancestor in (process, *process.parents()):
                if "agent.managed_worker" not in ancestor.cmdline():
                    continue
                folder = Path(ancestor.cwd()).resolve()
                if (folder.parent == project_root
                        and re.fullmatch(r"[a-f0-9]{12}", folder.name)):
                    return folder.name
        except (OSError, psutil.Error, AttributeError):
            continue
    return ""


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
            future = POOL.submit(router.complete, payload,
                                 task_for_client(self.client_address[1]))
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
    parser.add_argument("--ensure", action="store_true")
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.serve:
        serve()
    elif args.ensure_token:
        print(ensure_router())  # Launchers capture; never log this output.
    elif args.ensure:
        ensure_router()
    elif args.connect:
        connect_account()
    elif args.status:
        print(json.dumps(status()))
