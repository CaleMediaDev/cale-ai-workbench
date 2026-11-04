"""Keep the owner's reference voice cached across spoken chat lines."""

import io
import json
from unittest.mock import Mock, patch

import cale_voice_worker


def test_worker_builds_one_voice_prompt_for_multiple_lines():
    model = Mock()
    prompt = object()
    model.create_voice_clone_prompt.return_value = prompt
    requests = "\n".join(json.dumps({"text": text, "output_path": f"{text}.wav"})
                         for text in ("first", "second")) + "\n"
    with (patch.object(cale_voice_worker, "load_model", return_value=model),
          patch.object(cale_voice_worker, "generate", return_value={"path": "voice.wav"}) as generate,
          patch("sys.stdin", io.StringIO(requests)),
          patch("sys.stdout", new_callable=io.StringIO) as output):
        cale_voice_worker.main()
    model.create_voice_clone_prompt.assert_called_once_with(
        ref_audio=str(cale_voice_worker.REFERENCE),
        ref_text=cale_voice_worker.REFERENCE_TEXT,
    )
    assert [call.args[1] for call in generate.call_args_list] == [prompt, prompt]
    assert len(output.getvalue().splitlines()) == 3


def test_prompt_initialization_diagnostics_do_not_corrupt_json_output():
    model = Mock()
    prompt = object()

    def create_prompt(**kwargs):
        print("Extracting reference voice")
        return prompt

    model.create_voice_clone_prompt.side_effect = create_prompt
    request = json.dumps({"text": "hello", "output_path": "voice.wav"}) + "\n"
    with (patch.object(cale_voice_worker, "load_model", return_value=model),
          patch.object(cale_voice_worker, "generate", return_value={"path": "voice.wav"}) as generate,
          patch("sys.argv", ["cale_voice_worker.py"]),
          patch("sys.stdin", io.StringIO(request)),
          patch("sys.stdout", new_callable=io.StringIO) as output,
          patch("sys.stderr", new_callable=io.StringIO) as diagnostics):
        cale_voice_worker.main()

    assert [json.loads(line) for line in output.getvalue().splitlines()] == [
        {"ready": True},
        {"path": "voice.wav"},
    ]
    assert "Extracting reference voice" in diagnostics.getvalue()
    generate.assert_called_once_with(model, prompt, "hello", "voice.wav")
