"""Queue rows that outlived their file: the startup self-heal, the in-tray's
ordering, and the bulk clear.

Six live queue rows pointed at files that no longer existed — other subsystems
(the junk filter's move to _Filtered, chiefly) moved the file out from under the
pending_scans row without removing the row. _repair_queue_paths re-links what it
safely can on boot; the in-tray sinks whatever is left; the clear route removes
the rows (and only the rows — no document is ever deleted).
"""
import json
import os
import shutil
from datetime import date, timedelta

import database
import app as klr
from conftest import make_pdf


TODAY = date.today()


def _queue_item(env, filename, folder=None, **overrides):
    """A queued-scan dict backed by a real temp PDF, persisted the way the
    ingest path persists it (memory + DB), so repairs can be checked in both."""
    path = make_pdf(os.path.join(folder or env.scan_folder, filename))
    d = {"vendor": "QUEUE VENDOR CO", "amount": 120.0,
         "entity_code": "TRUST1", "property_code": "T27", "doc_type": "Bill",
         "ai_confidence": "High", "batch_number": "BATCH-1",
         "received_date": TODAY.isoformat(), "scanned_date": TODAY.isoformat(),
         "pdf_path": path}
    d.update(overrides)
    return d


def _enqueue(sid, data):
    klr.pending_scans[sid] = data
    database.save_pending_scan(sid, json.dumps(data))


def _db_path(sid):
    for scan_id, blob in database.get_all_pending_scans():
        if scan_id == sid:
            return json.loads(blob).get("pdf_path")
    return None


def _move_to(path, subfolder):
    """Move a queued file into a holding subfolder beside it (what the junk
    filter and set-aside do), leaving the queue row pointing at the old path."""
    folder = os.path.join(os.path.dirname(path), subfolder)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, os.path.basename(path))
    shutil.move(path, dest)
    return dest


# ── _repair_queue_paths ──────────────────────────────────────────────────────

def test_repair_relinks_row_whose_file_the_junk_filter_moved_to_filtered(env):
    """The #931 case: the whole-queue reclassify moved the doc to _Filtered and
    recorded it there, but left the queue row pointing at the old path."""
    item = _queue_item(env, "gl_proposal_part3.pdf", folder=env.email_folder)
    old_path = item["pdf_path"]
    _enqueue(1, item)
    dest = _move_to(old_path, "_Filtered")

    klr._repair_queue_paths()

    assert klr.pending_scans[1]["pdf_path"] == dest
    assert _db_path(1) == dest          # memory and the DB must not diverge


def test_repair_does_not_relink_from_skipped(env):
    """_Skipped is a human's decision to take the sheet out of the flow —
    re-linking would undo it."""
    item = _queue_item(env, "setaside_me.pdf")
    old_path = item["pdf_path"]
    _enqueue(1, item)
    _move_to(old_path, "_Skipped")

    klr._repair_queue_paths()

    assert klr.pending_scans[1]["pdf_path"] == old_path
    assert _db_path(1) == old_path


def test_repair_leaves_a_genuinely_missing_file_alone(env):
    item = _queue_item(env, "vanished.pdf")
    old_path = item["pdf_path"]
    _enqueue(1, item)
    os.remove(old_path)

    klr._repair_queue_paths()

    assert klr.pending_scans[1]["pdf_path"] == old_path
    assert _db_path(1) == old_path


def test_repair_does_not_steal_a_file_another_queue_row_owns(env):
    """Two rows, one surviving file: the row that still owns it keeps it."""
    healthy = _queue_item(env, "contested.pdf", vendor="HEALTHY OWNER CO")
    live_path = healthy["pdf_path"]
    _enqueue(1, healthy)

    broken = dict(healthy)
    broken["vendor"] = "BROKEN ROW CO"
    broken["pdf_path"] = os.path.join(env.email_folder, "contested.pdf")
    _enqueue(2, broken)

    klr._repair_queue_paths()

    assert klr.pending_scans[1]["pdf_path"] == live_path
    assert klr.pending_scans[2]["pdf_path"] == os.path.join(env.email_folder,
                                                            "contested.pdf")


def test_repair_does_not_steal_a_file_a_record_owns(env):
    item = _queue_item(env, "already_filed.pdf")
    filed = item["pdf_path"]
    database.insert_entry({"date_received": TODAY.isoformat(), "doc_type": "Bill",
                           "vendor": "RECORD OWNER CO", "amount": 10.0,
                           "status": "Pending", "pdf_path": filed})
    broken = dict(item)
    broken["pdf_path"] = os.path.join(env.email_folder, "already_filed.pdf")
    _enqueue(1, broken)

    klr._repair_queue_paths()

    assert klr.pending_scans[1]["pdf_path"] == os.path.join(env.email_folder,
                                                            "already_filed.pdf")


