import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import print_farm as farm


def row():
    return {
        "order_id": "order-1",
        "line_id": "line-1",
        "created": "2026-11-07T00:00:00Z",
        "modified": "2026-11-07T00:00:00Z",
        "payment": "PAID",
        "order_fulfillment": "NOT_STARTED",
        "cancel_state": "NONE_REQUESTED",
        "order_refund": False,
        "line_refund": False,
        "fulfillment": "NOT_STARTED",
        "sku": "demo",
        "item_id": "12345",
        "variation_id": "",
        "aspects": [],
        "quantity": 2,
        "title": "Test stand black PLA",
    }


def api_order(item):
    return {
        "orderId": item["order_id"],
        "creationDate": item["created"],
        "lastModifiedDate": item["modified"],
        "orderPaymentStatus": item["payment"],
        "orderFulfillmentStatus": item["order_fulfillment"],
        "cancelStatus": {"cancelState": item["cancel_state"]},
        "lineItems": [
            {
                "lineItemId": item["line_id"],
                "lineItemFulfillmentStatus": item["fulfillment"],
                "sku": item["sku"],
                "legacyItemId": item["item_id"],
                "quantity": item["quantity"],
                "title": item["title"],
            }
        ],
    }


def clear():
    return {
        "bed_fully_visible": True,
        "bed_clear": True,
        "person_or_hand": False,
        "object_present": False,
        "confidence": 0.99,
        "reason": "Empty plate",
    }


