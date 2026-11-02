"""Cheap, deterministic task sizing for the local model router.

This does not consume a second AI request. The original user task, saved board
brief and tool history remain in the request, so a long project can scale up
without forcing every routine tool step through the slowest model.
"""

import json
import re

from chat_assistant import bare_song_brief

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
REVIEW_WORKER = re.compile(r"(?im)^# Kanban task [^\r\n]+\s[—-]\sreview\s*$")
PROJECT_REQUEST = re.compile(
    r"REQUEST AND ACCEPTANCE CRITERIA:\s*\n(.*?)\n(?:END OF ORIGINAL REQUEST|\s*This is a persistent local project\.)",
    re.IGNORECASE | re.DOTALL,
)
RECENT_FOLLOWUPS = re.compile(
    r"Recent user follow-ups \(read the full file\):\s*\n"
    r"(.*?)(?=\n(?:Relevant local memory|Previous supervisor result):|\Z)",
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
SIMPLE_MUSIC = re.compile(
    r"\b(?:make|generate|create)\b.{0,100}\b(?:song|instrumental|jingle|music|beat|soundtrack)\b",
    re.IGNORECASE | re.DOTALL,
)
MUSIC_VIDEO = re.compile(r"\bmusic\s+video\b", re.IGNORECASE)
COMPLEX_PROJECT = re.compile(
    r"\b(?:multi[- ](?:page|feature|module|agent)|enterprise|production[- ]ready|"
    r"full[- ]stack|distributed|migration|security audit|large[- ]scale)\b",
    re.IGNORECASE,
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


def synthetic_worker_note(text):
    """Hermes board reminders are protocol messages, not new user scope."""
    return text.lstrip().startswith("[System: You are a Hermes kanban worker.")


def repeated_tool_failures(messages):
    """Escalate while the latest tool results show an unresolved repair loop."""
    recent = [message_text(msg.get("content")) for msg in messages
              if isinstance(msg, dict) and msg.get("role") == "tool"][-8:]
    failures = 0
    for content in reversed(recent):
        if "[Tool loop warning:" in content:
            failures += 1
            continue
        try:
            result, _ = json.JSONDecoder().raw_decode(content.lstrip())
        except (ValueError, TypeError):
            break
        if isinstance(result, dict) and (result.get("error") or
                                         result.get("isError") is True or
                                         result.get("success") is False or
                                         result.get("status") == "blocked" or
                                         isinstance(result.get("exit_code"), int)
                                         and result["exit_code"] != 0):
            failures += 1
        else:
            break
    return failures >= 3


def repair_messages(messages):
    """Nudge an escalated model to diagnose errors instead of replaying a failed step."""
    if not repeated_tool_failures(messages):
        return messages
    hint = {
        "role": "system",
        "content": (
            "Several recent tool actions failed. Inspect their exact errors before "
            "calling another tool. Do not repeat the same command or arguments "
            "unchanged. Check the path, tool schema, and prerequisites; then try "
            "a different repair. If the blocker cannot be resolved, record the "
            "specific problem and ask for the one missing input."
        ),
    }
    return [hint, *messages]


def task_size(messages):
    """Return quick, balanced, or deep from user intent and actual tool history."""
    users = [
        message_text(msg.get("content"))
        for msg in messages
        if isinstance(msg, dict) and msg.get("role") == "user"
    ]
    # A long-lived worker can accumulate enough saved guidance that its
    # original brief drops out of the tail. Inspect both ends without making
    # the model tier depend on the length of the worker wrapper itself.
    all_user_text = "\n".join(users)
    request = all_user_text[-16000:]
    beginning = all_user_text[:16000]
    project = PROJECT_REQUEST.search(beginning) or PROJECT_REQUEST.search(request)
    if project:
        original = project.group(1).strip()
        # The latest saved user follow-ups can turn a small original task into
        # substantial new work. They are part of the worker prompt but live
        # after END OF ORIGINAL REQUEST, outside PROJECT_REQUEST's capture.
        followups = RECENT_FOLLOWUPS.search(request)
        intent = original + ("\n" + followups.group(1).strip() if followups else "")
        latest_real_user = next(
            (item.strip() for item in reversed(users[1:]) if not synthetic_worker_note(item)),
            "",
        )
        if latest_real_user:
            intent += "\n" + latest_real_user
    else:
        intent = request
    tool_calls = sum(
        len(msg.get("tool_calls") or [])
        for msg in messages
        if isinstance(msg, dict) and msg.get("role") == "assistant"
    )
    if repeated_tool_failures(messages):
        return "deep"
    # The first independent review turn must reason about the saved result,
    # even when the original request looked like a quick media generation.
    review_worker = bool(REVIEW_WORKER.search(beginning))
    if review_worker and tool_calls == 0:
        return "deep"
    if review_worker and SIMPLE_MUSIC.search(intent):
        return "balanced"
    # A detailed song brief describes the output, not a software project.
    # Keep the local worker on the middle model for rendering and verification;
    # the largest model can take minutes just to answer a routine tool turn.
    if (
        (SIMPLE_MUSIC.search(intent) or bare_song_brief(intent))
        and not PROJECT_BUILD.search(intent)
        and not COMPLEX_PROJECT.search(intent)
        and not MUSIC_VIDEO.search(intent)
    ):
        # A resumed song with saved artifacts is review/repair work. The fast
        # model can render a fresh simple jingle, but it repeatedly fumbles
        # Windows file paths and verification commands in this phase.
        if project and "Existing project files (inspect and verify before regenerating):" in all_user_text:
            return "balanced"
        if len(intent) > 280:
            return "balanced"
        return "quick" if tool_calls <= 3 else "balanced"
    # Hermes may resume a board card with a condensed task body instead of
    # the original PROJECT_REQUEST block. Keep recognizable standalone song
    # work on its media tier; be conservative for other unparsed projects.
    if (PROJECT_MARKER.search(beginning) or PROJECT_MARKER.search(request)) and not project:
        return "deep"
    if len(intent) > 800 or COMPLEX_PROJECT.search(intent):
        return "deep"
    if PROJECT_BUILD.search(intent):
        # A short app request benefits from one strong planning turn. Keeping
        # the 70B+ model on every file read and edit made local projects crawl.
        return "deep" if tool_calls == 0 or len(intent) > 280 else "balanced"
    if tool_calls >= 4:
        return "deep" if len(intent) > 280 else "balanced"
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
        return "deep" if tool_calls == 0 or len(intent) > 280 else "balanced"
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
            ready,
            key=lambda name: (
                abs(ready[name] - 35) > 5,
                name != balanced,
                abs(ready[name] - 35),
            ),
        )
    ordered = []
    for name in preference:
        if name not in ordered and name in ready:
            ordered.append(name)
    return ordered
