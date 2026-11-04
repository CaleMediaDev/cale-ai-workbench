"""Keep the local Qwen voice model loaded between Cale's spoken updates."""

import contextlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
REFERENCE = ROOT / "assets/cale-voice-reference.wav"
REFERENCE_TEXT = "Hey, I'm Cale with Cale Media. How you doing?"


def load_model():
    """Use the local GPU when present; downloads are staged during setup."""
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from huggingface_hub import snapshot_download
        from qwen_tts import Qwen3TTSModel

        cuda = torch.cuda.is_available()
        # Resolve the pinned cache snapshot first. Passing a local directory
        # prevents Transformers from probing Hugging Face during offline use.
        local_model = snapshot_download(MODEL_ID, local_files_only=True)
        return Qwen3TTSModel.from_pretrained(
            local_model,
            device_map="cuda:0" if cuda else "cpu",
            dtype=torch.bfloat16 if cuda else torch.float32,
            attn_implementation="sdpa",
        )


def generate(model, voice_prompt, text: str, output_path: str) -> dict:
    """Render one bounded update using Cale's own reference recording."""
    import soundfile as sf

    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.redirect_stdout(sys.stderr):
        audio, sample_rate = model.generate_voice_clone(
            text=str(text)[:280],
            language="English",
            voice_clone_prompt=voice_prompt,
        )
    # Keep an existing recording intact if encoding or writing fails.
    # Preserve the suffix so soundfile can infer the requested audio format.
    with tempfile.NamedTemporaryFile(
        dir=target.parent, suffix=target.suffix, delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        sf.write(temporary_path, audio[0], sample_rate)
        temporary_path.replace(target)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {"path": str(target), "engine": "Cale's local voice"}


def main():
    model = load_model()
    # Extract the owner's voice once per worker. Re-encoding the same reference
    # for every short chat line adds avoidable seconds to spoken replies.
    with contextlib.redirect_stdout(sys.stderr):
        voice_prompt = model.create_voice_clone_prompt(
            ref_audio=str(REFERENCE), ref_text=REFERENCE_TEXT,
        )
    if "--warm-model" in sys.argv:
        print(json.dumps({"ready": True}), flush=True)
        return
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                response = generate(model, voice_prompt, request["text"], request["output_path"])
        except Exception as exc:
            response = {"error": str(exc)[:300]}
        print(json.dumps(response, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
