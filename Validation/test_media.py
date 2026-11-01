"""Small installed-media smoke test: installed weights and offline speech loop."""

import json
import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "Media/python/Scripts/python.exe"


def call(action, **arguments):
    completed = subprocess.run(
        [str(PYTHON), str(ROOT / "media_worker.py")],
        input=json.dumps({"action": action, **arguments}),
        text=True,
        capture_output=True,
        timeout=120,
        cwd=ROOT,
    )
    payload = json.loads(completed.stdout.splitlines()[-1])
    if completed.returncode:
        raise RuntimeError(payload.get("error", completed.stderr[-500:]))
    return payload


def main():
    for name in ("lcm", "whisper", "trainer"):
        assert (ROOT / "Media/models" / name).is_dir(), f"Missing {name} model"
    with urlopen(
        Request(
            "http://127.0.0.1:11434/api/show",
            data=b'{"model":"cale-vision"}',
            headers={"Content-Type": "application/json"},
        ),
        timeout=30,
    ) as response:
        info = json.load(response)
    assert "vision" in info.get("capabilities", []), info.get("capabilities")
    with tempfile.TemporaryDirectory(dir=ROOT / "Temp") as folder:
        target = str(Path(folder) / "spoken.wav")
        speech = call(
            "speak", text="The offline voice test is working.", output_path=target
        )
        assert Path(speech["path"]).is_file()
        transcript = call("transcribe_audio", path=target)["text"].lower()
        assert "offline" in transcript and "voice" in transcript, transcript
    print("PASS: media weights, local vision model, offline speech and transcription")


if __name__ == "__main__":
    main()
