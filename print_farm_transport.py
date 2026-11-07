"""K1 SE LAN transport. Start is explicit, single-shot, and durably recorded.

Protocol source: the printers' own app.b05d1c1a.js; file/history response
shapes verified against all four printers on 2026-10-26. Starts require explicit
test authorization or a claimed one-use bed authorization for a paid order.
"""

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import websocket

PRINTERS = {}  # Populated only from this installation's printer settings.


class Hold(RuntimeError):
    pass


def ready_state(status):
    """Firmware state 2 means completed (from its own LAN UI status table)."""
    return status.get("state") == 0 or (
        status.get("state") == 2 and float(status.get("printProgress", 0)) == 100
    )


def file_fingerprint(file):
    fields = (
        "name",
        "path",
        "file_size",
        "create_time",
        "timeCost",
        "material",
        "nozzleTemp",
        "bedTemp",
    )
    return hashlib.sha256(
        json.dumps({k: file.get(k) for k in fields}, sort_keys=True).encode()
    ).hexdigest()


def validate_file(file):
    name = file.get("name", "")
    if (
        not name.lower().endswith(".gcode")
        or any(c in name for c in "/\\:\r\n\x00")
        or file.get("path") != "/usr/data/printer_data/gcodes/" + name
        or not 0 < int(file.get("file_size", 0)) < 200_000_000
    ):
        raise Hold("Unexpected printer file path or size")


