"""Singer and genre requests are explicit in the local generator caption."""

import music_style


def test_katy_perry_reference_gets_female_pop_direction_without_voice_clone():
    prompt = music_style.enhance("Make a song like Katy Perry")
    assert "female lead vocals" in prompt
    assert "dance-pop" in prompt
    assert music_style.vocal_intent(prompt) == "female"
    assert music_style.enhance(prompt) == prompt


def test_techno_uses_electronic_instruments_unless_guitar_is_requested():
    prompt = music_style.enhance("Make a techno song", instrumental=True)
    assert "drum machine" in prompt
    assert "sequenced synthesizers" in prompt
    assert prompt.startswith("Entirely electronic production")
    assert music_style.enhance(prompt, instrumental=True) == prompt
    assert "drum machine" in music_style.enhance("Make a tehno beat", instrumental=True)
    assert music_style.enhance("Make techno with distorted guitar", instrumental=True) == (
        "Make techno with distorted guitar"
    )


def test_dubstep_caption_keeps_halftime_drops_instead_of_techno_kick():
    request = "150 BPM heavy dubstep with halftime drums, syncopated wobbles and huge bass drops"
    caption = music_style.enhance(request, instrumental=True)
    assert "halftime snare-led drums" in caption
    assert "four-on-the-floor" not in caption
    assert music_style.enhance(caption, instrumental=True) == caption
    explicit = music_style.enhance(
        "Make dubstep with a four-on-the-floor kick", instrumental=True
    )
    assert "four-on-the-floor kick" in explicit


def test_explicit_vocal_request_overrides_artist_reference():
    assert music_style.vocal_intent("Make a Katy Perry style song with male vocals") == "male"
    assert music_style.vocal_intent("Make a song with female and male duet") == "duet"


def test_negated_instruments_and_singers_do_not_override_intent():
    techno = music_style.enhance("Make techno without guitars", instrumental=True)
    assert techno.startswith("Entirely electronic production")
    assert "guitar" not in techno.lower()
    assert music_style.enhance(techno, instrumental=True) == techno
    pop = music_style.enhance("Make a Katy Perry song, not a guy voice")
    assert pop.startswith("Prominent expressive female lead vocals")
    assert "guy" not in pop.lower()
    assert music_style.vocal_intent("Make a Katy Perry song, not a guy voice") == "female"
    assert music_style.enhance(pop) == pop


def test_misspelled_artist_and_feedback_become_positive_music_directions():
    prompt = music_style.enhance("make a song like katy pary")
    assert "female lead vocals" in prompt
    assert "dance-pop" in prompt
    direction = music_style.feedback_direction("It was a guy voice", prompt)
    assert "female lead singer" in direction
    assert "guy" not in direction
    learned = music_style.learned_directions(
        "Past user feedback:\n- Improve: It was a guy voice", prompt
    )
    assert learned == direction


def test_techno_feedback_does_not_repeat_rejected_instrument_name():
    direction = music_style.feedback_direction(
        "There are always guitars when I want techno", "make a tehno song"
    )
    assert "sequenced synth bass" in direction
    assert "guitar" not in direction


def test_vocal_chops_are_texture_not_full_length_singing():
    request = "Heavy dubstep with minimal robotic vocal chops and a fake drop"
    assert not music_style.requires_sustained_vocals(request)
    caption = music_style.enhance(request)
    assert "Sparse chopped vocal syllables" in caption
    assert not music_style.requires_sustained_vocals(caption)
    assert music_style.requires_sustained_vocals("Rap song with vocal chops")
    assert music_style.requires_sustained_vocals(request, lyrics="[Verse] Sing this")
    assert music_style.requires_sustained_vocals("Make a pop song with lead vocals")


def test_rejected_singer_does_not_turn_vocal_texture_into_sustained_singing():
    for exclusion in ("no male singer", "without a female singer"):
        request = "Heavy dubstep with vocal chops, " + exclusion
        assert not music_style.requires_sustained_vocals(request)
        caption = music_style.enhance(request)
        assert "Sparse chopped vocal syllables" in caption
        assert "clear sung verses and chorus" not in caption
        assert not music_style.requires_sustained_vocals(caption)
        assert music_style.requires_sustained_vocals(
            request + ", but include lead vocals"
        )
        assert music_style.requires_sustained_vocals(request, lyrics="Sing this verse")


def test_negated_gendered_lead_does_not_override_vocal_texture():
    for gender in ("male", "female"):
        request = "Heavy dubstep with vocal chops, no " + gender + " lead vocals"
        assert not music_style.explicit_lead_vocals(request)
        assert not music_style.unambiguous_vocal_song(request)
        assert not music_style.requires_sustained_vocals(request)
        caption = music_style.enhance(request)
        assert "Sparse chopped vocal syllables" in caption
        assert "clear sung verses and chorus" not in caption
        assert "no " + gender + " lead vocals" not in caption
        assert not music_style.requires_sustained_vocals(caption)

        positive = "Heavy dubstep with " + gender + " lead vocals"
        assert music_style.explicit_lead_vocals(positive)
        assert music_style.requires_sustained_vocals(positive)
        assert gender + " lead vocals throughout" in music_style.enhance(positive)


