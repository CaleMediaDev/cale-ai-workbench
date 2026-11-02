"""The chat should answer questions and create durable tasks only for work."""

import json
from unittest.mock import patch

import pytest

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
    assert chat_assistant.spoken_replay_target("read aloud the rap", history).startswith("Here")
    assert chat_assistant.spoken_replay_target("please read me the rap", history).startswith("Here")
    assert chat_assistant.spoken_replay_target("what is the rap about", history) is None
    history.append({"role": "assistant", "content": "Finished reading that aloud."})
    assert chat_assistant.spoken_replay_target("say that again", history).startswith("Here")


def test_voice_replay_reads_unlabelled_generated_verse():
    history = [
        {"role": "assistant", "content": "The lake is bright tonight,\nThe city lights are in flight."},
        {"role": "user", "content": "say the rap it made with its voice"},
    ]
    assert chat_assistant.spoken_replay_target(
        "say the rap it made with its voice", history
    ) == history[0]["content"]


def test_voice_replay_prefers_recent_unlabelled_verse_over_old_rap_mention():
    verse = "The water shines beneath the moon,\nWe'll build that dream before it's noon."
    history = [
        {"role": "assistant", "content": "I can write a rap for you."},
        {"role": "assistant", "content": verse},
        {"role": "user", "content": "read the rap with your voice"},
    ]
    assert chat_assistant.spoken_replay_target("read the rap with your voice", history) == verse


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
        "lyrics": "",
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
    assert chat_assistant.music_request("make a 2-minute beat")["instrumental"] is True
    assert chat_assistant.music_request("make a rap beat with vocals")["instrumental"] is False
    rap = "make a 3 min rap with heavy bass about cale media"
    assert chat_assistant.route(rap, []) == "music"
    assert chat_assistant.music_request(rap)["duration_seconds"] == 180
    assert chat_assistant.music_request("make a 1 minute 30 second song")["duration_seconds"] == 90
    assert chat_assistant.music_request("make a 1 minute and 30 second song")["duration_seconds"] == 90
    assert chat_assistant.music_request("make a 1:30 song")["duration_seconds"] == 90
    with pytest.raises(ValueError, match="10 seconds to 10 minutes"):
        chat_assistant.music_request("make an 11 minute song")


def test_direct_song_preserves_explicit_lyrics_and_routes_ambiguous_text_to_agent():
    request = "Make a 90-second soul song with these lyrics:\n[Verse]\nWe made it home\n[Chorus]\nSing it again"
    spec = chat_assistant.music_request(request)
    assert spec == {
        "prompt": "Make a 90-second soul song",
        "lyrics": "[Verse]\nWe made it home\n[Chorus]\nSing it again",
        "duration_seconds": 90,
        "instrumental": False,
    }
    assert chat_assistant.route(request, []) == "music"
    lyric_words = "Make a soul song with lyrics:\nCover the night\nMeet me at 1:30"
    assert chat_assistant.music_request(lyric_words)["lyrics"] == "Cover the night\nMeet me at 1:30"
    assert chat_assistant.music_request(lyric_words)["duration_seconds"] == 30
    assert chat_assistant.route("Make a soul song with lyrics:\nWhat's the status of our love", []) == "music"
    assert chat_assistant.route("Make a song\nUse this melody\nThen change the chorus", []) == "task"
    assert chat_assistant.route("Make an instrumental song with lyrics: sing along", []) == "task"


