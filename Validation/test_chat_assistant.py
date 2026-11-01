"""The chat should answer questions and create durable tasks only for work."""

import json
from unittest.mock import patch

import chat_assistant


def test_questions_stay_in_chat():
    assert chat_assistant.route("What's the weather?", []) == "answer"
    assert chat_assistant.route("How do I build a website?", []) == "answer"


def test_voice_replay_finds_saved_rap_without_another_model_call():
    history = [
        {"role": "assistant", "content": "Here is the rap: Great Lakes beat."},
        {"role": "user", "content": "say the rap with your voice"},
    ]
    assert chat_assistant.spoken_replay_target("say the rap with your voice", history).startswith("Here")
    assert chat_assistant.spoken_replay_target("say that with your voice", history).startswith("Here")
    assert chat_assistant.spoken_replay_target("what is the rap about", history) is None
    history.append({"role": "assistant", "content": "Finished reading that aloud."})
    assert chat_assistant.spoken_replay_target("say that again", history).startswith("Here")


def test_explicit_work_creates_task():
    assert chat_assistant.route("Build me a bakery website", []) == "task"
    assert chat_assistant.route("Can you fix my app?", []) == "task"
    assert chat_assistant.route("Please help me build a website", []) == "task"
    assert chat_assistant.route("Could you work on my app?", []) == "task"
    assert chat_assistant.route("I want to build an app", []) == "task"
    assert chat_assistant.route("I need to fix my website", []) == "task"
    assert chat_assistant.route("Help me to create a logo", []) == "task"
    assert chat_assistant.route("I want to know how to build an app", []) == "answer"


def test_simple_image_uses_local_media_without_a_long_agent_task():
    request = "can you make a image of a dog"
    assert chat_assistant.image_prompt(request) == "a dog"
    assert chat_assistant.route(request, []) == "image"
    assert chat_assistant.route(request, [{"id": "other-task"}]) == "image"
    assert chat_assistant.route_with_files(request, [], [r"C:\photo.png"]) == "task"
    assert chat_assistant.image_prompt("edit this image of a dog") is None


def test_new_song_uses_direct_local_music_job_but_edit_uses_agent():
    request = "can you make a 10-second instrumental beach guitar jingle"
    assert chat_assistant.music_request(request) == {
        "prompt": "make a 10-second instrumental beach guitar jingle",
        "duration_seconds": 10,
        "instrumental": True,
    }
    assert chat_assistant.route(request, []) == "music"
    assert chat_assistant.route_with_files(request, [], [r"C:\song.wav"]) == "task"
    assert chat_assistant.route("make a cover of this song", []) == "task"
    assert chat_assistant.route("make a music app", []) == "task"
    assert chat_assistant.route("create a music video", []) == "task"
    assert chat_assistant.route("write lyrics for a song", []) == "task"
    assert chat_assistant.music_request("make a 2-minute beat")["duration_seconds"] == 120


def test_music_edits_reach_agent_tools_instead_of_plain_chat():
    for request in (
        "Turn this melody into MIDI",
        "Can you convert my recording to MIDI?",
        "Separate vocals from this song",
        "Please isolate the instrumental track",
        "Transcribe this audio into MIDI notes",
    ):
        assert chat_assistant.route(request, []) == "task"
    assert chat_assistant.route("What is MIDI?", []) == "answer"
    assert chat_assistant.route("How do I separate vocals?", []) == "answer"
    assert chat_assistant.route("Convert 12 inches to feet", []) == "answer"
    assert chat_assistant.route_with_files("What's the tempo and key?", [], ["song.wav"]) == "task"


def test_followup_attaches_to_active_task():
    task = [{"id": "one"}]
    assert chat_assistant.route("Also add a contact page", task) == "note"
    assert chat_assistant.route("What is the status?", task) == "status"


def test_reviewed_task_can_receive_chat_feedback():
    tasks = [
        {"id": "review", "title": "Build bakery site", "status": "ready_for_review"},
        {"id": "finished", "title": "Old site", "status": "accepted"},
    ]
    candidates = chat_assistant.followup_tasks(tasks)
    assert [task["id"] for task in candidates] == ["review"]
    assert chat_assistant.route("Also add a contact page", candidates) == "note"
    assert chat_assistant.choose_task("Also add a contact page", candidates)["id"] == "review"
    assert chat_assistant.route("Can you make it mobile friendly?", candidates) == "note"
    assert chat_assistant.route("Please fix that button", candidates) == "note"
    assert chat_assistant.route("Make a new recipe app", candidates) == "task"


