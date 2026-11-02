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


def test_explicit_bpm_is_parsed_for_music_engine():
    assert music_style.requested_bpm("Heavy dubstep at 150 BPM") == 150
    assert music_style.requested_bpm("80-bpm synth groove") == 80
    assert music_style.requested_bpm("a fast electronic track") is None
    assert music_style.requested_bpm("10 bpm drone") is None