def test_music_edits_reach_agent_tools_instead_of_plain_chat():
    for request in (
        "Turn this melody into MIDI",
        "Can you convert my recording to MIDI?",
        "Separate vocals from this song",
        "Please isolate the instrumental track",
        "Transcribe this audio into MIDI notes",
        "Trim this song to 30 seconds, fade out the end, and mix my voice over it",
        "Fade the soundtrack out at the end",
        "Mix my voice over the background music",
        "Adjust the volume of this recording",
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


def test_finished_task_can_receive_named_revision_in_chat():
    tasks = [
        {"id": "live", "title": "Build bakery site", "status": "working", "updated": 20},
        {"id": "song", "title": "Techno song", "status": "accepted", "updated": 10},
    ]
    candidates = chat_assistant.followup_tasks(tasks, "Improve Techno song with no guitars")
    assert [task["id"] for task in candidates] == ["live", "song"]
    assert chat_assistant.route("Improve Techno song with no guitars", candidates) == "note"
    assert chat_assistant.choose_task("Improve Techno song with no guitars", candidates)["id"] == "song"


def test_vague_revision_offers_recent_finished_tasks_without_guessing():
    tasks = [
        {"id": str(index), "title": f"Finished {index}", "status": "accepted", "updated": index}
        for index in range(8)
    ]
    candidates = chat_assistant.followup_tasks(tasks, "Make it louder")
    assert [task["id"] for task in candidates] == ["7", "6", "5", "4", "3"]
    assert chat_assistant.route("Make it louder", candidates) == "note"
    assert chat_assistant.choose_task("Make it louder", candidates) is None
    assert chat_assistant.choose_task("2", candidates)["id"] == "6"
    assert chat_assistant.followup_tasks(tasks, "Make a new app") == []


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
    assert chat_assistant.business_contact_query("What's the phone number for Pure Michigan Lawn Care?") == "Pure Michigan Lawn Care"
    assert chat_assistant.business_contact_query("Can you find the number of Pure Michigan Lawn Care?") == "Pure Michigan Lawn Care"


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


def test_selected_file_memory_answers_only_explicit_file_questions():
    row = {"path": r"C:\Selected\acme-invoice.txt",
           "excerpt": "Acme invoice total is $42."}
    with (
        patch("user_file_memory.search", return_value=[row]) as search,
        patch.object(chat_assistant, "local_answer", return_value="The total is $42.") as answer,
    ):
        assert chat_assistant.answer_with_tools("What does my Acme invoice say?", []) == "The total is $42."
        search.assert_called_once()
        assert search.call_args.kwargs["match_all"] is True
        assert "acme" in search.call_args.args[1]
        assert row["path"] in answer.call_args.kwargs["file_evidence"]
        search.reset_mock()
        chat_assistant.answer_with_tools("How do I make an invoice?", [])
        chat_assistant.answer_with_tools("What's the best photo editor?", [])
        search.assert_not_called()


def test_file_memory_miss_does_not_guess_or_use_an_unrelated_recent_file():
    with (
        patch("user_file_memory.search", return_value=[]) as search,
        patch.object(chat_assistant, "local_answer") as answer,
    ):
        result = chat_assistant.answer_with_tools("What does my Acme invoice say?", [])
    assert "couldn't find" in result.lower()
    search.assert_called_once()
    answer.assert_not_called()


def test_file_memory_terms_ignore_contraction_fragments():
    assert chat_assistant.memory_search_terms("What's in my Acme invoice?") == "acme invoice"


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


def test_business_number_discloses_tel_and_visible_conflict():
    page = {
        "source_url": "https://shop.example/contact",
        "page_title": "Shop",
        "phone_links": ["tel:+15175550100"],
        "visible_numbers": ["(517) 258-1313"],
        "name_matching_domain": True,
    }
    answer = chat_assistant.business_contact_answer("Shop", [page])
    assert "different phone numbers" in answer
    assert "(517) 555-0100" in answer
    assert "(517) 258-1313" in answer


def test_business_number_distinguishes_failed_lookup_from_no_listing():
    with patch("web_tools.find_business_contact", return_value={
        "sources": [], "lookup_status": "pages_unavailable"
    }):
        answer = chat_assistant.answer_with_tools("What's the phone number for Shop?", [])
    assert "could not be read" in answer


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


def test_web_answer_does_not_present_a_cut_off_citation_as_complete():
    pages = [{"title": "Official page", "url": "https://example.gov/fact",
              "retrieved_at": "2026-11-01T00:00:00Z", "excerpt": "The value is 42."}]
    partial = {"choices": [{"message": {"content": "The value is 42 [1], and"},
                            "finish_reason": "length"}]}
    complete = {"choices": [{"message": {"content": "The value is 42 [1]."},
                             "finish_reason": "stop"}]}
    with (
        patch.object(chat_assistant, "web_sources", return_value=(pages, [])),
        patch("cloud_ai.post", side_effect=[partial, complete]) as post,
    ):
        answer = chat_assistant.answer_from_web("What is the value?")
    assert "The value is 42 [1]." in answer
    assert "and\nSources:" not in answer
    assert post.call_count == 2


def test_quick_chat_disables_reasoning_trace_to_leave_room_for_answer():
    response = {"choices": [{"message": {"content": "Hello!"}}]}
    with patch("cloud_ai.post", return_value=response) as post:
        answer = chat_assistant.local_answer("Say hello", [])
    assert answer == "Hello!"
    assert post.call_args.args[1]["reasoning_effort"] == "none"


def test_quick_chat_retries_cut_off_writing_before_displaying_it():
    partial = {"choices": [{"message": {"content": "The rap starts... Keep"},
                            "finish_reason": "length"}]}
    complete = {"choices": [{"message": {"content": "The full rap ends cleanly."},
                             "finish_reason": "stop"}]}
    with patch("cloud_ai.post", side_effect=[partial, complete]) as post:
        answer = chat_assistant.local_answer("Old-school Michigan rap", [])
    assert answer == "The full rap ends cleanly."
    assert [call.args[1]["max_tokens"] for call in post.call_args_list] == [2048, 4096]


def test_quick_chat_never_presents_cut_off_answer_as_complete():
    partial = {"choices": [{"message": {"content": "Keep"}, "finish_reason": "length"}]}
    with patch("cloud_ai.post", return_value=partial) as post:
        with pytest.raises(RuntimeError, match="too long for one chat reply"):
            chat_assistant.local_answer("A very long story", [])
    assert post.call_count == 4


def test_quick_chat_times_out_without_blocking_later_messages_for_many_minutes():
    clock = [0.0]
    timeouts = []

    def unavailable(_url, _payload, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"] + 1
        raise OSError("model stalled")

    with (
        patch.object(chat_assistant.time, "monotonic", side_effect=lambda: clock[0]),
        patch("cloud_ai.post", side_effect=unavailable),
    ):
        with pytest.raises(RuntimeError, match="took too long"):
            chat_assistant.local_answer("Are you there?", [])
    assert sum(timeouts) <= 150
    assert len(timeouts) == 2


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
