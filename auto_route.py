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
PROJECT_REQUEST = re.compile(
    r"REQUEST AND ACCEPTANCE CRITERIA:\s*\n(.*?)\n\s*This is a persistent local project\.",
    re.IGNORECASE | re.DOTALL,
)
QUESTION_START = re.compile(
    r"^(what|why|how|when|where|who|which|is|are|does|do|tell me|explain)\b",
    re.IGNORECASE,
)
LIVE_FACT = re.compile(
    r"\b(?:phone\s+number|contact\s+(?:info|information|details)|"
    r"business\s+hours|opening\s+hours|street\s+address|"
    r"current|latest|today|right\s+now|weather|stock\s+price|"
    r"sports?\s+score)\b",
    re.IGNORECASE,
)
CONTACT_NUMBER = re.compile(r"\b(?:phone|number|contact|address)\b", re.IGNORECASE)
LOOKUP_WORD = re.compile(
    r"\b(?:find|lookup|look\s+up|search|what(?:'?s| is)?|where(?:'?s| is)?)\b",
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
    project = PROJECT_REQUEST.search(request)
    intent = project.group(1).strip() if project else request
    tool_calls = sum(
        len(msg.get("tool_calls") or [])
        for msg in messages
        if isinstance(msg, dict) and msg.get("role") == "assistant"
    )
    if PROJECT_MARKER.search(request) and not project:
        return "deep"
    if len(intent) > 800 or tool_calls >= 4:
        return "deep"
    live_lookup = LIVE_FACT.search(intent) or (
        CONTACT_NUMBER.search(intent) and LOOKUP_WORD.search(intent)
    )
    latest = intent.strip()
    if (
        len(intent) <= 280
        and tool_calls == 0
        and QUESTION_START.match(latest)
        and not live_lookup
    ):
        return "quick"
    if BUILD_ACTION.search(intent):
        return "deep"
    if live_lookup:
        return "balanced"
    if len(intent) <= 280 and tool_calls == 0:
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