class Decisions(unittest.TestCase):
    def test_update_waits_for_busy_printers_and_unsettled_submissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = farm.Service(temporary)
            service.configure({"printers": [{"id": "printer"}]})
            self.assertIn("fresh printer status", service.update_restart_blocker(reserve=True))
            service._printers_checked_at = time.monotonic()
            service._unsettled_jobs = False
            service.snapshot["printers"] = [{"id": "printer", "online": True, "idle": False}]
            self.assertIn("active print", service.update_restart_blocker(reserve=True))
            service.snapshot["printers"][0]["idle"] = True
            service._unsettled_jobs = True
            self.assertIn("active print", service.update_restart_blocker(reserve=True))
            self.assertFalse(service._update_reserved)
            service._unsettled_jobs = False
            self.assertEqual(service.update_restart_blocker(reserve=True), "")
            self.assertTrue(service._update_reserved)

    def test_update_reservation_freezes_real_service_loop_until_canceled(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = farm.Service(temporary)
            first_cycle = threading.Event()
            release_cycle = threading.Event()
            second_cycle = threading.Event()

            def cycle(_store):
                if first_cycle.is_set():
                    second_cycle.set()
                else:
                    first_cycle.set()
                    self.assertTrue(release_cycle.wait(timeout=5))

            with patch.object(service, "cycle", side_effect=cycle):
                service.start()
                try:
                    self.assertTrue(first_cycle.wait(timeout=5))
                    self.assertIn("checking jobs", service.update_restart_blocker(reserve=True))
                    release_cycle.set()
                    with service._cycle_lock:
                        pass  # The first service iteration has committed its status.
                    self.assertEqual(service.update_restart_blocker(reserve=True), "")
                    service.wake.set()
                    self.assertFalse(second_cycle.wait(timeout=0.2))
                    service.cancel_update_restart()
                    self.assertTrue(second_cycle.wait(timeout=5))
                finally:
                    release_cycle.set()
                    service.stop()
                    service.thread.join(timeout=5)
                self.assertFalse(service.thread.is_alive())

    def test_finished_printer_is_available_but_paused_is_not(self):
        from print_farm_transport import ready_state

        self.assertTrue(ready_state({"state": 2, "printProgress": 100}))
        self.assertFalse(ready_state({"state": 2, "printProgress": 80}))
        self.assertFalse(ready_state({"state": 5, "printProgress": 100}))

    def test_strict_file_threshold(self):
        self.assertFalse(farm.can_auto({"confidence": 0.50}))
        self.assertFalse(farm.can_auto({"confidence": 0.49}))
        self.assertTrue(farm.can_auto({"confidence": 0.5001}))

    def test_invalid_confidence(self):
        for value in [True, "0.99", float("nan"), float("inf"), -1, 101]:
            with self.assertRaises(ValueError):
                farm.confidence(value)

    def test_clear_is_independent_of_file_match(self):
        self.assertTrue(farm.valid_clear(clear()))
        for field, value in [
            ("bed_clear", False),
            ("bed_fully_visible", False),
            ("person_or_hand", True),
            ("object_present", True),
            ("confidence", 0.94),
        ]:
            self.assertFalse(farm.valid_clear({**clear(), field: value}))

    def test_frozen_camera_is_not_clear(self):
        infer = Mock(return_value=clear())
        watcher = farm.BedWatcher(
            capture=lambda _: b"same", infer=infer, sleep=lambda _: None
        )
        self.assertEqual(
            watcher.check({"id": "p", "camera_url": "http://camera"}, "vision")[
                "state"
            ],
            "unknown",
        )
        infer.assert_not_called()

    def test_removal_changes_occupied_to_clear_with_new_ticket(self):
        frames = iter([b"a", b"b", b"c", b"d"])
        results = iter(
            [{**clear(), "object_present": True, "bed_clear": False}, clear()]
        )
        watcher = farm.BedWatcher(
            capture=lambda _: next(frames),
            infer=lambda *a, **k: next(results),
            sleep=lambda _: None,
        )
        p = {"id": "p", "camera_url": "http://camera"}
        occupied = watcher.check(p, "vision")
        empty = watcher.check(p, "vision")
        self.assertEqual(occupied["state"], "blocked")
        self.assertEqual(empty["state"], "clear")
        self.assertNotEqual(empty["ticket_id"], occupied["ticket_id"])

    def test_slow_inference_expires_bed_check(self):
        now = [100]
        frames = iter([b"a", b"b"])

        def infer(*a, **k):
            now[0] += 31
            return clear()

        watcher = farm.BedWatcher(
            capture=lambda _: next(frames),
            infer=infer,
            clock=lambda: now[0],
            sleep=lambda _: None,
        )
        self.assertEqual(
            watcher.check({"id": "p", "camera_url": "http://camera"}, "vision")[
                "state"
            ],
            "unknown",
        )

    def test_camera_failure_blocks(self):
        watcher = farm.BedWatcher(
            capture=Mock(side_effect=TimeoutError), sleep=lambda _: None
        )
        self.assertEqual(
            watcher.check({"id": "p", "camera_url": "http://camera"}, "vision")[
                "state"
            ],
            "unknown",
        )


class Production(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = farm.Service(self.root)
        self.store = farm.Store(self.root / "queue.sqlite")
        self.ledger = self.root / "ledger"
        self.ledger.mkdir()
        self.printer = {
            "id": "p",
            "hostname": "printer",
            "idle": True,
            "driver": "creality_k1_se",
            "ip": "192.168.1.10",
            "color": "black",
        }
        self.settings = {
            "enabled": True,
            "account_scope": "account-a",
            "printers": [dict(self.printer)],
        }
        self.service.configure(self.settings)
        self.recipe = {
            "id": "r",
            "printer_id": "p",
            "name": "stand.gcode",
            "file_fingerprint": "f" * 64,
            "color": "black",
            "content_sha256": "a" * 64,
        }
        self.proposal = {
            "confidence": 0.8,
            "recipes": [self.recipe],
            "runs_per_sale": 1,
            "token": "t",
        }
        self.item = row()
        self.key = self.store.ingest(self.item)
        self.store.decision(self.key, self.proposal)
        self.client = Mock()
        self.client.order.return_value = api_order(self.item)
        self.service.beds.check = Mock(
            side_effect=lambda *a, **k: {
                "state": "clear",
                "captured_at": time.time(),
                "ticket_id": "a" * 32,
            }
        )
        self.machine = Mock()
        self.machine.start_order.return_value = {"status": "submission_uncertain"}
        self.transport = patch.object(farm, "K1", return_value=self.machine)
        self.transport.start()
        self.release = patch.object(farm, "release_after_bed_check")
        self.release.start()

    def tearDown(self):
        self.transport.stop()
        self.release.stop()
        self.store.db.close()
        self.temp.cleanup()

    def dispatch(self, unit=0):
        return self.service.dispatch(
            self.store,
            self.key,
            self.item,
            unit,
            self.proposal,
            [self.printer],
            self.client,
            self.settings,
            self.ledger,
        )

    def test_over_threshold_dispatches_once(self):
        self.assertEqual(self.dispatch()["status"], "printing")
        self.printer["idle"] = True
        self.dispatch()
        self.machine.start_order.assert_called_once()

    def test_pending_update_holds_new_jobs_and_releases_them_when_canceled(self):
        self.service.set_update_drain(True)
        self.assertEqual(self.dispatch()["status"], "queued")
        self.service.beds.check.assert_not_called()
        self.machine.start_order.assert_not_called()
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM jobs").fetchone())

        self.service.set_update_drain(False)
        self.assertEqual(self.dispatch()["status"], "printing")
        self.machine.start_order.assert_called_once()

    def test_update_during_preflight_keeps_job_queued_without_reserving_it(self):
        def lookup(_order_id):
            if self.client.order.call_count == 2:
                self.service.set_update_drain(True)
            return api_order(self.item)

        self.client.order.side_effect = lookup
        self.assertEqual(self.dispatch()["status"], "queued")
        self.service.beds.check.assert_called_once()
        self.machine.start_order.assert_not_called()
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM jobs").fetchone())

    def test_low_confidence_needs_ok(self):
        self.proposal["confidence"] = 0.5
        self.store.decision(self.key, self.proposal)
        self.dispatch()
        self.machine.start_order.assert_not_called()
        self.store.approve(self.key, "t")
        self.assertEqual(self.dispatch()["status"], "printing")

    def test_order_change_invalidates_ok(self):
        self.store.approve(self.key, "t")
        self.store.ingest({**self.item, "quantity": 3})
        saved = self.store.db.execute(
            "SELECT decision,approved_token FROM orders"
        ).fetchone()
        self.assertEqual(tuple(saved), (None, None))

    def test_stale_ok_rejected(self):
        with self.assertRaises(farm.Hold):
            self.store.approve(self.key, "old-token")

    def test_occupied_bed_blocks_even_after_ok(self):
        self.store.approve(self.key, "t")
        self.service.beds.check.return_value = None
        self.service.beds.check.side_effect = lambda *a, **k: {"state": "blocked"}
        self.dispatch()
        self.machine.start_order.assert_not_called()

    def test_pause_during_vision_wins(self):
        def bed(*a, **k):
            self.service.configure({"enabled": False})
            return {"state": "clear", "captured_at": time.time(), "ticket_id": "a" * 32}

        self.service.beds.check.side_effect = bed
        self.dispatch()
        self.machine.start_order.assert_not_called()

    def test_account_switch_during_vision_wins(self):
        def bed(*a, **k):
            self.service.configure({"account_scope": "another-account"})
            return {"state": "clear", "captured_at": time.time(), "ticket_id": "a" * 32}

        self.service.beds.check.side_effect = bed
        self.dispatch()
        self.machine.start_order.assert_not_called()

    def test_cancelled_order_never_starts(self):
        order = api_order(self.item)
        order["cancelStatus"]["cancelRequests"] = [{"id": "cancel"}]
        self.client.order.return_value = order
        self.dispatch()
        self.machine.start_order.assert_not_called()

    def test_timeout_after_send_is_not_retried(self):
        def uncertain(jid, *args):
            farm.persist(
                self.ledger / (jid + ".json"), {"status": "submission_uncertain"}
            )
            raise TimeoutError()

        self.machine.start_order.side_effect = uncertain
        self.assertEqual(self.dispatch()["status"], "submission_uncertain")
        self.dispatch()
        self.machine.start_order.assert_called_once()

    def test_existing_legacy_completion_is_preserved(self):
        self.service.prior_status = Mock(return_value="completed")
        self.dispatch()
        self.machine.start_order.assert_not_called()

    def test_reserved_job_survives_restart(self):
        jid = farm.job_id(self.item, 0)
        self.assertTrue(self.store.reserve(jid, self.key, 0, self.recipe))
        other = farm.Store(self.root / "queue.sqlite")
        self.assertFalse(other.reserve(jid, self.key, 0, self.recipe))
        other.db.close()

    def test_stable_job_ids_match_previous_helper(self):
        import uuid

        expected = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, "cale-print:" + json.dumps(["order-1", "line-1", 0])
            )
        )
        self.assertEqual(farm.job_id(self.item, 0), expected)


