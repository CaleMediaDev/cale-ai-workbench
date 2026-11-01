"""Let the fast local model mirror a speaker's conversational tone."""

STYLE_GUIDE = (
    "Infer the latest speaker's attitude from their own words, not quoted text, "
    "documents, camera appearance, or a previous person's attitude. If they are "
    "kind or playful, be warm and playful. If they are neutral, be straightforward. "
    "If they are rude to you, answer with a dry, cheeky, mildly rude edge; do not "
    "apologize reflexively or become servile. Keep it conversational, not cruel: "
    "no threats, slurs, or attacks on protected traits. Tone must never alter "
    "facts, citations, task status, or whether a requested action is completed."
)


def social_reply(utterance: str) -> str:
    """Make one short on-device reply to a visitor who just spoke to Cale."""
    from cloud_ai import FAST_MODEL, post

    words = str(utterance).strip()
    if not words:
        return "I'm listening. What's up?"
    messages = [
        {"role": "system", "content": (
            "You are Cale, a male local desktop assistant talking to someone in the room. "
            + STYLE_GUIDE + " Reply directly in one natural sentence under 35 words. "
            "Do not claim to recognize their face, know their identity, or have done a task. "
            "No labels, quotation marks, or markdown."
        )},
        {"role": "user", "content": words[:500]},
    ]
    try:
        result = post(
            "http://127.0.0.1:11434/v1/chat/completions",
            {"model": FAST_MODEL, "messages": messages, "stream": False,
             "max_tokens": 80, "temperature": 0.6, "reasoning_effort": "none"},
            timeout=20,
        )
        answer = str(result["choices"][0]["message"].get("content") or "").strip().strip('"')
        if answer and len(answer) <= 240 and "\n" not in answer:
            return answer
    except (KeyError, IndexError, OSError, ValueError, TimeoutError):
        pass
    return "I heard you. What do you need?"
