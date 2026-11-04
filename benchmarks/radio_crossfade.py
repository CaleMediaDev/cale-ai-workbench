"""Exercise two real Windows MCI decks with quiet generated tones, no inference.

This proves overlapping device states and gain commands, not beat matching or
the absence of silence inside a generated song's own musical arrangement.
"""
import argparse
import json
import math
import struct
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import radio_station


def tone(path, frequency):
    rate = 22050
    # Quiet source plus 1% master volume avoids disturbing existing playback.
    samples = b"".join(struct.pack("<h", round(3276*math.sin(2*math.pi*frequency*i/rate))) for i in range(rate*6))
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    first, second = args.output/"first.wav", args.output/"second.wav"
    tone(first, 440)
    tone(second, 660)
    player = radio_station.Player()
    player.volume = 1
    samples, paused = [], False
    started = time.monotonic()
    try:
        player.play(first)
        outgoing = player.active
        player.prepare(second)
        assert player.prepared.opened and not player.prepared.busy() and player.prepared.volume == 0
        while player.clock()[0] < 3:
            time.sleep(0.02)
        player.blend(second, 3)
        incoming = player.active
        assert outgoing.busy() and incoming.busy()
        while True:
            player.advance_mix()
            before = incoming.clock()[0]
            if not paused and before >= 0.8:
                player.pause()
                assert outgoing.command(f"status {outgoing.alias} mode") == "paused"
                assert incoming.command(f"status {incoming.alias} mode") == "paused"
                player.set_volume(0)
                assert outgoing.volume == incoming.volume == 0
                player.set_volume(1)
                time.sleep(0.15)
                assert abs(incoming.clock()[0]-before) < 0.08
                player.resume()
                paused = True
            sample = {"seconds": round(time.monotonic()-started, 3),
                      "outgoing_active": outgoing.busy(), "incoming_active": incoming.busy(),
                      "outgoing_gain": outgoing.volume if outgoing.opened else 0, "incoming_gain": incoming.volume,
                      "outgoing_device_volume": int(outgoing.command(f"status {outgoing.alias} volume")) if outgoing.opened else 0,
                      "incoming_device_volume": int(incoming.command(f"status {incoming.alias} volume"))}
            samples.append(sample)
            assert sample["outgoing_active"] or sample["incoming_active"], sample
            assert sample["outgoing_device_volume"] or sample["incoming_device_volume"], sample
            assert abs(sample["incoming_device_volume"]-round(incoming.volume*10)) <= 1, sample
            if not player.mixing:
                break
            time.sleep(0.04)
        assert len(samples) > 20 and paused
        assert incoming.busy() and not outgoing.opened and incoming.volume == 1
    finally:
        player.stop()
    report = {"passed": True, "samples": len(samples),
              "overlap_samples": sum(row["outgoing_active"] and row["incoming_active"] for row in samples),
              "no_both_inactive_sample": True, "no_both_zero_gain_sample": True,
              "device_reported_per_deck_volumes_verified": True,
              "preload_without_start_passed": True, "pause_resume_mute_passed": paused,
              "outgoing_closed_after_fade": True, "all_decks_closed_after_stop": not player.busy(),
              "seconds": round(time.monotonic()-started, 2),
              "interpretation": "Real Windows MCI overlap and gain commands on quiet six-second tones. Not a loopback-audio, beat-matching, or subjective listening test."}
    (args.output/"result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