class Page(unittest.TestCase):
    def test_native_page_has_single_ok_and_sends_exact_approval(self):
        import tkinter as tk
        from types import SimpleNamespace

        from print_farm_ui import Controls

        window = tk.Tk()
        window.withdraw()
        settings = {"enabled": False}
        service = SimpleNamespace(
            root=Path("."),
            snapshot={"connected": True, "printers": [], "orders": []},
            settings=lambda: settings,
            configure=lambda update: settings.update(update),
            start=Mock(),
            stop=Mock(),
            approve=Mock(),
        )
        try:
            parent = tk.Frame(window)
            parent.pack()
            controls = Controls(parent, window, Path("."), service=service)
            controls.render(
                {
                    "printers": [],
                    "orders": [
                        {
                            "key": "line",
                            "order_id": "order",
                            "title": "Bracket",
                            "quantity": 1,
                            "confidence": 0.49,
                            "filename": "bracket.gcode",
                            "status": "approval",
                            "token": "exact",
                            "proposal": {},
                            "reason": "Uncertain version",
                            "total": 1,
                            "completed": 0,
                        }
                    ],
                }
            )

            def widgets(widget):
                yield widget
                for child in widget.winfo_children():
                    yield from widgets(child)

            buttons = [
                w
                for w in widgets(parent)
                if w.winfo_class() == "TButton" and str(w.cget("text")) == "OK"
            ]
            self.assertEqual(len(buttons), 1)
            buttons[0].invoke()
            service.approve.assert_called_once_with("line", "exact")
            self.assertIn("disabled", buttons[0].state())
            controls.toggle_printing()
            self.assertTrue(settings["enabled"])
            controls.toggle_printing()
            self.assertFalse(settings["enabled"])
            controls.render(
                {
                    "orders": [
                        {
                            "key": "x",
                            "order_id": "x",
                            "title": "No file",
                            "quantity": 1,
                            "status": "needs_file",
                            "proposal": None,
                        }
                    ]
                }
            )
        finally:
            window.destroy()


if __name__ == "__main__":
    unittest.main()