def test_any_gendered_vocal_exclusion_preserves_texture_and_positive_intent():
    for gender, other in (("male", "female"), ("female", "male")):
        for exclusion in ("no any " + gender + " vocals",
                          "without any " + gender + " lead vocals",
                          "avoid any " + gender + " singer"):
            request = "Heavy dubstep with vocal chops, " + exclusion
            assert not music_style.explicit_lead_vocals(request)
            assert not music_style.unambiguous_vocal_song(request)
            assert not music_style.requires_sustained_vocals(request)
            assert music_style.vocal_intent(request) == other
            caption = music_style.enhance(request)
            assert "Sparse chopped vocal syllables" in caption
            assert "clear sung verses and chorus" not in caption
            assert exclusion not in caption
            assert music_style.enhance(caption) == caption

            positive = request + ", but include " + other + " lead vocals"
            assert music_style.vocal_intent(positive) == other
            assert music_style.explicit_lead_vocals(positive)
            assert music_style.requires_sustained_vocals(positive)
            assert other + " lead vocals throughout" in music_style.enhance(positive)


def test_negated_lead_vocals_preserve_vocal_texture():
    for exclusion in ("no lead vocals", "without any lead vocals",
                      "avoid lead vocals", "don't want lead vocals"):
        request = "Heavy dubstep with vocal chops, " + exclusion
        assert not music_style.explicit_lead_vocals(request)
        assert not music_style.unambiguous_vocal_song(request)
        assert not music_style.requires_sustained_vocals(request)
        caption = music_style.enhance(request)
        assert "Sparse chopped vocal syllables" in caption
        assert "clear sung verses and chorus" not in caption
        assert exclusion not in caption
        assert not music_style.requires_sustained_vocals(caption)
        assert music_style.enhance(caption) == caption

    positive = "Heavy dubstep with vocal chops and lead vocals"
    assert music_style.explicit_lead_vocals(positive)
    assert music_style.unambiguous_vocal_song(positive)
    assert music_style.requires_sustained_vocals(positive)


def test_sparse_female_hook_does_not_get_full_verse_direction():
    request = ("Female singing voice, main vocal hook: waapy waaap. "
               "Very little conventional singing, with massive instrumental drops.")
    assert music_style.sparse_vocal_style(request)
    caption = music_style.enhance(request)
    assert "short female vocal hooks" in caption
    assert "clear sung verses and chorus" not in caption


def test_clear_vocal_request_is_distinct_from_negated_or_instrumental_brief():
    assert music_style.unambiguous_vocal_song(
        "Male rapper with two verses and a vocal hook, then an instrumental break")
    assert not music_style.unambiguous_vocal_song("Make an instrumental rap beat")
    assert not music_style.unambiguous_vocal_song("No male vocals; instrumental only")
    assert not music_style.unambiguous_vocal_song(
        "Make an instrumental song with a male rapper")


def test_past_feedback_does_not_veto_current_vocal_request():
    request = "Make a song with female lead vocals"
    for complaint in ("There were no vocals", "It sounded like an instrumental track"):
        prompt = request + "\nPast user feedback:\n- Improve: " + complaint
        assert music_style.unambiguous_vocal_song(prompt)
    for request in ("No vocals, even with a singer reference",
                    "Make an instrumental track with a female singer reference"):
        prompt = request + "\nPast user feedback:\n- Improve: Add lead vocals"
        assert not music_style.unambiguous_vocal_song(prompt)


def test_explicit_vocal_free_request_does_not_gain_singer_or_texture_cues():
    for exclusion in ("no vocals", "without vocals"):
        request = "Katy Perry-style techno with " + exclusion
        assert music_style.vocal_intent(request) == "none"
        assert not music_style.requires_sustained_vocals(request)
        caption = music_style.enhance(request)
        assert "female lead vocals" not in caption
        assert "Sparse chopped vocal syllables" not in caption
        assert "drum machine" in caption
        assert music_style.enhance(caption) == caption

    texture = "Katy Perry-style wordless vocals with no lyrics"
    assert music_style.vocal_intent(texture) == "female"
    assert "Sparse chopped vocal syllables" in music_style.enhance(texture)
    current = "Katy Perry-style song\nPast user feedback: no vocals"
    assert music_style.vocal_intent(current) == "female"
    assert music_style.requires_sustained_vocals(current)


