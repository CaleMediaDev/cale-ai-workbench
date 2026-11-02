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
    assert "live band instruments" in prompt
    assert music_style.enhance(prompt, instrumental=True) == prompt
    assert "drum machine" in music_style.enhance("Make a tehno beat", instrumental=True)
    assert music_style.enhance("Make techno with distorted guitar", instrumental=True) == (
        "Make techno with distorted guitar"
    )


def test_explicit_vocal_request_overrides_artist_reference():
    assert music_style.vocal_intent("Make a Katy Perry style song with male vocals") == "male"
    assert music_style.vocal_intent("Make a song with female and male duet") == "duet"


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
