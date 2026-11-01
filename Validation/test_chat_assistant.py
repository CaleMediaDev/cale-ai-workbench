"""The chat should answer questions and create durable tasks only for work."""

import chat_assistant


def test_questions_stay_in_chat():
    assert chat_assistant.route("What's the weather?", []) == "answer"
    assert chat_assistant.route("How do I build a website?", []) == "answer"


def test_explicit_work_creates_task():
    assert chat_assistant.route("Build me a bakery website", []) == "task"
    assert chat_assistant.route("Can you fix my app?", []) == "task"


def test_followup_attaches_to_active_task():
    task = [{"id": "one"}]
    assert chat_assistant.route("Also add a contact page", task) == "note"
    assert chat_assistant.route("What is the status?", task) == "status"


def test_business_number_question_uses_live_lookup_path():
    assert (
        chat_assistant.business_contact_query("whats pure michigan lawn care number")
        == "pure michigan lawn care"
    )
    assert (
        chat_assistant.business_contact_query("What's Pure Michigan Lawn Care's phone number?")
        == "Pure Michigan Lawn Care"
    )
