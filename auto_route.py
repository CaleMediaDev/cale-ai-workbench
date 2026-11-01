"""Cheap, deterministic task sizing for the local model router.

This does not consume a second AI request. The original user task, saved board
brief and tool history remain in the request, so a long project stays on a
stronger model after ordinary context compression and worker restarts.
"""

import re

BUILD_ACTION = re.compile(
    r"\b(build|create|develop|implement|code|refactor|debug|fix|repair|"
    r"redesign|migrate|integrate|train|automate|audit|reverse.engineer)\b",
    re.IGNORECASE,
)
PROJECT_MARKER = re.compile(
    r"USER PROJECT:|persistent local project|\.local-ai/(?:PLAN|PROGRESS)\.md",
    re.IGNORECASE,
)
QUESTION_START = re.compile(
    r"^(what|why|how|when|where|who|which|is|are|does|do|tell me|explain)\b",
    re.IGNORECASE,
)


def message_text(content):
    """Extract text from OpenAI string or multipart message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def task_size(messages):
    """Return quick, balanced, or deep from user intent and actual tool history."""
    users = [
        message_text(msg.get("content"))
        for msg in messages
        if isinstance(msg, dict) and msg.get("role") == "user"
    ]
    request = "\n".join(users)[-16000:]
    tool_calls = sum(
        len(msg.get("tool_calls") or [])
        for msg in messages
        if isinstance(msg, dict) and msg.get("role") == "assistant"
    )
    if PROJECT_MARKER.search(request):
        return "deep"
    latest = users[-1].strip() if users else ""
    if len(request) <= 280 and tool_calls == 0 and QUESTION_START.match(latest):
        return "quick"
    if BUILD_ACTION.search(request):
        return "deep"
    if len(request) > 800 or tool_calls >= 4:
        return "deep"
    if len(request) <= 280 and tool_calls == 0:
        return "quick"
    return "balanced"


def model_order(size, available, main, fast, balanced):
    """Rank every installed tool-capable alias; the main model is a fallback."""
    ready = {name: parameters for name, parameters in available.items() if parameters}
    if size == "quick":
        preference = sorted(ready, key=lambda name: (ready[name], name != fast))
    elif size == "deep":
        preference = sorted(ready, key=lambda name: ready[name], reverse=True)
    else:
        preference = sorted(
            ready, key=lambda name: (abs(ready[name] - 35), name != balanced)
        )
    ordered = []
    for name in [*preference, main]:
        if name not in ordered and (name in ready or name == main):
            ordered.append(name)
    return ordered or [main]
