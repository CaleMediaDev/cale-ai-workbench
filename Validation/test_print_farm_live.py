import io
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from print_farm_live import CameraTile, LiveCameras, observation, scan_due


def picture(color):
    output = io.BytesIO()
    Image.new("RGB", (160, 90), color).save(output, format="JPEG")
    return output.getvalue()


class LiveObservationTests(unittest.TestCase):
    def test_quiet_scene_saves_scans_but_changes_and_periodic_checks_still_run(self):
        frame = {
            "bytes": b"frame",
            "at": 100,
            "changed_at": 100,
            "signature": bytes([128] * 576),
        }
        result = {"checked_at": 80, "signature": bytes([129] * 576)}
        self.assertFalse(scan_due(frame, result, False, 100))
        self.assertTrue(scan_due(frame, result, True, 100))
        result["checked_at"] = 55
        self.assertTrue(scan_due(frame, result, False, 100))
        result["checked_at"] = 90
        frame["signature"] = bytes([180] * 60 + [128] * 516)
        self.assertTrue(scan_due(frame, result, False, 100))
        frame["changed_at"] = 60
        self.assertFalse(scan_due(frame, result, False, 100))

    def test_pending_queue_inference_precedes_another_background_scan(self):
        from print_farm import InferenceGate

        gate = InferenceGate()
        self.assertTrue(gate.acquire(False))
        entered = threading.Event()

        def foreground():
            with gate:
                entered.set()

        thread = threading.Thread(target=foreground)
        thread.start()
        try:
            deadline = time.time() + 2
            while not gate.waiters and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(gate.waiters, 1)
            self.assertFalse(gate.acquire(False))
        finally:
            gate.release()
            thread.join(2)
        self.assertTrue(entered.is_set())

    def test_status_changes_preserve_camera_and_responsive_grid(self):
        import tkinter as tk
        from pathlib import Path
        from types import SimpleNamespace

        from print_farm_ui import Controls

        window = tk.Tk()
        window.withdraw()
        parent = tk.Frame(window)
        service = SimpleNamespace(
            settings=lambda: {},
            snapshot={"printers": [], "orders": []},
            start=Mock(),
            stop=Mock(),
            request_bed_check=Mock(),
            bed_check_states=lambda: {},
        )
        try:
            page = Controls(parent, window, Path("."), service=service)
            data = {
                "printers": [
                    {"id": str(i), "hostname": str(i), "detail": "Idle", "online": True, "idle": True}
                    for i in range(4)
                ],
                "orders": [],
            }
            page.render(data)
            tile = page.camera_tiles["0"]
            card = page.printer_cards["0"]
            self.assertTrue(card.check_bed.instate(["disabled"]))
            data["printers"][0].update(camera_url="http://camera", bed={"state": "out_of_view"})
            page.render(data)
            self.assertIn("Bed out of view", card.bed.cget("text"))
            card.check_bed.invoke()
            service.request_bed_check.assert_called_once_with("0")
            page.render(data, bed_checks={"0": "queued"})
            self.assertIn("Check queued", card.bed.cget("text"))
            self.assertTrue(card.check_bed.instate(["disabled"]))
            page.render(data, bed_checks={"0": "checking"})
            self.assertIn("Checking bed", card.bed.cget("text"))
            self.assertEqual(page.printer_cards["0"].percent.cget("text"), "—")
            data["printers"][0].update(detail="Printing / busy", progress="78.4", idle=False,
                                       seconds_remaining=982, nozzle_c="220.02", bed_c="50.21",
                                       bed={"state": "clear"}, color="black")
            page.render(data)
            self.assertIs(tile, page.camera_tiles["0"])
            self.assertTrue(card.check_bed.instate(["disabled"]))
            self.assertEqual(card.percent.cget("text"), "78%")
            self.assertIn("PRINTING", card.status.cget("text"))
            self.assertIn("clear", card.bed.cget("text"))
            self.assertEqual([value.cget("text") for value in card.metrics],
                             ["17m", "220°C", "50°C"])
            self.assertEqual(card.accent.cget("bg"), card.FILAMENT["black"][0])
            badge_color = card.status.cget("fg")
            data["printers"][0]["color"] = "white"
            page.render(data)
            self.assertEqual(card.accent.cget("bg"), card.FILAMENT["white"][0])
            self.assertEqual(card.icon.itemcget("printer", "fill"), card.FILAMENT["white"][0])
            self.assertEqual(card.status.cget("fg"), badge_color)
            data["printers"][0].update(detail="Finished · waiting for clear bed", idle=True)
            page.render(data)
            self.assertIn("Unload finished print", card.bed.cget("text"))
            self.assertTrue(card.camera.unload_required)
            data["printers"][0].update(detail="Offline", online=False, progress=78)
            page.render(data)
            self.assertEqual(card.percent.cget("text"), "—")
            self.assertIn("OFFLINE", card.status.cget("text"))
            self.assertEqual([value.cget("text") for value in card.metrics], ["—"] * 3)
            data["printers"][0]["busy_unconfirmed"] = True
            page.render(data)
            self.assertIn("CONNECTION LOST", card.status.cget("text"))
            self.assertEqual(card.percent.cget("text"), "—")
            del data["printers"][0]["busy_unconfirmed"]

            # A general service heartbeat can stay fresh while the printer
            # sample stalls. Only the printer timestamp can restore the badge.
            page.live.update = Mock()
            data["printers"][0].update(detail="Printing / busy", online=True, idle=False)
            data["printer_telemetry_at"] = 100
            service.snapshot = data
            with patch("print_farm_ui.time.time", return_value=100):
                page.tick()
            self.assertIn("PRINTING", card.status.cget("text"))
            data["checked_at"] = 121
            with patch("print_farm_ui.time.time", return_value=121):
                page.tick()
                self.assertIn("SIGNAL DELAYED", card.status.cget("text"))
                self.assertIn("SIGNAL DELAYED", page.fleet_status.cget("text"))
                self.assertEqual(card.percent.cget("text"), "—")
                self.assertEqual([value.cget("text") for value in card.metrics], ["—"] * 3)
                self.assertIn("check pending", card.bed.cget("text"))
                self.assertIs(tile, page.camera_tiles["0"])
                data["printer_telemetry_at"] = 121
                page.tick()
            self.assertIn("PRINTING", card.status.cget("text"))
            self.assertEqual(card.percent.cget("text"), "78%")
            page.layout_printers(1100)
            self.assertEqual(page.printer_cards["3"].grid_info()["row"], 1)
            page.layout_printers(1320)
            self.assertEqual(page.printer_cards["3"].grid_info()["column"], 3)
        finally:
            window.destroy()

    def test_completion_uses_observed_run_when_printer_clock_is_wrong(self):
        from print_farm_observe import observe

        record = {
            "status": "printing",
            "path": "/job.gcode",
            "submitted_at": 1790000000,
            "prior_history_ids": [1],
        }
        run = {
            "id": 2,
            "filename": "/job.gcode",
            "printfinish": 0,
            "starttime": 1500000000,
        }
        snapshot = {
            "status": {"state": 1, "printFileName": "/job.gcode"},
            "history": [run],
        }
        observe(record, snapshot)
        self.assertEqual(record["active_history_id"], 2)
        later = {**run, "id": 3}
        snapshot["history"] = [later]
        observe(record, snapshot)
        self.assertEqual(record["active_history_id"], 2)
        run["printfinish"] = 1
        snapshot["history"] = [later, run]
        snapshot["status"]["state"] = 2
        self.assertEqual(observe(record, snapshot)["status"], "completed")
        self.assertNotIn("bed_cleared_at", record)
        unobserved = {
            "status": "printing",
            "path": "/job.gcode",
            "submitted_at": 1790000000,
            "prior_history_ids": [1],
        }
        self.assertEqual(observe(unobserved, snapshot)["status"], "printing")

    def test_unbound_job_uses_unique_submission_time_and_preserves_ambiguous_history(self):
        from print_farm_observe import observe

        record = {
            "status": "submission_uncertain",
            "path": "/job.gcode",
            "submitted_at": 1790000000,
            "prior_history_ids": [],
        }
        original = {"id": 2, "filename": "/job.gcode", "printfinish": 1,
                    "starttime": record["submitted_at"] + 2}
        unrelated = {**original, "id": 3, "printfinish": 0,
                     "starttime": record["submitted_at"] + 400}
        snapshot = {"status": {"state": 0}, "history": [unrelated]}
        self.assertEqual(observe(dict(record), snapshot)["status"], "submission_uncertain")
        snapshot["history"] = [unrelated, original]
        self.assertEqual(observe(dict(record), snapshot)["status"], "completed")
        unrelated["starttime"] = record["submitted_at"] + 3
        self.assertEqual(observe(dict(record), snapshot)["status"], "submission_uncertain")

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

    def test_automatic_overlay_uses_inspected_frame_and_falls_back_when_scene_changes(self):
        import tkinter as tk

        window = tk.Tk()
        window.withdraw()
        live = LiveCameras(lambda: {})
        now = time.time()
        raw, analyzed = picture("blue"), picture("red")
        live.frames["p"] = {"bytes": raw, "at": now, "changed_at": now, "signature": bytes(576)}
        analysis = {
            "frame": analyzed,
            "captured_at": now,
            "checked_at": now,
            "state": "occupied",
            "confidence": 0.9,
            "objects": [{"label": "part", "box": [0, 0, 500, 500]}],
            "signature": bytes(576),
        }
        try:
            tile = CameraTile(window, live, "p")
            tile.after_cancel(tile.timer)
            tile.refresh(force=True)
            self.assertEqual(tile.last_key[0], hash(raw))
            self.assertFalse(
                any(tile.canvas.type(i) == "rectangle" for i in tile.canvas.find_all())
            )
            tile.after_cancel(tile.timer)
            live.results["p"] = analysis
            tile.refresh(force=True)
            self.assertEqual(tile.last_key[0], hash(analyzed))
            self.assertTrue(
                any(tile.canvas.type(i) == "rectangle" for i in tile.canvas.find_all())
            )
            tile.after_cancel(tile.timer)
            tile.unload_required = True
            analysis.update(state="clear", reason="Bed looks empty")
            tile.refresh(force=True)
            self.assertIn("Printer finished", tile.detected.cget("text"))
            self.assertEqual(tile.reason.cget("text"), "")
            tile.unload_required = False
            tile.after_cancel(tile.timer)
            live.frames["p"]["signature"] = bytes([255]) * 576
            tile.refresh(force=True)
            self.assertEqual(tile.last_key[0], hash(raw))
            self.assertFalse(tile.canvas.find_withtag("detection"))
            self.assertIn("waiting for an updated scan", tile.detected.cget("text"))
            tile.after_cancel(tile.timer)
            live.frames["p"]["changed_at"] = now - 20
            tile.refresh(force=True)
            self.assertIsNone(tile.photo)
            self.assertIn("waiting for a live camera", tile.detected.cget("text"))
            self.assertEqual(tile.canvas.coords(tile.canvas.find_all()[0]),
                             [tile.canvas.winfo_width() / 2, tile.canvas.winfo_height() / 2])
        finally:
            window.destroy()


if __name__ == "__main__":
    unittest.main()
