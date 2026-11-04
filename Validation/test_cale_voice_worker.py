"""Keep the owner's reference voice cached across spoken chat lines."""

import io
import json
from unittest.mock import Mock, patch

import cale_voice_worker


def test_model_loading_uses_float16_when_cuda_does_not_support_bfloat16():
    import sys
    from types import SimpleNamespace

    torch = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=Mock(return_value=True),
            is_bf16_supported=Mock(return_value=False),
        ),
        bfloat16=object(),
        float16=object(),
        float32=object(),
    )
    snapshot_download = Mock(return_value="cached-model")
    model_class = Mock()
    with patch.dict(sys.modules, {
        "torch": torch,
        "huggingface_hub": SimpleNamespace(snapshot_download=snapshot_download),
        "qwen_tts": SimpleNamespace(Qwen3TTSModel=model_class),
    }):
        result = cale_voice_worker.load_model()

    torch.cuda.is_bf16_supported.assert_called_once_with()
    snapshot_download.assert_called_once_with(
        cale_voice_worker.MODEL_ID, local_files_only=True,
    )
    model_class.from_pretrained.assert_called_once_with(
        "cached-model",
        device_map="cuda:0",
        dtype=torch.float16,
        attn_implementation="sdpa",
    )
    assert result is model_class.from_pretrained.return_value


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


def test_successful_audio_write_atomically_replaces_existing_recording(tmp_path):
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    target = tmp_path / "voice.wav"
    target.write_bytes(b"previous recording")
    model = Mock()
    model.generate_voice_clone.return_value = ([[0.0]], 24000)

    def write(path, audio, sample_rate):
        assert Path(path) != target and Path(path).suffix == ".wav"
        assert target.read_bytes() == b"previous recording"
        Path(path).write_bytes(b"complete recording")

    with patch.dict(sys.modules, {"soundfile": SimpleNamespace(write=write)}):
        result = cale_voice_worker.generate(model, object(), "hello", str(target))
    assert result["path"] == str(target)
    assert target.read_bytes() == b"complete recording"
    assert list(tmp_path.iterdir()) == [target]


def test_failed_audio_write_preserves_existing_recording(tmp_path):
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    import pytest

    target = tmp_path / "voice.wav"
    target.write_bytes(b"previous recording")
    model = Mock()
    model.generate_voice_clone.return_value = ([[0.0]], 24000)

    def failing_write(path, audio, sample_rate):
        path = Path(path)
        assert path.suffix == ".wav"
        path.write_bytes(b"partial recording")
        raise OSError("audio write failed")

    soundfile = SimpleNamespace(write=Mock(side_effect=failing_write))
    with patch.dict(sys.modules, {"soundfile": soundfile}):
        with pytest.raises(OSError, match="audio write failed"):
            cale_voice_worker.generate(model, object(), "hello", str(target))

    soundfile.write.assert_called_once()
    assert target.read_bytes() == b"previous recording"
    assert list(tmp_path.iterdir()) == [target]


def test_audio_writer_diagnostics_do_not_corrupt_json_output(tmp_path):
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    target = tmp_path / "voice.wav"
    model = Mock()
    model.generate_voice_clone.return_value = ([[0.0]], 24000)

    def write(path, audio, sample_rate):
        print("Audio writer diagnostic")
        Path(path).write_bytes(b"complete recording")

    request = json.dumps({"text": "hello", "output_path": str(target)}) + "\n"
    with (patch.object(cale_voice_worker, "load_model", return_value=model),
          patch.dict(sys.modules, {"soundfile": SimpleNamespace(write=write)}),
          patch("sys.argv", ["cale_voice_worker.py"]),
          patch("sys.stdin", io.StringIO(request)),
          patch("sys.stdout", new_callable=io.StringIO) as output,
          patch("sys.stderr", new_callable=io.StringIO) as diagnostics):
        cale_voice_worker.main()

    assert [json.loads(line) for line in output.getvalue().splitlines()] == [
        {"ready": True},
        {"path": str(target.resolve()), "engine": "Cale's local voice"},
    ]
    assert "Audio writer diagnostic" in diagnostics.getvalue()
    assert target.read_bytes() == b"complete recording"
    assert list(tmp_path.iterdir()) == [target]
