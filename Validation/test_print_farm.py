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
    def test_printer_read_rejects_late_reply_and_restores_socket_timeout(self):
        from print_farm_transport import Hold, K1, PRINTERS

        with patch.dict(PRINTERS, {"p": ("192.0.2.1", "fixture", "black")}, clear=True):
            printer = K1("p")
        socket = Mock()
        socket.gettimeout.return_value = 4
        clock = [100.0]
        status = {}

        def receive(_socket):
            clock[0] += socket.settimeout.call_args.args[0]
            return {"state": 1} if clock[0] < 105 else {"historyList": [{"id": 1}]}

        with (patch("print_farm_transport.time.monotonic", side_effect=lambda: clock[0]),
              patch.object(printer, "receive", side_effect=receive)):
            with self.assertRaisesRegex(Hold, "did not answer"):
                printer.query(socket, "reqHistory", "historyList", status, deadline=105)
        self.assertEqual(status, {"state": 1})
        self.assertEqual([call.args[0] for call in socket.settimeout.call_args_list], [4, 4, 1, 4])
        socket.send.assert_called_once()

    def test_bed_authorization_expiring_during_ledger_write_does_not_send(self):
        from print_farm_transport import Hold, K1, PRINTERS, persist

        with patch.dict(PRINTERS, {"p": ("192.0.2.1", "fixture", "black")}, clear=True):
            printer = K1("p")
        socket = Mock()
        clock = [100.0]
        job_id = "00000000-0000-4000-8000-000000000001"
        recipe = {"file_fingerprint": "fixture", "automatic_orders_enabled": True}
        ticket = {
            "job_id": job_id, "printer_id": "p", "file_fingerprint": "fixture",
            "recipe_fingerprint": "fixture", "bed_clear": True, "filament_color": "black",
            "ticket_id": "a" * 32, "confirmed_at": 99, "claimed_at": 100, "expires_at": 104,
        }

        def save(path, data, **options):
            persist(path, data, **options)
            if data.get("status") == "submission_uncertain":
                clock[0] = 104.0

        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary)
            with (patch("print_farm_transport.time.time", side_effect=lambda: clock[0]),
                  patch("print_farm_transport.time.monotonic", side_effect=lambda: clock[0]),
                  patch("print_farm_transport.persist", side_effect=save),
                  patch.object(printer, "open", return_value=(socket, {})),
                  patch.object(printer, "query", return_value=[]) as query,
                  patch.object(printer, "preflight", return_value={"path": "/fixture.gcode"})):
                with self.assertRaisesRegex(Hold, "expired before submission"):
                    printer.start_order(job_id, recipe, ticket, ledger)
            self.assertEqual([call.kwargs["deadline"] for call in query.call_args_list], [104, 104])
            self.assertEqual(json.loads((ledger / (job_id + ".json")).read_text())["status"], "held")
            self.assertFalse((ledger / "p.lock").exists())
        socket.send.assert_not_called()
        socket.close.assert_called_once_with()

    def test_history_poll_skips_file_inventory_and_closes_connection(self):
        from print_farm_transport import K1, PRINTERS

        with patch.dict(PRINTERS, {"p": ("192.0.2.1", "fixture", "black")}, clear=True):
            printer = K1("p")
        socket = Mock()
        state = {"state": 2}
        history = [{"id": 1}]
        with (patch.object(printer, "open", return_value=(socket, state)),
              patch.object(printer, "query", return_value=history) as query):
            snapshot = printer.inspect(include_files=False)
        query.assert_called_once_with(socket, "reqHistory", "historyList", state)
        self.assertEqual(snapshot["history"], history)
        socket.close.assert_called_once_with()

    def test_healthy_printer_reconciles_while_another_history_read_times_out(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = farm.Service(root)
            store = farm.Store(root / "queue.sqlite")
            saved = threading.Event()
            fast_committed_before_timeout = []
            now = time.time()
            for printer_id in ("slow", "fast"):
                store.reserve(printer_id, printer_id, 0, {"printer_id": printer_id})
                farm.persist(root / (printer_id + ".json"), {
                    "status": "submission_uncertain", "path": "/" + printer_id + ".gcode",
                    "submitted_at": now,
                })
            original = farm.persist

            def save_result(path, data):
                original(path, data)
                if path.stem == "fast":
                    saved.set()

            def history(printer_id):
                if printer_id == "slow":
                    fast_committed_before_timeout.append(saved.wait(timeout=3))
                    raise TimeoutError("Fixture printer did not answer")
                return {
                    "status": {"state": 2},
                    "history": [{"id": 8, "filename": "/fast.gcode", "starttime": now, "printfinish": 1}],
                }

            def printer(printer_id):
                return Mock(inspect=Mock(side_effect=lambda **_: history(printer_id)))

            try:
                with (patch.object(farm, "K1", side_effect=printer),
                      patch.object(farm, "persist", side_effect=save_result)):
                    service.reconcile(store, root)
                self.assertEqual(fast_committed_before_timeout, [True])
                statuses = dict(store.db.execute("SELECT id,status FROM jobs").fetchall())
                self.assertEqual(statuses, {"slow": "dispatching", "fast": "completed"})
            finally:
                store.db.close()

    def test_status_sharing_violation_does_not_stop_print_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = farm.Service(temporary)
            original = farm.persist
            failed = threading.Event()
            recovered = threading.Event()

            def write_status(path, snapshot):
                if not failed.is_set():
                    failed.set()
                    service.wake.set()
                    raise PermissionError("Status snapshot temporarily locked")
                original(path, snapshot)
                recovered.set()

            with (patch.object(service, "cycle") as cycle,
                  patch.object(farm, "persist", side_effect=write_status)):
                service.start()
                try:
                    self.assertTrue(recovered.wait(timeout=5))
                    self.assertTrue(service.thread.is_alive())
                    self.assertGreaterEqual(cycle.call_count, 2)
                    self.assertTrue((service.root / "status.json").is_file())
                finally:
                    service.stop()
                    service.thread.join(timeout=5)
                self.assertFalse(service.thread.is_alive())

    def test_failed_atomic_replace_preserves_previous_file_and_removes_its_temp(self):
        from print_farm_transport import persist

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "status.json"
            persist(path, {"progress": 50})
            with patch("print_farm_transport.os.replace", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    persist(path, {"progress": 60})
            self.assertEqual(json.loads(path.read_text()), {"progress": 50})
            self.assertEqual(list(path.parent.iterdir()), [path])

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

    def test_update_waits_for_a_busy_printer_across_failed_polls(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = farm.Service(temporary)
            service.configure({"printers": [{"id": "printer"}]})
            store = farm.Store(Path(temporary) / "queue.sqlite")
            service._unsettled_jobs = False
            samples = [
                {"id": "printer", "online": True, "idle": False},
                {"id": "printer", "online": False, "idle": False},
                {"id": "printer", "online": False, "idle": False},
                {"id": "printer", "online": True, "idle": True},
            ]
            try:
                with (patch.object(service, "scan_printers", side_effect=[[sample] for sample in samples]),
                      patch.object(service, "account_client", return_value=None)):
                    service.cycle(store)
                    self.assertIn("active print", service.update_restart_blocker(reserve=True))
                    for _ in range(2):
                        service.cycle(store)
                        self.assertTrue(service.snapshot["printers"][0]["busy_unconfirmed"])
                        self.assertIn("reconnect", service.update_restart_blocker(reserve=True))
                    service.cycle(store)
                    self.assertNotIn("busy_unconfirmed", service.snapshot["printers"][0])
                    self.assertEqual(service.update_restart_blocker(reserve=True), "")
            finally:
                store.db.close()

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

    def test_hidden_bed_is_distinct_from_occupied(self):
        frames = iter([b"a", b"b"])
        watcher = farm.BedWatcher(
            capture=lambda _: next(frames),
            infer=lambda *args, **kwargs: {**clear(), "bed_fully_visible": False},
            sleep=lambda _: None,
        )
        result = watcher.check({"id": "p", "camera_url": "http://camera"}, "vision")
        self.assertEqual(result["state"], "out_of_view")

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

    def test_slow_camera_capture_expires_before_vision_is_called(self):
        now = [100]
        durations = iter([(b"a", 19), (b"b", 10)])
        infer = Mock(return_value=clear())

        def capture(_):
            image, duration = next(durations)
            now[0] += duration
            return image

        def sleep(seconds):
            now[0] += seconds

        watcher = farm.BedWatcher(capture=capture, infer=infer, clock=lambda: now[0], sleep=sleep)
        result = watcher.check({"id": "p", "camera_url": "http://camera"}, "vision")
        self.assertEqual(result["state"], "unknown")
        infer.assert_not_called()

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

    def test_manual_bed_check_is_read_only_and_coalesces_requests(self):
        self.printer.update(online=True, camera_url="http://camera")
        self.settings["printers"] = [dict(self.printer)]
        self.service.configure(self.settings)

        def inspect(printer, _model):
            self.assertEqual(self.service.bed_check_states(), {"p": "checking"})
            self.service.request_bed_check(printer["id"])
            return {"state": "clear", "ticket_id": "fixture"}

        self.service.beds.check.side_effect = inspect
        for _ in range(2):
            self.service.request_bed_check(self.printer["id"])
        self.assertEqual(self.service.bed_check_states(), {"p": "queued"})
        self.service.inspect_requested_bed([self.printer], self.settings, self.store)
        self.service.inspect_requested_bed([self.printer], self.settings, self.store)
        self.service.beds.check.assert_called_once()
        self.assertEqual(self.printer["bed"]["state"], "clear")
        self.assertNotIn("ticket_id", self.printer["bed"])
        self.assertEqual(self.service.bed_check_states(), {})
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM jobs").fetchone())
        self.machine.start_order.assert_not_called()

        self.printer["idle"] = False
        self.service.request_bed_check(self.printer["id"])
        self.service.inspect_requested_bed([self.printer], self.settings, self.store)
        self.service.beds.check.assert_called_once()
        self.assertEqual(self.printer["bed"]["state"], "skipped_busy")

    def test_manual_check_waits_for_fresh_status_and_discards_a_replaced_camera(self):
        self.printer.update(online=True, camera_url="http://camera")
        self.settings["printers"] = [dict(self.printer)]
        self.service.configure(self.settings)
        self.service.request_bed_check("p")
        self.service._printers_checked_at = time.monotonic() - farm.PRINTER_TELEMETRY_MAX_AGE - 1
        self.service.inspect_requested_bed([self.printer], self.settings, self.store)
        self.assertEqual(self.service.bed_check_states(), {"p": "queued"})
        self.service.beds.check.assert_not_called()

        def inspect(*_):
            self.service.configure({"printers": [{**self.printer, "camera_url": "http://new-camera"}]})
            return {"state": "clear", "ticket_id": "fixture"}

        self.service._printers_checked_at = time.monotonic()
        self.service.beds.check.side_effect = inspect
        self.service.inspect_requested_bed([self.printer], self.settings, self.store)
        self.assertEqual(self.printer["bed"]["state"], "skipped_camera_changed")
        self.assertEqual(self.service.bed_check_states(), {})

    def test_camera_change_after_reservation_prevents_submission(self):
        send = Mock()

        def start(*_):
            self.service.configure({"printers": [{**self.printer, "camera_url": "http://new-camera"}]})
            self.machine.before_submit()
            send()

        self.machine.start_order.side_effect = start
        result = self.dispatch()
        self.assertEqual(result["status"], "held")
        self.assertIn("camera changed", result["reason"])
        send.assert_not_called()

    def test_ebay_outage_keeps_saved_progress_visible_and_dispatch_waits_for_recovery(self):
        self.proposal["reason"] = "Saved file match"
        self.store.decision(self.key, self.proposal)
        jid = farm.job_id(self.item, 0)
        self.store.reserve(jid, self.key, 0, self.recipe)
        self.store.outcome(jid, "completed")
        self.client.orders.side_effect = [TimeoutError("Fixture eBay timeout"), []]
        with (patch.object(self.service, "account_client", return_value=self.client),
              patch.object(self.service, "scan_printers", return_value=[self.printer]),
              patch.object(self.service, "catalog", return_value=[self.recipe]) as catalog,
              patch.object(self.service, "dispatch", return_value={"status": "printing"}) as dispatch):
            self.service.cycle(self.store)
            self.assertIn("showing saved orders", self.service.snapshot["status"])
            self.assertEqual(self.service.snapshot["orders"][0]["completed"], 1)
            self.assertEqual(self.service.snapshot["orders"][0]["status"], "queued")
            self.service.cycle(self.store)
            self.client.orders.assert_called_once()
            catalog.assert_not_called()
            dispatch.assert_not_called()
            self.assertIsNone(self.store.db.execute("SELECT 1 FROM meta WHERE key='checkpoint'").fetchone())

            self.service.last_poll = 0
            self.service.cycle(self.store)
            self.assertEqual(self.service.snapshot["status"], "Automatic printing on")
            dispatch.assert_called_once()
            self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM meta WHERE key='checkpoint'").fetchone())

    def test_expiry_during_order_refresh_leaves_job_queued_for_fresh_camera_check(self):
        now = [100]

        def lookup(_):
            if self.client.order.call_count == 2:
                now[0] = 130
            return api_order(self.item)

        self.client.order.side_effect = lookup
        with patch.object(farm.time, "time", side_effect=lambda: now[0]):
            self.assertEqual(self.dispatch()["status"], "queued")
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM jobs").fetchone())
        self.machine.start_order.assert_not_called()

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

    def test_locked_dispatch_record_reconciles_without_resending(self):
        jid = farm.job_id(self.item, 0)
        path = self.ledger / (jid + ".json")
        sent_at = time.time()

        def uncertain(*_args):
            farm.persist(path, {
                "status": "submission_uncertain",
                "path": "/job.gcode",
                "submitted_at": sent_at,
                "prior_history_ids": [],
            })
            raise TimeoutError()

        self.machine.start_order.side_effect = uncertain
        with patch.object(farm, "read_dispatch_record", side_effect=PermissionError("locked")):
            self.assertEqual(self.dispatch()["status"], "printing")
            self.service.reconcile(self.store, self.ledger)
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs").fetchone()[0], "dispatching")
        self.machine.inspect.assert_not_called()
        self.dispatch()
        self.machine.start_order.assert_called_once()
        self.machine.inspect.return_value = {
            "status": {"state": 2},
            "history": [{"id": 8, "filename": "/job.gcode", "starttime": sent_at, "printfinish": 1}],
        }
        self.service.reconcile(self.store, self.ledger)
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs").fetchone()[0], "completed")
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
            bed_check_states=lambda: {},
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