def test_repair_skips_an_ambiguous_basename(env):
    """The same name under both watched trees — no way to tell which is ours."""
    item = _queue_item(env, "twins.pdf")
    old_path = item["pdf_path"]
    _enqueue(1, item)
    _move_to(old_path, "_Filtered")
    make_pdf(os.path.join(env.email_folder, "twins.pdf"))

    klr._repair_queue_paths()

    assert klr.pending_scans[1]["pdf_path"] == old_path


def test_repair_is_idempotent_and_noops_when_nothing_is_broken(env):
    item = _queue_item(env, "idempotent.pdf", folder=env.email_folder)
    _enqueue(1, item)
    dest = _move_to(item["pdf_path"], "_Filtered")

    klr._repair_queue_paths()
    assert klr.pending_scans[1]["pdf_path"] == dest

    klr._repair_queue_paths()               # second pass: nothing left to fix
    assert klr.pending_scans[1]["pdf_path"] == dest
    assert _db_path(1) == dest

    healthy = _queue_item(env, "healthy.pdf")
    _enqueue(2, healthy)
    klr._repair_queue_paths()
    assert klr.pending_scans[2]["pdf_path"] == healthy["pdf_path"]


def test_repair_survives_an_unreadable_config(env, monkeypatch):
    """A failure here must never block boot."""
    item = _queue_item(env, "boom.pdf")
    _enqueue(1, item)
    os.remove(item["pdf_path"])
    monkeypatch.setattr(klr, "load_config", lambda: (_ for _ in ()).throw(OSError("nope")))

    klr._repair_queue_paths()               # must not raise

    assert 1 in klr.pending_scans


# ── in-tray ordering ─────────────────────────────────────────────────────────

def test_intray_opens_on_the_healthy_sheet_not_the_missing_one(env, staff_client):
    """Every orphan is old, so oldest-first used to open the tray on an error
    notice. Real work leads; the missing-file sheet sinks to the bottom."""
    c, headers = staff_client
    old_missing = _queue_item(env, "old_ghost.pdf", vendor="PHANTOM FILE VENDOR",
                              received_date=(TODAY - timedelta(days=30)).isoformat())
    os.remove(old_missing["pdf_path"])
    _enqueue(1, old_missing)
    _enqueue(2, _queue_item(env, "new_real.pdf", vendor="REAL WORK VENDOR",
                            received_date=TODAY.isoformat()))

    r = c.get("/intray")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "REAL WORK VENDOR" in html
    assert "PHANTOM FILE VENDOR" not in html     # not the sheet on top
    assert "sheet 1 of 2" in html                # still queued, still counted
    assert "1 sheet in this tray lost its scan file" in html


def test_intray_missing_sheet_is_still_reachable_and_last(env, staff_client):
    c, headers = staff_client
    missing = _queue_item(env, "ghost2.pdf", vendor="PHANTOM FILE VENDOR",
                          received_date=(TODAY - timedelta(days=30)).isoformat())
    os.remove(missing["pdf_path"])
    _enqueue(1, missing)
    _enqueue(2, _queue_item(env, "real2.pdf", received_date=TODAY.isoformat()))

    r = c.get("/intray?scan=1")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "missing from disk" in html
    assert "sheet 2 of 2" in html                # last in the tray, not first


# ── /intray/clear_missing ────────────────────────────────────────────────────

def test_clear_missing_removes_only_the_rows_that_lost_their_file(env, staff_client):
    c, headers = staff_client
    missing = _queue_item(env, "ghost3.pdf", vendor="PHANTOM FILE VENDOR")
    os.remove(missing["pdf_path"])
    _enqueue(1, missing)
    healthy = _queue_item(env, "real3.pdf", vendor="REAL WORK VENDOR")
    _enqueue(2, healthy)

    r = c.post("/intray/clear_missing", headers=headers, data={})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/intray")

    assert 1 not in klr.pending_scans
    assert 2 in klr.pending_scans
    assert [sid for sid, _ in database.get_all_pending_scans()] == [2]
    assert os.path.exists(healthy["pdf_path"])   # the healthy sheet's file is untouched


def test_clear_missing_ignores_a_submitted_list(env, staff_client):
    """The missing set is recomputed server-side, so a stale tab can't clear a
    healthy sheet by naming it."""
    c, headers = staff_client
    _enqueue(1, _queue_item(env, "real4.pdf", vendor="REAL WORK VENDOR"))

    r = c.post("/intray/clear_missing", headers=headers, data={"ids": ["1"]})
    assert r.status_code == 302
    assert 1 in klr.pending_scans


def test_clear_missing_needs_csrf(env, staff_client):
    c, _headers = staff_client
    missing = _queue_item(env, "ghost5.pdf")
    os.remove(missing["pdf_path"])
    _enqueue(1, missing)

    r = c.post("/intray/clear_missing", data={})
    assert r.status_code in (400, 403)
    assert 1 in klr.pending_scans


def test_clear_missing_banner_absent_when_the_tray_is_healthy(env, staff_client):
    c, headers = staff_client
    _enqueue(1, _queue_item(env, "real6.pdf"))
    html = c.get("/intray").get_data(as_text=True)
    assert "lost its scan file" not in html
    assert "clear them from the queue" not in html