def test_explicit_singing_exclusion_does_not_gain_singer_cues():
    for exclusion in ("no singing", "without singing"):
        request = "Katy Perry-style techno with " + exclusion
        assert music_style.vocal_intent(request) == "none"
        assert not music_style.requires_sustained_vocals(request)
        assert not music_style.unambiguous_vocal_song(request)
        caption = music_style.enhance(request)
        assert "female lead vocals" not in caption
        assert "clear sung verses and chorus" not in caption
        assert "Sparse chopped vocal syllables" not in caption
        assert "drum machine" in caption
        assert music_style.enhance(caption) == caption

    texture = "Katy Perry-style wordless vocals with no lyrics"
    assert music_style.vocal_intent(texture) == "female"
    assert not music_style.requires_sustained_vocals(texture)
    assert "Sparse chopped vocal syllables" in music_style.enhance(texture)


def test_explicit_instrumental_request_does_not_gain_vocal_cues():
    for description in ("instrumental track", "fully instrumental"):
        request = "Katy Perry-style techno, " + description
        assert music_style.vocal_intent(request) == "none"
        assert not music_style.requires_sustained_vocals(request)
        caption = music_style.enhance(request)
        assert "female lead vocals" not in caption
        assert "Sparse chopped vocal syllables" not in caption
        assert "drum machine" in caption
        assert music_style.enhance(caption) == caption

    request = "Female lead vocals with an instrumental break"
    assert music_style.vocal_intent(request) == "female"
    assert music_style.requires_sustained_vocals(request)
    assert "female lead vocals throughout" in music_style.enhance(request)

    request += "\nPast user feedback: Make an instrumental track"
    assert music_style.vocal_intent(request) == "female"
    assert music_style.requires_sustained_vocals(request)


def test_vocal_exclusions_use_the_shared_negation_vocabulary():
    for negation in ("no", "without", "not", "avoid", "exclude", "skip",
                     "don't want", "do not want"):
        for sound in ("vocals", "singing"):
            request = "Katy Perry-style techno, " + negation + " any " + sound
            assert music_style.vocal_intent(request) == "none"
            assert not music_style.requires_sustained_vocals(request)
            assert not music_style.unambiguous_vocal_song(request)
            caption = music_style.enhance(request)
            assert "female lead vocals" not in caption
            assert "clear sung verses and chorus" not in caption
            assert "Sparse chopped vocal syllables" not in caption
            assert "drum machine" in caption
            assert music_style.enhance(caption) == caption

    texture = "Katy Perry-style wordless vocals, avoid lyrics"
    assert music_style.vocal_intent(texture) == "female"
    assert not music_style.requires_sustained_vocals(texture)
    assert "Sparse chopped vocal syllables" in music_style.enhance(texture)


def test_explicit_bpm_is_parsed_for_music_engine():
    assert music_style.requested_bpm("Heavy dubstep at 150 BPM") == 150
    assert music_style.requested_bpm("80-bpm synth groove") == 80
    assert music_style.requested_bpm("a fast electronic track") is None
    assert music_style.requested_bpm("10 bpm drone") is None


def test_explicit_lead_and_chops_keep_full_lead_caption_idempotent():
    for gender in ("female", "male"):
        request = f"Dubstep with vocal chops and {gender} lead vocals"
        caption = music_style.enhance(request)
        assert f"{gender} lead vocals throughout" in caption
        assert f"short {gender} vocal hooks" not in caption
        assert "vocal chops" in caption
        assert music_style.enhance(caption) == caption
        assert music_style.requires_sustained_vocals(caption)


def test_old_chopped_feedback_does_not_override_current_sung_verses():
    request = "Female singer with sung verses and chorus"
    expected = music_style.enhance(request)
    for complaint in ("There were vocal chops", "There were only short hooks"):
        feedback = "\nPast user feedback: " + complaint
        caption = music_style.enhance(request + feedback)
        assert caption == expected + feedback
        assert "female lead vocals throughout, clear sung verses and chorus" in caption
        assert "short female vocal hooks" not in caption
        assert music_style.requires_sustained_vocals(caption)
        assert music_style.enhance(caption) == caption


def test_lead_cue_from_old_feedback_does_not_override_current_chopped_texture():
    request = "Heavy dubstep with vocal chops, without any male lead vocals"
    caption = music_style.enhance(request + "\nPast user feedback: Add female lead vocals")
    assert caption.startswith("Sparse chopped vocal syllables")
    assert "clear sung verses and chorus" not in caption
    assert not music_style.requires_sustained_vocals(caption)


def test_past_feedback_cannot_supply_current_song_tempo():
    feedback = "\nPast user feedback:\n- Similar task the user liked: Piano at 80 BPM"
    assert music_style.requested_bpm("Make a bright synth track" + feedback) is None
    assert music_style.requested_bpm("Make a 150-BPM synth track" + feedback) == 150
