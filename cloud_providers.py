"""Small, explicit adapters for optional user-owned cloud inference accounts.

The marketing homepages are not API bases. Keep endpoints and model IDs here,
and never accept a model or URL supplied by an untrusted task prompt.
"""

from __future__ import annotations

import json
import re
import time
from urllib.request import Request, urlopen

# Official user-owned agents are separate from inference API/free-plan keys.
# These pinned native packages are published by the vendors on npm. We verify
# their published SHA512 before extracting the one unmodified executable.
from pathlib import Path
import os
import threading

AGENT_ROOT = Path(__file__).resolve().parent / "Toolbox" / "subscription-agents"
_AGENT_LOCK = threading.RLock()
SUBSCRIPTION_AGENTS = {
    "claude": {"name": "Claude Code", "version": "2.1.318", "scope": "@anthropic-ai", "package": "claude-code", "member": "package/claude.exe",
        "integrity": {
            "x64": "g/fa2hqcU/+Qd3NMaY/8i+AoX6HQt3zBcj+V/Ot9icr5TN4PmOmuy6YhVNFLHb2IniLIrBA+gZFqonUe0aYLxg==",
            "arm64": "+yn6RI0BeeiG+xbX8OkxSgRAiKIJZhO1foPNG8wUfjIPYaaxYsm8GL3TV7KyuBgGrjPYR1+pFzXaHY2bLsF37g=="}},
    "grok": {"name": "Grok Build", "version": "1.0.70", "scope": "@xai-official", "package": "grok", "member": "package/bin/grok.exe.br",
        "integrity": {
            "x64": "CZLdwfihazTIX/jgSqm3kBQxCeIjHJxMSq8w2VIhLP3I+zcAFYgj7Viy4YJAK8cLt4lhICoRLhGGb8Lq/zYMYw==",
            "arm64": "3W8ZmZ+MoqKfsqjAdm9ldbT7NfiTGOdAIEkOPyzEyyI9/qb4a13B11EL3ffqtiNPbadGWjYvwHsMirYZI8WHzg=="}},
}


def _subscription_spec(provider):
    if provider not in SUBSCRIPTION_AGENTS:
        raise ValueError("Choose Claude or Grok.")
    return SUBSCRIPTION_AGENTS[provider]


def _subscription_binary(provider):
    spec = _subscription_spec(provider)
    return AGENT_ROOT / provider / spec["version"] / (provider + ".exe")


