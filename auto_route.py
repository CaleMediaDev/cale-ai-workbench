"""Cheap, deterministic task sizing for the local model router.

This does not consume a second AI request. The original user task, saved board
brief and tool history remain in the request, so a long project stays on a
stronger model after ordinary context compression and worker restarts.
"""

import re

BUILD_ACTION = re.compile(
    r"\b(build|create|develop|implement|code|refactor|debug|fix|repair|"
    r"redesign|migrate|integrate|train|automate|audit|reverse.engineer|"
    r"add|improve|upgrade|install|connect|set\s+up|update|change|remove)\b",
    re.IGNORECASE,
)
PROJECT_MARKER = re.compile(
    r"USER PROJECT:|persistent local project|\.local-ai/(?:PLAN|PROGRESS)\.md",
    re.IGNORECASE,
)
PROJECT_REQUEST = re.compile(
    r"REQUEST AND ACCEPTANCE CRITERIA:\s*\n(.*?)\n(?:END OF ORIGINAL REQUEST|\s*This is a persistent local project\.)",
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
TOOL_WORK = re.compile(
    r"\b(?:read|open|inspect|analy[sz]e|summari[sz]e|extract|transcribe|"
    r"index|edit|search|browse|visit|scrape|crawl|generate|draw|make|create)\b"
    r".{0,90}\b(?:attached|uploaded|selected|my\s+(?:file|folder|document|"
    r"image|photo|video|audio|data)|this\s+(?:file|folder|document|pdf|"
    r"image|photo|video|audio|website|page|repo)|"
    r"(?:pdf|csv|spreadsheet|screenshot|audio|video|image|photo|"
    r"website|webpage|browser|url|repository|repo|file|folder))\b",
    re.IGNORECASE | re.DOTALL,
)
EXPLICIT_TOOL = re.compile(
    r"\b(?:use\s+(?:your\s+)?tools|search\s+(?:the\s+)?web|"
    r"look\s+it\s+up|browse\s+(?:the\s+)?internet)\b",
    re.IGNORECASE,
)
PROJECT_BUILD = re.compile(
    r"\b(?:build|create|develop|implement|code|redesign|make)\b"
    r".{0,80}\b(?:app|application|website|software|program|extension|"
    r"plugin|dashboard)\b",
    re.IGNORECASE | re.DOTALL,
)


def has_media_input(messages):
    """Detect image/audio/video parts that are not represented by text."""
    return any(
        isinstance(part, dict)
        and part.get("type") not in (None, "text", "input_text")
        for msg in messages
        if isinstance(msg, dict) and msg.get("role") == "user"
        for part in (msg.get("content") if isinstance(msg.get("content"), list) else [])
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
    if PROJECT_BUILD.search(intent):
        return "deep"
    live_lookup = LIVE_FACT.search(intent) or (
        CONTACT_NUMBER.search(intent) and LOOKUP_WORD.search(intent)
    )
    latest = intent.strip()
    if (
        len(intent) <= 280
        and tool_calls <= 2
        and QUESTION_START.match(latest)
        and not live_lookup
        and not has_media_input(messages)
        and not EXPLICIT_TOOL.search(intent)
    ):
        return "quick"
    if has_media_input(messages) or TOOL_WORK.search(intent) or EXPLICIT_TOOL.search(intent):
        return "balanced"
    if BUILD_ACTION.search(intent):
        return "deep"
    if live_lookup:
        return "balanced"
    # One or two tool calls can be routine discovery or a retry; they do not
    # turn a short request into a large project by themselves.
    if len(intent) <= 280 and tool_calls <= 2:
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