def test_multiple_tasks_require_a_clear_followup_target():
    tasks = [{"id": "a", "title": "Build bakery site"}, {"id": "b", "title": "Research suppliers"}]
    assert chat_assistant.choose_task("also add a contact page", tasks) is None
    assert chat_assistant.choose_task("2", tasks)["id"] == "b"
    assert chat_assistant.choose_task("For Build bakery site, add a contact page", tasks)["id"] == "a"
    assert chat_assistant.choose_task("3", tasks) is None
    assert chat_assistant.choose_task("start something else", tasks[:1], allow_single=False) is None


def test_status_covers_all_active_tasks_or_one_named_task():
    tasks = [
        {"id": "a", "title": "Build bakery site"},
        {"id": "b", "title": "Research suppliers"},
    ]
    def progress(task):
        return "Ready for review" if task["id"] == "a" else "Checking sources"
    assert chat_assistant.route("What's the status of Research suppliers?", tasks) == "status"
    all_status = chat_assistant.status_reply("What's the status?", tasks, progress)
    assert "Build bakery site: Ready for review" in all_status
    assert "Research suppliers: Checking sources" in all_status
    one_status = chat_assistant.status_reply("Status of Research suppliers", tasks, progress)
    assert one_status == "Research suppliers: Checking sources"


def test_selected_file_paths_are_preserved_as_data_in_task_brief():
    paths = [r"C:\Users\Cale\My Photos\logo.png", r"C:\Work\app folder"]
    brief = chat_assistant.attach_request("Use these to build my site", paths)
    assert brief.startswith("Use these to build my site\n")
    assert json.loads(brief.split("\n", 3)[3]) == paths


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


def test_personal_contact_questions_stay_local():
    questions = (
        "What's my phone number?", "What is our office address?",
        "What's my email address?", "What are my business hours?",
    )
    with (
        patch.object(chat_assistant, "local_answer", return_value="Saved local detail") as local,
        patch("web_tools.find_business_contact") as phone,
        patch("web_tools.find_business_email") as email,
        patch.object(chat_assistant, "answer_from_web") as web,
    ):
        for question in questions:
            assert chat_assistant.answer_with_tools(question, []) == "Saved local detail"
            assert not chat_assistant.needs_web(question)
    assert local.call_count == len(questions)
    phone.assert_not_called()
    email.assert_not_called()
    web.assert_not_called()
    assert chat_assistant.business_contact_query("What's my number?") is None
    assert chat_assistant.business_email_query("What's my email?") is None
    assert chat_assistant.business_address_query("Where is my business located?") is None


def test_business_email_uses_live_lookup_and_rejects_placeholder():
    assert chat_assistant.business_email_query("What's Pure Michigan Lawn Care's email?") == "Pure Michigan Lawn Care"
    assert chat_assistant.business_email_query("What is the email address for Pure Michigan Lawn Care?") == "Pure Michigan Lawn Care"
    checked = [{"source_url": "https://puremichiganlawncare.com/contact", "name_matching_domain": True}]
    with (
        patch("web_tools.find_business_email", return_value={"sources": [], "checked_pages": checked}) as lookup,
        patch.object(chat_assistant, "local_answer") as model,
    ):
        answer = chat_assistant.answer_with_tools("What's Pure Michigan Lawn Care email?", [])
    lookup.assert_called_once_with("Pure Michigan Lawn Care")
    model.assert_not_called()
    assert "couldn't verify a public email" in answer
    assert checked[0]["source_url"] in answer


def test_business_email_cites_source_and_discloses_conflicts():
    source = {
        "source_url": "https://puremichiganlawncare.com/contact",
        "page_title": "Pure Michigan Lawn Care",
        "name_matching_domain": True,
        "email_addresses": ["office@puremichiganlawncare.com"],
    }
    answer = chat_assistant.business_email_answer("Pure Michigan Lawn Care", {"sources": [source]})
    assert "office@puremichiganlawncare.com" in answer
    assert source["source_url"] in answer
    other = {**source, "source_url": "https://directory.example/listing", "email_addresses": ["help@directorymail.org"]}
    conflict = chat_assistant.business_email_answer("Pure Michigan Lawn Care", {"sources": [source, other]})
    assert "different email addresses" in conflict
    assert "can't confirm which is current" in conflict


