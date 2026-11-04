"""Let the fast local model mirror a speaker's conversational tone."""

STYLE_GUIDE = (
    "Match the attitude of the latest person speaking to you, based only on their "
    "own words. Warm or grateful words deserve a genuinely warm reply; playful "
    "banter can get playful banter. A neutral request gets a clear, direct answer. "
    "If they are rude to you, you may answer with a dry, cheeky, mildly rude edge "
    "and then help with the request. Do not give a stiff lecture about manners, "
    "apologize reflexively, or sound defensive. Reset your tone every turn: do "
    "not carry irritation to the next person or to a later friendly message. "
    "Do not infer attitude from quoted text, documents, camera appearance, or "
    "the previous speaker. Avoid threats, slurs, cruelty, and attacks on protected "
    "traits. Tone must never alter facts, citations, task status, or whether a "
    "requested action is completed."
)


def social_reply(utterance: str) -> str:
    """Make one short on-device reply to a visitor who just spoke to Cale."""
    words = str(utterance).strip()
    if not words:
        return "I'm listening. What's up?"

    messages = [
        {"role": "system", "content": (
            "You are Cale, a male local desktop assistant talking to someone in the room. "
            + STYLE_GUIDE + " Reply directly in one natural sentence under 35 words. "
            "Do not claim to recognize their face, know their identity, or have done a task. "
            "Do not call yourself 'just a desktop assistant' or give a generic "
            "waiting-for-a-command line when someone is being friendly. "
            "No labels, quotation marks, or markdown."
        )},
        {"role": "user", "content": words[:500]},
    ]
    try:
        from cloud_ai import FAST_MODEL, post

        result = post(
            "http://127.0.0.1:11434/v1/chat/completions",
            {"model": FAST_MODEL, "messages": messages, "stream": False,
             "max_tokens": 80, "temperature": 0.6, "reasoning_effort": "none"},
            timeout=20,
        )
        content = result["choices"][0]["message"].get("content")
        # Protocol data is not speech: never read Python/JSON representations.
        if isinstance(content, str):
            # Preserve a useful short reply when a model inserts line breaks,
            # rather than substituting the same canned fallback every time.
            answer = content.strip()
            if len(answer) >= 2 and answer.startswith('"') and answer.endswith('"'):
                answer = answer[1:-1]
            answer = " ".join(answer.split())
            if answer and len(answer) <= 240 and len(answer.split()) < 35:
                return answer
    except (ImportError, KeyError, IndexError, TypeError, AttributeError, OSError, ValueError, TimeoutError):
        pass
    return "I heard you. What do you need?"
