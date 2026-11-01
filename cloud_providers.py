"""Small, explicit adapters for optional user-owned cloud inference accounts.

The marketing homepages are not API bases. Keep endpoints and model IDs here,
and never accept a model or URL supplied by an untrusted task prompt.
"""

from __future__ import annotations

import json
import re
import time
from urllib.request import Request, urlopen


# Limits are deliberately conservative client-side budgets, not claims about
# each account's actual quota. Provider dashboards remain authoritative.
PROVIDERS = {
    "google": {"name": "Google AI Studio", "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "model": "gemini-3.8-flash", "context": 1000000, "rpm": 5, "daily": 100, "speed": 140, "signup": "https://aistudio.google.com/api-keys"},
    "groq": {"name": "Groq", "url": "https://api.groq.com/openai/v1/chat/completions", "model": "openai/gpt-oss-20b", "context": 131072, "rpm": 10, "daily": 200, "speed": 900, "signup": "https://console.groq.com/keys"},
    "nvidia": {"name": "NVIDIA NIM", "url": "https://integrate.api.nvidia.com/v1/chat/completions", "model": "meta/llama-3.3-70b-instruct", "context": 65536, "rpm": 5, "daily": 100, "speed": 120, "signup": "https://build.nvidia.com/"},
    "cerebras": {"name": "Cerebras", "url": "https://api.cerebras.ai/v1/chat/completions", "model": "qwen-3.8-27b", "context": 8192, "rpm": 10, "daily": 200, "speed": 1200, "signup": "https://cloud.cerebras.ai/"},
    "mistral": {"name": "Mistral AI", "url": "https://api.mistral.ai/v1/chat/completions", "model": "mistral-small-latest", "context": 32000, "rpm": 5, "daily": 100, "speed": 100, "signup": "https://console.mistral.ai/"},
    # The no-payment-method tier allows 20 requests/day for this model.
    "sambanova": {"name": "SambaNova Cloud", "url": "https://api.sambanova.ai/v1/chat/completions", "model": "Meta-Llama-3.3-70B-Instruct", "context": 32000, "rpm": 10, "daily": 18, "speed": 400, "signup": "https://cloud.sambanova.ai/apis"},
    # BigModel lists this exact Flash model as free; FlashX is paid.
    "bigmodel": {"name": "Zhipu AI (BigModel)", "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions", "model": "glm-4.7-flash", "context": 200000, "rpm": 5, "daily": 30, "speed": 150, "signup": "https://bigmodel.cn/usercenter/proj-mgmt/apikeys"},
    # A registered ModelScope account receives 200 free Magicubes each day.
    # This model's card lists one Magicube per API-Inference request; reserve
    # room for the user's other ModelScope activity.
    "modelscope": {"name": "ModelScope", "url": "https://api-inference.modelscope.cn/v1/chat/completions", "model": "Qwen/Qwen3.5-35B-A3B", "context": 131072, "rpm": 5, "daily": 150, "speed": 120, "signup": "https://modelscope.cn/my/myaccesstoken"},
    "cohere": {"name": "Cohere", "url": "https://api.cohere.com/v2/chat", "model": "command-r-08-2024", "context": 128000, "rpm": 5, "daily": 30, "speed": 80, "signup": "https://dashboard.cohere.com/api-keys"},
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
    return {"id": data.get("id", ""), "model": data.get("model", "command-r-08-2024"),
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
