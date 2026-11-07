import io
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from print_farm_live import CameraTile, LiveCameras, observation


def picture(color):
    output = io.BytesIO()
    Image.new("RGB", (160, 90), color).save(output, format="JPEG")
    return output.getvalue()


class LiveObservationTests(unittest.TestCase):
    def test_video_decoder_parses_split_frames_and_closes_on_stop(self):
        live = LiveCameras(lambda: {})
        live.visible.set()
        live.sources = {"p": "rtsp://localhost/p"}
        raw = picture("red")
        process = Mock()
        process.poll.return_value = None
        process.stdout.read1.side_effect = [raw[:10], raw[10:]]
        with patch("print_farm_live.subprocess.Popen", return_value=process):
            stream = live.video_frames("p", "rtsp://localhost/p")
            self.assertEqual(next(stream), raw)
            live.stop()
            stream.close()
        process.terminate.assert_called()
        process.stdout.close.assert_called_once()
        self.assertEqual(live.processes, {})

    def test_failed_video_uses_configured_snapshot(self):
        live = LiveCameras(lambda: {}, capture=Mock(return_value=picture("red")))
        live.sources = {"p": "rtsp://localhost/p"}
        live.snapshot_urls = {"p": "http://localhost/frame"}
        live.stream_ids = {"p"}
        live.visible.set()
        live.video_frames = Mock(side_effect=OSError("video unavailable"))
        thread = threading.Thread(target=live.camera_loop, args=("p",))
        thread.start()
        deadline = time.time() + 2
        while not live.snapshot("p")[0].get("bytes") and time.time() < deadline:
            time.sleep(0.01)
        live.stop()
        thread.join(2)
        live.capture.assert_called_with("http://localhost/frame")
        self.assertIn("bytes", live.frames["p"])

    def test_rejects_invalid_confidence_and_boxes(self):
        result = observation(
            {
                "state": "clear",
                "confidence": float("nan"),
                "objects": [
                    {"label": "bad", "box": [-1, 0, 100, 100]},
                    {"label": "bad", "box": [100, 0, 50, 100]},
                    {"label": "part", "box": [10, 20, 400, 600]},
                ],
            }
        )
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual([v["label"] for v in result["objects"]], ["part"])

    def test_result_stays_bound_to_exact_analyzed_frame(self):
        raw = picture("red")
        live = LiveCameras(
            lambda: {}, infer=lambda *a, **k: {"state": "occupied", "confidence": 0.9}
        )
        live.sources = {"p": "http://camera"}
        frame = {"bytes": raw, "at": 123, "url": "http://camera"}
        result = live.analyze("p", frame)
        live.frames["p"] = {"bytes": picture("blue")}
        self.assertEqual(result["frame"], raw)
        self.assertEqual(result["captured_at"], 123)
        self.assertNotIn("ticket_id", result)

    def test_changed_camera_discards_inflight_result(self):
        live = LiveCameras(lambda: {})
        live.sources = {"p": "http://old"}

        def infer(*a, **k):
            live.sources["p"] = "http://new"
            return {"state": "clear", "confidence": 1}

        live.infer = infer
        live.analyze("p", {"bytes": picture("red"), "at": 1, "url": "http://old"})
        self.assertNotIn("p", live.results)

    def test_failed_camera_drops_old_live_frame(self):
        finished = threading.Event()

        def capture(url):
            finished.set()
            raise OSError("offline")

        live = LiveCameras(lambda: {}, capture=capture)
        live.sources = {"p": "http://camera"}
        live.frames["p"] = {"bytes": picture("red")}
        live.visible.set()
        thread = threading.Thread(target=live.camera_loop, args=("p",))
        thread.start()
        self.assertTrue(finished.wait(2))
        live.stop()
        thread.join(2)
        self.assertNotIn("bytes", live.frames["p"])
        self.assertIn("error", live.frames["p"])

    def test_overlay_draws_only_on_inspected_frame_and_stale_camera_is_hidden(self):
        import tkinter as tk

        window = tk.Tk()
        window.withdraw()
        live = LiveCameras(lambda: {})
        now = time.time()
        raw, analyzed = picture("blue"), picture("red")
        live.frames["p"] = {"bytes": raw, "at": now, "changed_at": now}
        live.results["p"] = {
            "frame": analyzed,
            "captured_at": now,
            "checked_at": now,
            "state": "occupied",
            "confidence": 0.9,
            "objects": [{"label": "part", "box": [0, 0, 500, 500]}],
        }
        try:
            tile = CameraTile(window, live, "p")
            tile.after_cancel(tile.timer)
            tile.refresh()
            self.assertEqual(tile.last_key[0], hash(raw))
            self.assertFalse(
                any(tile.canvas.type(i) == "rectangle" for i in tile.canvas.find_all())
            )
            tile.after_cancel(tile.timer)
            tile.overlay.set(True)
            tile.refresh()
            self.assertEqual(tile.last_key[0], hash(analyzed))
            self.assertTrue(
                any(tile.canvas.type(i) == "rectangle" for i in tile.canvas.find_all())
            )
            tile.after_cancel(tile.timer)
            live.frames["p"]["changed_at"] = now - 20
            tile.refresh()
            self.assertIsNone(tile.photo)
            self.assertIn("waiting for a live camera", tile.detected.cget("text"))
        finally:
            window.destroy()


if __name__ == "__main__":
    unittest.main()