def _agent_preferences(provider):
    _subscription_spec(provider)
    try:
        value = json.loads((AGENT_ROOT / (provider + ".json")).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_agent_preferences(provider, value):
    import secrets
    AGENT_ROOT.mkdir(parents=True, exist_ok=True)
    target = AGENT_ROOT / (provider + ".json")
    temporary = target.with_name(target.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temporary.write_text(json.dumps(value))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def subscription_agent_status(provider):
    """Local non-billing status; never reads credentials or probes inference.

    connected records the last successful vendor login, not a live entitlement
    check. The official CLI owns refresh/logout and may ask to sign in again.
    """
    spec = _subscription_spec(provider)
    installed = _subscription_binary(provider).is_file()
    value = _agent_preferences(provider)
    connected = installed and value.get("login_completed") is True
    return {"name": spec["name"], "installed": installed, "connected": connected,
            "enabled": connected and value.get("enabled") is True,
            "detail": ("Official sign-in completed previously; the agent checks current access when used."
                       if connected else "Sign in with your own subscription in the official agent.")}


def enable_subscription_agent(provider, enabled):
    """Explicit local permission to spend plan allowance, separate from login."""
    if type(enabled) is not bool:
        raise ValueError("Enabled must be true or false.")
    with _AGENT_LOCK:
        if enabled and not subscription_agent_status(provider)["connected"]:
            raise ValueError("Complete the official subscription sign-in first.")
        value = _agent_preferences(provider)
        value["enabled"] = enabled
        _save_agent_preferences(provider, value)
    return subscription_agent_status(provider)


def _subscription_environment():
    """Use the vendor's own saved session; never silently fall back to an API key."""
    denied = {"XAI_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
              "GROK_API_KEY", "GROK_API_BASE_URL", "GROK_BASE_URL", "GROK_MODEL",
              "XAI_BASE_URL", "XAI_API_BASE", "XAI_API_BASE_URL", "OPENAI_API_BASE",
              "CLAUDE_CODE_API_KEY_HELPER_TTL_MS"}
    return {key: value for key, value in os.environ.items()
            if key.upper() not in denied and not key.upper().startswith("ANTHROPIC_")
            and not key.upper().startswith("CLAUDE_CODE_USE_")}


def _agent_process(command, *, cwd, timeout, max_output=1024 * 1024, cancel=None):
    """Bound time/output; terminate only this owned process tree on interruption."""
    import subprocess
    import tempfile
    import psutil
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, cwd=str(cwd), env=_subscription_environment(),
            stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel and cancel(): raise InterruptedError("Subscription task stopped by current execution policy.")
                if time.monotonic() >= deadline or os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > max_output:
                    raise TimeoutError("Official agent exceeded its time or output limit.")
                time.sleep(.1)
            if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > max_output:
                raise ValueError("Official agent returned too much output.")
            output.seek(0); errors.seek(0)
            return process.returncode, output.read(max_output).decode("utf-8", "replace"), errors.read(max_output).decode("utf-8", "replace")
        finally:
            if process.poll() is None:
                try:
                    owner = psutil.Process(process.pid)
                    children = owner.children(recursive=True)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    children = []
                for child in reversed(children):
                    try: child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied): pass
                process.kill(); process.wait(timeout=10)
                psutil.wait_procs(children, timeout=5)


def _install_subscription_agent(provider, on_progress=None):
    """Install an integrity-pinned official Windows binary, without shell scripts."""
    import base64
    import hashlib
    import platform
    import tempfile
    import tarfile
    spec = _subscription_spec(provider)
    if os.name != "nt":
        raise RuntimeError("This connection installer supports Windows only.")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64" if machine in ("amd64", "x86_64") else None
    if arch is None: raise RuntimeError("This official agent does not support this PC architecture.")
    package = spec["package"] + "-win32-" + arch
    url = f"https://registry.npmjs.org/{spec['scope']}/{package}/-/{package}-{spec['version']}.tgz"
    target = _subscription_binary(provider)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agent-install-", dir=target.parent) as temporary:
        folder = Path(temporary); archive = folder / "download.tgz"
        digest = hashlib.sha512(); size = 0; deadline = time.monotonic() + 300
        with urlopen(Request(url), timeout=45) as response, archive.open("wb") as output:
            if response.geturl() != url: raise ValueError("Unexpected official package redirect.")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 400 * 1024 * 1024 or time.monotonic() > deadline:
                    raise ValueError("Official agent download exceeded its size or time limit.")
                digest.update(chunk); output.write(chunk)
                if on_progress: on_progress("Downloading official " + spec["name"] + ".")
        if digest.digest() != base64.b64decode(spec["integrity"][arch]):
            raise ValueError("Official agent package checksum did not match.")
        staged = folder / "agent.exe"
        with tarfile.open(archive, "r:gz") as package_file:
            members = []
            for item in package_file:
                if len(members) >= 32 or item.size > 400 * 1024 * 1024:
                    raise ValueError("Official agent package contents are too large.")
                members.append(item)
            candidates = [item for item in members if item.name == spec["member"]]
            if len(candidates) != 1 or not candidates[0].isfile() or candidates[0].size > 400 * 1024 * 1024:
                raise ValueError("Official agent package has no valid executable.")
            with package_file.extractfile(candidates[0]) as source, staged.open("wb") as output:
                while chunk := source.read(1024 * 1024): output.write(chunk)
        if provider == "grok":
            # Vendor npm bootstrap does the same Brotli decompression. Existing
            # Workbench Node is used only as a decoder, not for install scripts.
            node = Path(__file__).resolve().parent / "Toolbox/node-v26.13.0-win-x64/node.exe"
            if not node.is_file(): raise RuntimeError("Workbench Node tools are still installing. Try again when setup finishes.")
            unpacked = folder / "grok.exe"
            code = "const f=require('fs'),z=require('zlib');f.writeFileSync(process.argv[2],z.brotliDecompressSync(f.readFileSync(process.argv[1]),{maxOutputLength:419430400}));"
            result = _agent_process([str(node), "-e", code, str(staged), str(unpacked)], cwd=folder, timeout=45)
            if result[0] != 0: raise RuntimeError("Official Grok binary could not be unpacked.")
            staged = unpacked
        with staged.open("rb") as source:
            if source.read(2) != b"MZ": raise ValueError("Official agent is not a Windows executable.")
        os.replace(staged, target)


def connect_subscription_agent(provider, on_progress=None):
    """Install on click, then let the unmodified vendor CLI own browser login.

    No key/token is read, copied, returned, or stored by Workbench. Connecting
    never enables background plan use; the UI must request that separately.
    """
    spec = _subscription_spec(provider)
    with _AGENT_LOCK:
        _save_agent_preferences(provider, {"login_completed": False, "enabled": False})
        if not _subscription_binary(provider).is_file():
            if on_progress: on_progress("Installing official " + spec["name"] + ".")
            _install_subscription_agent(provider, on_progress)
        if on_progress: on_progress("Complete sign-in in the official browser window.")
        command = [str(_subscription_binary(provider))] + (["auth", "login"] if provider == "claude" else ["--no-auto-update", "login"])
        code, _, _ = _agent_process(command, cwd=AGENT_ROOT, timeout=600)
        if code != 0: raise RuntimeError("Official sign-in did not finish. Please try connecting again.")
        if provider == "claude":
            code, output, _ = _agent_process([str(_subscription_binary(provider)), "auth", "status"], cwd=AGENT_ROOT, timeout=15)
            try: status = json.loads(output)
            except ValueError: status = {}
            if code != 0 or status.get("authMethod") != "claude.ai":
                raise RuntimeError("Sign in to Claude Code with your Claude subscription, rather than an API account.")
        value = _agent_preferences(provider)
        value.update(login_completed=True, enabled=False)
        _save_agent_preferences(provider, value)
        return subscription_agent_status(provider)


def build_subscription_task_command(provider, folder, prompt):
    """Build a real official-agent task, never a generic subscription-token proxy."""
    _subscription_spec(provider)
    folder = Path(folder).resolve(strict=True)
    if not folder.is_dir(): raise ValueError("Choose an existing project folder.")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 64000:
        raise ValueError("Provide a task of at most 64KB.")
    executable = _subscription_binary(provider)
    if not executable.is_file(): raise ValueError("Connect the official agent first.")
    common = ["-p", prompt, "--output-format", "json", "--max-turns", "8"]
    if provider == "claude":
        # Manual permissions remain active; never bypass permissions or approve
        # shell/filesystem tools merely because a subscription was connected.
        return [str(executable), *common, "--permission-mode", "default"]
    return [str(executable), "--no-auto-update", *common, "--no-subagents"]


def run_subscription_task(provider, folder, prompt, timeout=300):
    """Run an explicitly enabled coding agent; result is not host verification."""
    import pc_offline
    import routing_mode
    import usage_history
    import cloud_ai
    def blocked():
        return (pc_offline.offline() or routing_mode.load_scope(cloud_ai.STATE) in ("local_only", "free_cloud_only")
                or cloud_ai.task_route(usage_history.CURRENT_TASK.get()) == "local_only")
    if blocked():
        raise ValueError("Subscription agents are disabled by the current offline or provider policy.")
    if type(timeout) not in (int, float) or not 1 <= timeout <= 600:
        raise ValueError("Choose a time limit between 1 and 600 seconds.")
    if not subscription_agent_status(provider)["enabled"]:
        raise ValueError("Enable use of your subscription before handing off a task.")
    command = build_subscription_task_command(provider, folder, prompt)
    started = time.monotonic()
    code, output, _ = _agent_process(command, cwd=folder, timeout=timeout,
        cancel=lambda: blocked() or not subscription_agent_status(provider)["enabled"])
    if code != 0: raise RuntimeError("Official agent could not finish this task; sign-in, plan limits, or permissions may need attention.")
    try: result = json.loads(output)
    except ValueError: raise ValueError("Official agent did not return its documented JSON result.") from None
    if not isinstance(result, dict): raise ValueError("Official agent returned an invalid task result.")
    # Record only reported counts, never estimated plan quota or inferred cost.
    # Claude cache read/write input is separate from its uncached input count.
    usage = _subscription_usage(provider, result)
    if usage is not None:
        usage_history.record_completion({"model": result.get("model", SUBSCRIPTION_AGENTS[provider]["name"]),
                                         "usage": usage}, provider + "_subscription", time.monotonic() - started)
    return {"provider": provider, "status": "agent_error" if result.get("is_error") else "finished",
            "exit_code": code, "output": result, "errors": [], "usage": usage,
            "verification": "agent_report_only"}


def _subscription_usage(provider, result):
    """Normalize actual CLI usage; absent/unsupported/invalid counts stay unknown."""
    def count(value):
        return value if type(value) is int and 0 <= value <= 1_000_000_000 else None
    raw = result.get("usage")
    if provider == "claude":
        models = result.get("modelUsage")
        if isinstance(models, dict) and models:
            if len(models) > 32 or not all(isinstance(v, dict) for v in models.values()): return None
            rows = list(models.values()); fields = ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens")
        elif isinstance(raw, dict):
            rows = [raw]; fields = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
        else: return None
        if any(count(row.get(fields[0])) is None or count(row.get(fields[3])) is None
               or any(count(row.get(key, 0)) is None for key in fields[1:3]) for row in rows): return None
        prompt = sum(sum(row.get(key, 0) for key in fields[:3]) for row in rows)
        completion = sum(row[fields[3]] for row in rows)
    elif isinstance(raw, dict):
        prompt = count(raw.get("prompt_tokens", raw.get("input_tokens")))
        completion = count(raw.get("completion_tokens", raw.get("output_tokens")))
        if prompt is None or completion is None: return None
    else: return None
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


# Limits are deliberately conservative client-side budgets, not claims about
# each account's actual quota. Provider dashboards remain authoritative.
PROVIDERS = {
    "google": {"name": "Google AI Studio", "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "model": "gemini-3.8-flash", "context": 1000000, "rpm": 5, "daily": 100, "speed": 140, "vision": True, "signup": "https://aistudio.google.com/api-keys"},
    "groq": {"name": "Groq", "url": "https://api.groq.com/openai/v1/chat/completions", "model": "openai/gpt-oss-20b", "context": 131072, "rpm": 10, "daily": 200, "speed": 900, "signup": "https://console.groq.com/keys"},
    "nvidia": {"name": "NVIDIA NIM", "url": "https://integrate.api.nvidia.com/v1/chat/completions", "model": "meta/llama-3.3-70b-instruct", "context": 65536, "rpm": 5, "daily": 100, "speed": 120, "signup": "https://build.nvidia.com/"},
    # Cerebras now grants a one-time $5 trial, not recurring free inference.
    # Keep the entry so existing users can disconnect a previously saved key.
    "cerebras": {"name": "Cerebras (trial only)", "url": "https://api.cerebras.ai/v1/chat/completions", "model": "qwen-3.8-27b", "context": 8192, "rpm": 10, "daily": 0, "speed": 1200, "free_eligible": False, "signup": "https://cloud.cerebras.ai/"},
    "mistral": {"name": "Mistral AI", "url": "https://api.mistral.ai/v1/chat/completions", "model": "mistral-small-latest", "context": 32000, "rpm": 5, "daily": 100, "speed": 100, "vision": True, "signup": "https://console.mistral.ai/"},
    # The no-payment-method tier allows 20 requests/day for this model.
    "sambanova": {"name": "SambaNova Cloud", "url": "https://api.sambanova.ai/v1/chat/completions", "model": "Meta-Llama-3.3-70B-Instruct", "context": 32000, "rpm": 10, "daily": 18, "speed": 400, "signup": "https://cloud.sambanova.ai/apis"},
    # BigModel lists this exact Flash model as free; FlashX is paid.
    "bigmodel": {"name": "Zhipu AI (BigModel)", "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions", "model": "glm-4.7-flash", "context": 200000, "rpm": 5, "daily": 30, "speed": 150, "signup": "https://bigmodel.cn/usercenter/proj-mgmt/apikeys"},
    # A registered ModelScope account receives 200 free Magicubes each day.
    # This model's card lists one Magicube per API-Inference request; reserve
    # room for the user's other ModelScope activity.
    "modelscope": {"name": "ModelScope", "url": "https://api-inference.modelscope.cn/v1/chat/completions", "model": "Qwen/Qwen3.5-35B-A3B", "context": 131072, "rpm": 5, "daily": 150, "speed": 120, "vision": True, "signup": "https://modelscope.cn/my/myaccesstoken"},
    # Aion's free account has a 20K-token daily limit. Reserve at most half
    # of that here, because other use and provider-side accounting are unknown.
    "aion": {"name": "Aion Labs", "url": "https://api.aionlabs.ai/v1/chat/completions", "model": "aion-labs/aion-3.5-mini", "context": 262144, "rpm": 5, "daily": 8, "daily_tokens": 10000, "speed": 80, "max_tokens": 2048, "signup": "https://www.aionlabs.ai/"},
    # Merkium documents permanent free API use and OpenAI-format tool calls.
    # Its 2-second spacing is satisfied by this more conservative 5 RPM cap.
    "merkium": {"name": "Merkium", "url": "https://chat.merkium.nl/api/v1/chat/completions", "model": "ganymede-1.1", "context": 1000000, "rpm": 5, "daily": 40, "speed": 80, "max_tokens": 4096, "signup": "https://chat.merkium.nl/"},
    # Cohere documents Command A+ as free until its trial/production limits.
    "cohere": {"name": "Cohere", "url": "https://api.cohere.com/v2/chat", "model": "command-a-plus-05-2026", "context": 128000, "rpm": 5, "daily": 30, "speed": 80, "vision": True, "signup": "https://dashboard.cohere.com/api-keys"},
    "huggingface": {"name": "Hugging Face", "url": "https://router.huggingface.co/v1/chat/completions", "model": "openai/gpt-oss-120b:fastest", "context": 8192, "rpm": 2, "daily": 2, "monthly": 2, "speed": 150, "max_tokens": 1024, "signup": "https://huggingface.co/settings/tokens"},
    "cloudflare": {"name": "Cloudflare Workers AI", "url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions", "model": "@cf/openai/gpt-oss-20b", "context": 128000, "rpm": 5, "daily": 12, "speed": 160, "max_tokens": 2048, "daily_neurons": 8500, "input_neurons_per_m": 18182, "output_neurons_per_m": 27273, "signup": "https://dash.cloudflare.com/?to=/:account/ai/workers-ai"},
}


def likely_faster(size: str, speed: float, local_speed: float) -> bool:
    """Account for cloud round-trip latency before diverting a local request."""
    expected_output = {"quick": 100, "balanced": 1000, "deep": 3000}.get(size, 1000)
    return 2.0 + expected_output / speed < expected_output / max(local_speed, 1)


def estimated_neurons(provider: str, payload: dict) -> int:
    """Reserve an upper-bound request cost before using a metered free tier."""
    spec = PROVIDERS[provider]
    if not spec.get("daily_neurons"):
        return 0
    body = request_body(provider, payload)
    input_bytes = len(json.dumps({"messages": body.get("messages", []),
                                  "tools": body.get("tools", [])},
                                 ensure_ascii=True).encode("utf-8"))
    # BPE tokens normally cover more than one byte. Counting every serialized
    # byte as a token, then adding 25%, guards against chat framing overhead.
    raw = (input_bytes * spec["input_neurons_per_m"] +
           body["max_tokens"] * spec["output_neurons_per_m"]) / 1_000_000
    return max(1, int(raw * 1.25) + 1)


def estimated_tokens(provider: str, payload: dict) -> int:
    """Reserve a conservative upper bound for token-limited free accounts."""
    if not PROVIDERS[provider].get("daily_tokens"):
        return 0
    body = request_body(provider, payload)
    prompt_bytes = len(json.dumps({"messages": body.get("messages", []),
                                   "tools": body.get("tools", [])},
                                  ensure_ascii=True).encode("utf-8"))
    return max(1, int(prompt_bytes * 1.25) + body["max_tokens"] + 1)


def request_body(provider: str, payload: dict) -> dict:
    """Copy only safe OpenAI chat fields; translate Cohere's v2 wire shape."""
    spec = PROVIDERS[provider]
    allowed = ("messages", "tools", "tool_choice", "temperature", "top_p", "stop", "seed", "response_format", "parallel_tool_calls")
    body = {key: payload[key] for key in allowed if key in payload}
    body.update(model=spec["model"], stream=False,
                max_tokens=min(spec.get("max_tokens", 8192), int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 4096)))
    if provider == "cohere":
        body.pop("seed", None)
        body.pop("parallel_tool_calls", None)
        body.pop("response_format", None)
        body["max_tokens"] = min(body["max_tokens"], 4096)
    if provider == "aion":
        # The documented chat endpoint accepts tools but not these optional
        # OpenAI parameters. Lower reasoning leaves room for visible output.
        for field in ("tool_choice", "top_p", "seed", "response_format", "parallel_tool_calls"):
            body.pop(field, None)
        body["reasoning_effort"] = "low"
    if provider == "merkium":
        # Only send parameters its chat-completions documentation lists.
        for field in ("seed", "response_format", "parallel_tool_calls"):
            body.pop(field, None)
    return body


def _cohere_completion(data: dict) -> dict:
    """Normalize Cohere text and tool calls into the worker's chat contract."""
    message = data.get("message") or {}
    content = "".join(part.get("text", "") for part in message.get("content", []) if isinstance(part, dict))
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        calls.append({"id": call.get("id"), "type": "function", "function": {
            "name": function.get("name"), "arguments": function.get("arguments", "{}")}})
    usage = data.get("usage") or {}
    billed = usage.get("billed_units") or usage.get("tokens") or {}
    return {"id": data.get("id", ""), "model": data.get("model", "command-a-plus-05-2026"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content or None,
                                               "tool_calls": calls or None}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": billed.get("input_tokens", 0),
                      "completion_tokens": billed.get("output_tokens", 0)}}


def complete(provider: str, key: str, payload: dict, timeout: int = 90,
             account_id: str = "") -> tuple[dict, float]:
    """Call one fixed provider endpoint and return its answer and elapsed time."""
    spec = PROVIDERS[provider]
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    if provider == "google":
        headers["x-goog-api-client"] = "cale-ai-workbench/0.5"
    url = spec["url"]
    if provider == "cloudflare":
        if not re.fullmatch(r"[a-fA-F0-9]{32}", account_id):
            raise ValueError("Cloudflare account ID must be 32 hexadecimal characters.")
        url = url.format(account_id=account_id)
    request = Request(url, data=json.dumps(request_body(provider, payload)).encode(), headers=headers)
    started = time.monotonic()
    with urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    if provider == "cohere":
        result = _cohere_completion(result)
    return result, time.monotonic() - started
