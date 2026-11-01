"""Small, local-first conversation router for the workbench's main screen."""

from __future__ import annotations

import re

TASK_VERBS = (
    "build", "make", "create", "develop", "design", "write", "research",
    "investigate", "fix", "repair", "update", "improve", "train", "analyze",
    "analyse", "generate", "implement", "set up", "install",
    "add", "change",
)


def route(text: str, active_tasks: list[dict]) -> str:
    """Keep ordinary questions in chat and send explicit work to the task queue."""
    value = text.strip().lower()
    if not value:
        return "empty"
    if re.search(r"\b(?:what(?:'s| is) the status|how(?:'s| is) (?:it|the task) going|what are you working on)\b", value):
        return "status"
    if active_tasks and re.match(r"^(?:also|actually|change it|add to (?:it|that)|tell (?:it|the ai) to|instead)\b", value):
        return "note"
    command = re.sub(r"^(?:please\s+|can you\s+|could you\s+|i want you to\s+|i need you to\s+)", "", value)
    if active_tasks and command.startswith(("add ", "change ")):
        return "note"
    if re.match(r"^i (?:want|need) (?:a|an|some) (?:website|app|program|logo|design|image|report|plan|tool)\b", value):
        return "task"
    if command.startswith(TASK_VERBS):
        return "task"
    return "answer"


def business_contact_query(text: str) -> str | None:
    """Recognize a common live lookup without sending the whole chat to a model."""
    value = text.strip().rstrip("? .")
    patterns = (
        r"^(?:what(?:'s| is|s)\s+)?(.+?)(?:'s)?\s+(?:phone\s+)?number$",
        r"^(?:find|look up|give me)\s+(?:the\s+)?(?:phone\s+)?number\s+(?:for|of)\s+(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, value, re.IGNORECASE)
        if match:
            name = match.group(1).strip().rstrip("'s")
            return name if len(name) >= 2 else None
    return None


def answer_with_tools(question: str, history: list[dict]) -> str:
    """Use a live source for contact lookups, then local inference for other chat."""
    name = business_contact_query(question)
    if name:
        from web_tools import find_business_contact

        result = find_business_contact(name)
        sources = result.get("sources", [])
        if not sources:
            return "I couldn't find a live page listing a phone number for " + name + "."
        source = sources[0]
        phone = source["phone_links"][0][4:]
        return (
            f"{source['page_title']} lists {phone}. Source: {source['source_url']} "
            "(checked live; I haven't independently confirmed who operates the site)."
        )
    return local_answer(question, history)


def local_answer(question: str, history: list[dict]) -> str:
    """Answer quick chat locally; this path never spends connected cloud credits."""
    from cloud_ai import FAST_MODEL, LOCAL_MODEL, post

    messages = [{
        "role": "system",
        "content": (
            "You are Cale, a concise desktop assistant. Answer the user's question "
            "plainly. Do not claim to have searched the web or changed files. "
            "For current facts you cannot verify, say what is uncertain. "
            "If the user asks you to do lasting work, tell them you can start it "
            "from this chat."
        ),
    }]
    messages.extend(
        {"role": item["role"], "content": item["content"]}
        for item in history[-10:]
        if item["role"] in ("user", "assistant")
    )
    messages.append({"role": "user", "content": question})
    errors = []
    for model in dict.fromkeys((FAST_MODEL, LOCAL_MODEL)):
        try:
            result = post(
                "http://127.0.0.1:11434/v1/chat/completions",
                {"model": model, "messages": messages, "stream": False,
                 "max_tokens": 450, "temperature": 0.3},
                timeout=120,
            )
            answer = result["choices"][0]["message"].get("content", "").strip()
            if answer:
                return answer
        except (KeyError, IndexError, OSError, ValueError) as exc:
            errors.append(str(exc))
    raise RuntimeError("Local chat is still getting ready. Try again after the model finishes installing.")
