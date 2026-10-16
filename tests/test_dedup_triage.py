"""End-to-end duplicate detection: ingest -> queue -> confirm -> re-ingest.

Runs the REAL pipeline (no API key in the test config, so classification uses
the regex fallback and the splitter passes files through) against temp folders.
"""
import os
import shutil

import app as klr
import database
from conftest import make_pdf

DOC_TEXT = ("INVOICE from ACME Utilities account 12-3456 amount due $147.48 "
            "due date 07/15/2026 service address 123 Example Ave Sometown "
            "please remit payment promptly thank you for your business today")


def _ingest_one(env, name, text=DOC_TEXT):
    path = os.path.join(env.scan_folder, name)
    make_pdf(path, text)
    klr.enqueue_scan(path, "Physical Mail")
    return path


def test_ingest_queues_with_hash(env):
    _ingest_one(env, "bill.pdf")
    items = klr._queue_items()
    assert len(items) == 1
    sid, d = items[0]
    assert d["file_hash"] and len(d["file_hash"]) == 64
    assert d["amount"] == 147.48
    assert d["due_date"] == "2026-07-15"


def test_second_identical_queue_file_set_aside(env):
    _ingest_one(env, "bill.pdf")
    # identical bytes under a different name
    src = klr._queue_items()[0][1]["pdf_path"]
    dup = os.path.join(env.scan_folder, "bill-again.pdf")
    shutil.copyfile(src, dup)
    klr.enqueue_scan(dup, "Physical Mail")
    assert len(klr._queue_items()) == 1                      # not queued twice
    aside = os.listdir(os.path.join(env.scan_folder, "_Duplicates"))
    assert aside == ["bill-again.pdf"]


def _confirm_first_item(env, staff):
    c, headers = staff
    sid, d = klr._queue_items()[0]
    r = c.post(f"/review/{sid}/confirm", headers=headers, data={
        "date_received": "2026-07-06", "mail_type": "Physical Mail",
        "doc_type": "Bill", "vendor": "ACME Utilities", "amount": "147.48",
        "due_date": "2026-07-15", "entity_code": "TRUST1",
        "property_codes": ["T27"], "invoice_number": "INV-1", "account_number": "12-3456",
        "date_ok": "1",   # bypass the year-sanity guard as the fixed dates age
    })
    assert r.status_code == 302
    return database.get_all_entries()[0]


def test_confirm_carries_hash_and_files_doc(env, staff_client):
    _ingest_one(env, "bill.pdf")
    entry = _confirm_first_item(env, staff_client)
    assert entry["file_hash"] and len(entry["file_hash"]) == 64
    assert entry["filed_path"] and os.path.exists(entry["filed_path"])
    assert not klr._queue_items()


def test_reingested_copy_auto_attaches(env, staff_client):
    _ingest_one(env, "bill.pdf")
    entry = _confirm_first_item(env, staff_client)
    # the same document arrives again (e.g. re-sent email)
    dup = os.path.join(env.scan_folder, "resent.pdf")
    shutil.copyfile(entry["filed_path"], dup)
    klr.enqueue_scan(dup, "Physical Mail")
    assert not klr._queue_items()                        # never reached the queue
    atts = database.get_attachments(entry["id"])
    assert len(atts) == 1 and os.path.exists(atts[0]["file_path"])
    assert "(Physical Mail copy)" in atts[0]["file_path"]


def test_triage_groups_and_bulk_resolve(env, staff_client):
    c, headers = staff_client
    _ingest_one(env, "bill.pdf")
    entry = _confirm_first_item(env, staff_client)

    # simulate a pre-dedup queue item pointing at an identical file (no hash yet)
    ghost = os.path.join(env.scan_folder, "ghost.pdf")
    shutil.copyfile(entry["filed_path"], ghost)
    sid = klr._queue_add({"pdf_path": ghost, "vendor": "ACME Utilities",
                          "mail_type": "Physical Mail"})

    certain, queue_dups, probable, near_queue = klr._triage_groups()
    assert [it["sid"] for it in certain] == [sid]
    assert klr.pending_scans[sid]["file_hash"]           # hashed + persisted lazily

    r = c.get("/review/triage")
    assert r.status_code == 200 and b"exact duplicates" in r.data

    r = c.post("/review/triage/resolve_certain", headers=headers)
    assert r.status_code == 302
    assert sid not in klr.pending_scans
    assert len(database.get_attachments(entry["id"])) == 1
    assert not os.path.exists(ghost)                     # moved next to the record


def test_triage_probable_by_identity_not_bytes(env, staff_client):
    _ingest_one(env, "bill.pdf")
    entry = _confirm_first_item(env, staff_client)
    # different bytes (extra text), same vendor+amount+due date
    _ingest_one(env, "mailed-copy.pdf", DOC_TEXT + " duplicate stamp mailed paper copy")
    certain, queue_dups, probable, near_queue = klr._triage_groups()
    assert not certain and not queue_dups
    assert len(probable) == 1
    assert probable[0]["matches"][0]["id"] == entry["id"]
    assert not near_queue                # a logged match wins over queue grouping


