"""The chat should answer questions and create durable tasks only for work."""

from unittest.mock import patch

import chat_assistant


def test_questions_stay_in_chat():
    assert chat_assistant.route("What's the weather?", []) == "answer"
    assert chat_assistant.route("How do I build a website?", []) == "answer"


def test_explicit_work_creates_task():
    assert chat_assistant.route("Build me a bakery website", []) == "task"
    assert chat_assistant.route("Can you fix my app?", []) == "task"
    assert chat_assistant.route("Please help me build a website", []) == "task"
    assert chat_assistant.route("Could you work on my app?", []) == "task"


def test_followup_attaches_to_active_task():
    task = [{"id": "one"}]
    assert chat_assistant.route("Also add a contact page", task) == "note"
    assert chat_assistant.route("What is the status?", task) == "status"


def test_multiple_tasks_require_a_clear_followup_target():
    tasks = [{"id": "a", "title": "Build bakery site"}, {"id": "b", "title": "Research suppliers"}]
    assert chat_assistant.choose_task("also add a contact page", tasks) is None
    assert chat_assistant.choose_task("2", tasks)["id"] == "b"
    assert chat_assistant.choose_task("For Build bakery site, add a contact page", tasks)["id"] == "a"
    assert chat_assistant.choose_task("3", tasks) is None
    assert chat_assistant.choose_task("start something else", tasks[:1], allow_single=False) is None


def test_business_number_question_uses_live_lookup_path():
    assert (
        chat_assistant.business_contact_query("whats pure michigan lawn care number")
        == "pure michigan lawn care"
    )
    assert (
        chat_assistant.business_contact_query("What's Pure Michigan Lawn Care's phone number?")
        == "Pure Michigan Lawn Care"
    )
    assert chat_assistant.business_contact_query("What's Ross number?") == "Ross"


def test_current_facts_use_web_but_stable_questions_stay_local():
    assert chat_assistant.needs_web("What's the weather in Howell today?")
    assert chat_assistant.needs_web("Search online for the latest release")
    assert not chat_assistant.needs_web("How do I multiply fractions?")
    assert chat_assistant.needs_web("Summarize https://example.org/")


def test_supplied_public_url_is_read_without_search():
    page = {
        "title": "Example", "source_url": "https://example.org/",
        "retrieved_at": "2026-11-01T00:00:00Z", "text": "Short page",
    }
    with (
        patch("web_tools.search_web") as search,
        patch("web_tools.read_webpage", return_value=page) as read,
    ):
        pages, leads = chat_assistant.web_sources("Summarize https://example.org/")
    search.assert_not_called()
    read.assert_called_once_with("https://example.org/")
    assert pages[0]["excerpt"] == "Short page"
    assert leads[0]["url"] == page["source_url"]


def test_web_sources_read_pages_and_skip_search_only_leads():
    leads = [
        {"title": "Generic home", "url": "https://example.com/"},
        {"title": "Official forecast", "url": "https://weather.gov/forecast"},
        {"title": "Blocked page", "url": "https://other.example/story"},
    ]
    page = {
        "title": "Official forecast", "source_url": "https://weather.gov/forecast",
        "retrieved_at": "2026-11-01T00:00:00Z", "text": "A" * 150,
    }
    with (
        patch("web_tools.search_web", return_value={"results": leads}),
        patch("web_tools.read_webpage", side_effect=[page, RuntimeError("blocked")]) as read,
    ):
        sources, returned_leads = chat_assistant.web_sources("weather today")
    assert len(sources) == 1
    assert sources[0]["url"] == page["source_url"]
    assert returned_leads == leads
    assert read.call_count == 2


def test_web_answer_uses_local_model_and_lists_read_sources():
    pages = [{
        "title": "Official page", "url": "https://example.gov/current",
        "retrieved_at": "2026-11-01T00:00:00Z", "excerpt": "The current value is 42.",
    }]
    response = {"choices": [{"message": {"content": "The value is 42 [1]."}}]}
    with (
        patch.object(chat_assistant, "web_sources", return_value=(pages, [])),
        patch("cloud_ai.post", return_value=response) as post,
    ):
        answer = chat_assistant.answer_from_web("What is the current value?")
    assert "42 [1]" in answer
    assert "https://example.gov/current" in answer
    assert post.call_args.args[0].startswith("http://127.0.0.1:11434/")
    assert post.call_args.args[1]["reasoning_effort"] == "none"


def test_quick_chat_disables_reasoning_trace_to_leave_room_for_answer():
    response = {"choices": [{"message": {"content": "Hello!"}}]}
    with patch("cloud_ai.post", return_value=response) as post:
        answer = chat_assistant.local_answer("Say hello", [])
    assert answer == "Hello!"
    assert post.call_args.args[1]["reasoning_effort"] == "none"


def test_search_leads_are_not_presented_as_verified_answer():
    lead = {"title": "Possible result", "url": "https://example.org/story"}
    with patch.object(chat_assistant, "web_sources", return_value=([], [lead])):
        answer = chat_assistant.answer_from_web("What happened today?")
    assert "couldn't read a page to verify" in answer
    assert lead["url"] in answer


def test_web_answer_rejects_citation_to_unread_page():
    pages = [{
        "title": "Read page", "url": "https://example.gov/read",
        "retrieved_at": "2026-11-01T00:00:00Z", "excerpt": "The value is 42.",
    }]
    response = {"choices": [{"message": {"content": "The value is 99 [9]."}}]}
    with (
        patch.object(chat_assistant, "web_sources", return_value=(pages, [])),
        patch("cloud_ai.post", return_value=response),
    ):
        answer = chat_assistant.answer_from_web("Current value?")
    assert "99 [9]" not in answer
    assert "could not summarize" in answer
