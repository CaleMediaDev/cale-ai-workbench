"""Read-only print reconciliation and explicit bed-clear lock release."""

import json
import time
import uuid
from pathlib import Path

from print_farm_transport import Hold, persist, ready_state


def observe(record, client):
    if record.get("status") not in ("submission_uncertain", "printing"):
        return record
    snap = client.inspect()
    status = snap["status"]
    newer = [
        h
        for h in snap["history"]
        if h.get("filename") == record["path"]
        and h.get("id") not in record.get("prior_history_ids", [])
    ]
    # A later job can replace the current display before this worker reconnects.
    # Its display cannot override a successful history entry for this exact run.
    # Completion never clears the bed lock or authorizes another print.
    finished_run = (
        len(newer) == 1
        and newer[0].get("printfinish") == 1
        and (
            (
                record.get("active_history_id") is not None
                and newer[0].get("id") == record["active_history_id"]
            )
            or (
                type(record.get("submitted_at")) in (int, float)
                and type(newer[0].get("starttime")) in (int, float)
                and abs(newer[0]["starttime"] - record["submitted_at"]) < 30
            )
        )
    )
    if finished_run:
        record["status"] = "completed"
    elif (
        int(status.get("state", -1)) in (0, 3, 4)
        and len(newer) == 1
        and newer[0].get("printfinish") != 1
    ):
        record["status"] = "failed"
    elif (
        int(status.get("state", -1)) in (1, 5)
        and status.get("printFileName", "").rsplit("/", 1)[-1]
        == record["path"].rsplit("/", 1)[-1]
    ):
        record["status"] = "printing"
        # Some LAN printers retain an old internal clock. Bind the new history
        # entry observed while this exact file is running, rather than requiring
        # that firmware clock to match the PC when the run completes.
        if (
            len(newer) == 1
            and newer[0].get("id") is not None
            and newer[0].get("printfinish") != 1
        ):
            record["active_history_id"] = newer[0]["id"]
    record["last_observed_at"] = time.time()
    return record


def release_after_bed_check(ledger, printer_id, authorization, client):
    """Release only a terminal job's lock after a newer, explicit physical check."""
    ledger = Path(ledger)
    lock = ledger / (printer_id + ".lock")
    if not lock.exists():
        return
    guard = ledger / (printer_id + ".release-lock")
    persist(guard, {"ticket_id": authorization["ticket_id"]}, exclusive=True)
    try:
        locked = json.loads(lock.read_text())
        jid = locked["job_id"]
        if str(uuid.UUID(jid)) != jid:
            raise Hold("Invalid prior job lock")
        record_path = ledger / (jid + ".json")
        record = json.loads(record_path.read_text())
        if (
            record.get("printer_id") != printer_id
            or record.get("status") not in ("completed", "failed")
            or not record.get("last_observed_at")
            or authorization.get("confirmed_at", 0) <= record["last_observed_at"]
            or authorization.get("bed_clear") is not True
            or time.time() >= authorization.get("expires_at", 0)
        ):
            raise Hold("Prior print needs reconciliation and a new bed check")
        snap = client.inspect()
        if time.time() - snap["checked_at"] > 15 or not ready_state(snap["status"]):
            raise Hold("Fresh idle status required before releasing the prior bed lock")
        record["bed_cleared_at"] = authorization["confirmed_at"]
        record["bed_clear_ticket"] = authorization["ticket_id"]
        persist(record_path, record)
        lock.unlink()
    finally:
        guard.unlink(missing_ok=True)