def _queue_bill(env, name, text, **fields):
    """Queue a real PDF directly with the fields the AI would have extracted
    (the keyless test classifier's regex fallback doesn't extract vendors)."""
    path = os.path.join(env.scan_folder, name)
    make_pdf(path, text)
    base = {"pdf_path": path, "mail_type": "Email", "vendor": "ACME Utilities",
            "amount": 147.48, "due_date": "2026-07-15"}
    base.update(fields)
    return klr._queue_add(base)


def test_triage_near_queue_repeats_set_aside(env, staff_client):
    """The same bill (identity, not bytes) queued three times with nothing
    logged yet: triage groups them, and one click sets aside the repeats while
    keeping the earliest-received copy in the queue."""
    c, headers = staff_client
    _queue_bill(env, "first.pdf", DOC_TEXT)
    _queue_bill(env, "second.pdf", DOC_TEXT + " resent email copy")
    _queue_bill(env, "third.pdf", DOC_TEXT + " mailed paper copy")
    assert len(klr._queue_items()) == 3                  # different bytes, all queued

    certain, queue_dups, probable, near_queue = klr._triage_groups()
    assert not certain and not queue_dups and not probable
    assert len(near_queue) == 1
    group = near_queue[0]
    assert len(group["extras"]) == 2
    earliest = min(sid for sid, _ in klr._queue_items())
    assert group["kept"]["sid"] == earliest              # earliest received is kept

    r = c.get("/review/triage")
    assert r.status_code == 200 and b"waiting more than once" in r.data

    r = c.post(f"/review/triage/set_aside_near/{group['kept']['sid']}", headers=headers)
    assert r.status_code == 302
    assert [sid for sid, _ in klr._queue_items()] == [earliest]
    aside = os.listdir(os.path.join(env.scan_folder, "_Duplicates"))
    assert len(aside) == 2                               # moved aside, not deleted


def _log_custom_bill(env, staff_client, name, vendor, amount, invoice, due="2026-07-15"):
    """Queue a bill with the given identity and confirm it into the log."""
    c, headers = staff_client
    sid = _queue_bill(env, name, f"invoice text for {name}", vendor=vendor,
                      amount=amount, due_date=due)
    # vendor_ok=1: this helper intentionally logs near-spelling vendors (e.g.
    # "Southern California Edison" vs "... (SCE)") to test hash/legacy-dupe
    # merging — bypass the vendor-match interstitial, which is a separate
    # feature (report #10 follow-up) not under test here.
    r = c.post(f"/review/{sid}/confirm?vendor_ok=1", headers=headers, data={
        "date_received": "2026-07-06", "mail_type": "Physical Mail",
        "doc_type": "Bill", "vendor": vendor, "amount": str(amount),
        "due_date": due, "entity_code": "TRUST1", "property_codes": ["T27"],
        "invoice_number": invoice,
        "date_ok": "1",   # bypass the year-sanity guard as the fixed dates age
    })
    assert r.status_code == 302
    return next(e for e in database.get_all_entries() if e["vendor"] == vendor)


def test_triage_probable_sorted_best_first_and_bulk_attach(env, staff_client):
    c, headers = staff_client
    e1 = _log_custom_bill(env, staff_client, "acme.pdf", "ACME Utilities", 147.48, "INV-1")
    e2 = _log_custom_bill(env, staff_client, "beta.pdf", "Beta Corp", 50.0, "B-9")
    # queued FIRST but the weaker match: same vendor+amount+due as e1, yet a
    # DIFFERENT invoice number (meaningful conflict, read with High confidence)
    weak_sid = _queue_bill(env, "weak.pdf", "acme resend", vendor="ACME Utilities",
                           amount=147.48, due_date="2026-07-15",
                           invoice_number="INV-999", ai_confidence="High")
    # queued second but the stronger match: e2 on vendor+amount+due AND invoice
    strong_sid = _queue_bill(env, "strong.pdf", "beta resend", vendor="Beta Corp",
                             amount=50.0, due_date="2026-07-15", invoice_number="B-9")
    certain, queue_dups, probable, near_queue = klr._triage_groups()
    assert [it["sid"] for it in probable] == [strong_sid, weak_sid]   # best first

    # bulk-attach ONLY the ticked row; a bogus id is ignored
    r = c.post("/review/triage/attach_selected", headers=headers,
               data={"sids": [str(strong_sid), "999999"]})
    assert r.status_code == 302
    assert strong_sid not in klr.pending_scans               # attached + dequeued
    assert weak_sid in klr.pending_scans                     # untouched
    assert len(database.get_attachments(e2["id"])) == 1
    assert not database.get_attachments(e1["id"])

    r = c.get("/review/triage")
    assert r.status_code == 200 and b"bulk-attach-form" in r.data


def test_triage_set_aside_near_stale_kept_sid_is_noop(env, staff_client):
    """A stale tab posting a scan id that no longer heads a group must not
    touch the queue."""
    c, headers = staff_client
    _ingest_one(env, "only.pdf")
    r = c.post("/review/triage/set_aside_near/999999", headers=headers)
    assert r.status_code == 302
    assert len(klr._queue_items()) == 1