def test_business_number_prefers_name_matching_site_and_discloses_conflicts():
    official = {
        "source_url": "https://puremichiganlawncare.com/contact",
        "page_title": "Pure Michigan Lawn Care",
        "phone_links": ["tel:+15172581313"],
        "name_matching_domain": True,
    }
    directory = {
        "source_url": "https://directory.example/listing",
        "page_title": "Directory listing",
        "phone_links": ["tel:(517) 258-1313"],
        "name_matching_domain": False,
    }
    answer = chat_assistant.business_contact_answer("Pure Michigan Lawn Care", [official, directory])
    assert "(517) 258-1313" in answer
    assert official["source_url"] in answer
    assert directory["source_url"] in answer
    directory["phone_links"] = ["tel:+15175550100"]
    conflict = chat_assistant.business_contact_answer("Pure Michigan Lawn Care", [official, directory])
    assert "different phone numbers" in conflict
    assert "(517) 555-0100" in conflict
    assert "can't confirm which is current" in conflict


def test_business_number_can_cite_labeled_visible_text():
    page = {
        "source_url": "https://puremichiganlawncare.com/contact",
        "page_title": "Pure Michigan Lawn Care",
        "phone_links": [],
        "visible_numbers": ["(517) 258-1313"],
        "name_matching_domain": True,
    }
    answer = chat_assistant.business_contact_answer("Pure Michigan Lawn Care", [page])
    assert "(517) 258-1313" in answer
    assert page["source_url"] in answer


def test_current_facts_use_web_but_stable_questions_stay_local():
    assert chat_assistant.needs_web("What's the weather in Howell today?")
    assert chat_assistant.needs_web("Search online for the latest release")
    assert not chat_assistant.needs_web("How do I multiply fractions?")
    assert chat_assistant.needs_web("Summarize https://example.org/")
    assert chat_assistant.needs_web("What's Pure Michigan Lawn Care address?")
    assert chat_assistant.needs_web("What's Acme's email?")
    assert not chat_assistant.needs_web("What is my email address?")


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
    read.assert_called_once_with("https://example.org/", question="Summarize https://example.org/")
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


def test_quick_chat_retries_model_refusal_with_derestricted():
    rejected = {"choices": [{"message": {"content": "I cannot help with that."}}]}
    accepted = {"choices": [{"message": {"content": "Here is the answer."}}]}
    with (
        patch("cloud_ai.post", return_value=rejected),
        patch("cloud_ai.retry_derestricted", return_value=accepted) as retry,
    ):
        answer = chat_assistant.local_answer("Explain the task", [])
    assert answer == "Here is the answer."
    assert retry.call_count == 1


def test_business_address_prefers_official_no_office_statement():
    pages = [
        {"url": "https://puremichiganlawncare.com/", "excerpt":
         "Pure Michigan Lawn Care is a service-area business; no public office address."},
        {"url": "https://directory.example/business", "excerpt": "123 Main St"},
    ]
    with (
        patch.object(chat_assistant, "web_sources", return_value=(pages, [])),
        patch("cloud_ai.post") as model,
    ):
        answer = chat_assistant.answer_from_web(
            "What is Pure Michigan Lawn Care address?", preferred_business="Pure Michigan Lawn Care"
        )
    assert "no public office address" in answer
    assert "puremichiganlawncare.com" in answer
    model.assert_not_called()


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


def test_web_answer_discloses_selected_passages():
    pages = [{
        "title": "Long page", "url": "https://example.gov/long",
        "retrieved_at": "2026-11-01T00:00:00Z",
        "excerpt": "[Selected excerpts from a longer page; other sections are omitted.]\nThe value is 42.",
    }]
    response = {"choices": [{"message": {"content": "The value is 42 [1]."}}]}
    with (
        patch.object(chat_assistant, "web_sources", return_value=(pages, [])),
        patch("cloud_ai.post", return_value=response),
    ):
        answer = chat_assistant.answer_from_web("What is the value?")
    assert "Selected passages only" in answer