def persist(path, data, exclusive=False):
    encoded = json.dumps(data, indent=2).encode()
    if exclusive:
        with path.open("xb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
    else:
        temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        created = False
        try:
            with temp.open("xb") as f:
                created = True
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
        except OSError:
            if created:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            raise


class K1:
    def __init__(self, printer_id, connect=websocket.create_connection):
        if printer_id not in PRINTERS:
            raise Hold("Unknown printer")
        self.id = printer_id
        self.ip, self.hostname, self.color = PRINTERS[printer_id]
        self.connect = connect
        self.before_submit = lambda: None

    def open(self):
        ws = self.connect("ws://" + self.ip + ":9999", timeout=4)
        try:
            status = self.receive(ws)
            if (
                status.get("model") != "K1 SE"
                or status.get("hostname") != self.hostname
            ):
                raise Hold("Printer identity does not match configuration")
            return ws, status
        except Exception:
            ws.close()
            raise

    @staticmethod
    def receive(ws):
        raw = ws.recv()
        if len(raw) > 8_000_000:
            raise Hold("Oversized printer response")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise Hold("Unexpected printer response")
        return value

    def query(self, ws, request, response, status):
        ws.send(json.dumps({"method": "get", "params": {request: 1}}))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                value = self.receive(ws)
            except websocket.WebSocketTimeoutException:
                continue
            status.update(value)
            if response in value:
                return value[response]
        raise Hold("Printer did not answer the read request")

    def inspect(self, *, include_files=True):
        ws, status = self.open()
        checked_at = time.time()
        try:
            files = self.query(ws, "reqGcodeFile", "retGcodeFileInfo2", status) if include_files else []
            history = self.query(ws, "reqHistory", "historyList", status)
            return {
                "printer_id": self.id,
                "checked_at": checked_at,
                "status": status,
                "files": files,
                "history": history,
            }
        finally:
            ws.close()

    def preflight(self, recipe, snapshot):
        if recipe.get("printer_id") != self.id or recipe.get("color") != self.color:
            raise Hold("Recipe targets another printer or filament color")
        if recipe.get("material") != "PLA" or recipe.get("units_per_run") != 1:
            raise Hold("Only the verified single-unit PLA recipe is supported")
        status = snapshot["status"]
        if time.time() - snapshot["checked_at"] > 15 or not ready_state(status):
            raise Hold("A fresh idle printer is required")
        error = status.get("err")
        if error and (
            not isinstance(error, dict)
            or any(error.get(k) for k in ("errcode", "code", "key"))
        ):
            raise Hold("Printer reports an error")
        matches = [f for f in snapshot["files"] if f.get("path") == recipe.get("path")]
        if len(matches) != 1:
            raise Hold("Recipe file missing or ambiguous")
        file = matches[0]
        validate_file(file)
        if file_fingerprint(file) != recipe.get("file_fingerprint"):
            raise Hold("Saved printer file metadata changed; review again")
        import urllib.parse
        import urllib.request

        digest = recipe.get("content_sha256", "")
        if len(digest) != 64:
            raise Hold("File content has not been verified")
        url = (
            "http://"
            + self.ip
            + "/downloads/gcode/"
            + urllib.parse.quote(file["name"], safe="")
        )
        with urllib.request.urlopen(url, timeout=10) as response:
            content = response.read(file["file_size"] + 1)
        if (
            len(content) != file["file_size"]
            or hashlib.sha256(content).hexdigest() != digest
        ):
            raise Hold("Printer file content changed; match this file again")
        return file

    def start_supervised(self, job_id, recipe, authorization, ledger):
        """Call only after the user approves this exact test and checks the bed.

        A timeout/exception after submission is an unknown outcome, never a retry.
        The printer lock remains until completion is reconciled and its bed cleared.
        """
        if str(uuid.UUID(job_id)) != job_id:
            raise Hold("A canonical job UUID is required")
        if (
            authorization.get("job_id") != job_id
            or authorization.get("printer_id") != self.id
            or authorization.get("recipe_fingerprint") != recipe.get("file_fingerprint")
            or authorization.get("bed_clear") is not True
            or not (
                authorization.get("filament_color") == self.color
                or (
                    self.color == "black"
                    and authorization.get("black_pla_loaded") is True
                )
            )
            or not 0 <= time.time() - authorization.get("confirmed_at", 0) <= 120
        ):
            raise Hold("Fresh explicit test and bed-clear confirmation required")
        if recipe.get("reviewed_for_supervised_test") is not True:
            raise Hold("Recipe has not been reviewed for this test")
        return self._start_once(
            job_id,
            recipe,
            ledger,
            authorization["confirmed_at"] + 120,
            "supervised_test",
        )

    def start_order(self, job_id, recipe, authorization, ledger):
        """Only an approved recipe and a freshly claimed bed ticket may start an order."""
        now = time.time()
        # A server clock a few seconds ahead must not turn a valid claim into
        # a failed print. Wait until its timestamp is current; do not broaden
        # the 30-second freshness window or accept a future authorization.
        ahead = authorization.get("claimed_at", 0) - now
        if 0 < ahead <= 5:
            time.sleep(ahead + 0.05)
            now = time.time()
        if str(uuid.UUID(job_id)) != job_id:
            raise Hold("Canonical job ID required")
        if (
            recipe.get("automatic_orders_enabled") is not True
            or authorization.get("job_id") != job_id
            or authorization.get("printer_id") != self.id
            or authorization.get("recipe_fingerprint") != recipe.get("file_fingerprint")
            or authorization.get("bed_clear") is not True
            or authorization.get("filament_color") != self.color
            or not isinstance(authorization.get("ticket_id"), str)
            or len(authorization["ticket_id"]) != 32
            or not 0 <= now - authorization.get("claimed_at", 0) <= 30
            or not 0 <= now - authorization.get("confirmed_at", 0) <= 3600
            or not now
            < authorization.get("expires_at", 0)
            <= authorization["confirmed_at"] + 3600
        ):
            raise Hold("Fresh claimed bed authorization and approved recipe required")
        deadline = min(authorization["expires_at"], authorization["claimed_at"] + 120)
        return self._start_once(
            job_id, recipe, ledger, deadline, "ebay_order", authorization["ticket_id"]
        )

    def _start_once(self, job_id, recipe, ledger, deadline, source, ticket_id=None):
        ledger = Path(ledger)
        ledger.mkdir(parents=True, exist_ok=True)
        job_path = ledger / (job_id + ".json")
        lock_path = ledger / (self.id + ".lock")
        # Exclusive durable files prevent duplicate submission across processes/restarts.
        persist(lock_path, {"job_id": job_id, "printer_id": self.id}, exclusive=True)
        ws = None
        submitted = False
        try:
            persist(
                job_path,
                {
                    "status": "checking",
                    "printer_id": self.id,
                    "job_id": job_id,
                    "source": source,
                },
                exclusive=True,
            )
            ws, status = self.open()
            checked_at = time.time()
            files = self.query(ws, "reqGcodeFile", "retGcodeFileInfo2", status)
            history = self.query(ws, "reqHistory", "historyList", status)
            file = self.preflight(
                recipe,
                {
                    "status": status,
                    "files": files,
                    "history": history,
                    "checked_at": checked_at,
                },
            )
            if time.time() > deadline:
                raise Hold("Bed-clear confirmation expired during checks")
            self.before_submit()
            record = {
                "job_id": job_id,
                "printer_id": self.id,
                "path": file["path"],
                "recipe_fingerprint": recipe["file_fingerprint"],
                "source": source,
                "ticket_id": ticket_id,
                "status": "submission_uncertain",
                "submitted_at": time.time(),
                "prior_history_ids": [h.get("id") for h in history],
            }
            persist(job_path, record)
            submitted = True
            ws.send(
                json.dumps(
                    {
                        "method": "set",
                        "params": {"opGcodeFile": "printprt:" + file["path"]},
                    }
                )
            )
            # Sending succeeds independently of the printer accepting the job.
            # A later observer must confirm state/history; never resend automatically.
            return record
        except Exception:
            if not submitted:
                lock_path.unlink(missing_ok=True)
            raise
        finally:
            if ws is not None:
                ws.close()
