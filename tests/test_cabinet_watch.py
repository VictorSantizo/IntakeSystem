"""cabinet_watch.py: live pickup of hand-filed documents (reports #28/#46/#50).

Covers the decision ladder end-to-end against an isolated archive tree:
register-in-place (Pending and Paid), the OCR-vs-filename conflict holds, the
near-match Paid veto, byte-hash re-linking and copy-attach, hand-move
re-linking (file and whole-folder), self-move suppression via filing_ledger,
the leave-in-place confirm path, and the catch-up sweep."""
import json
import os

import pytest

import app as klr
import cabinet_watch
import database
import filing_ledger
from conftest import make_pdf

OCR = {
    "is_cover_sheet": False, "received_date": "2026-07-10",
    "stamped_received_date": None,
    "doc_type": "Bill", "ai_confidence": "High",
    "vendor": "Frontier Communications", "amount": 89.10,
    "due_date": "2026-08-15", "entity_code": "TRUST1",
    "entity_name": "Test Trust One",
    "property_codes": ["T27"], "property_addresses": [],
    "bill_type_code": "ELEC", "invoice_number": "INV-77",
    "account_number": "12-3456", "notes": "",
    "important": False, "important_reason": "",
    "extracted_text": "INVOICE amount due $89.10 " + "words " * 30,
}


def _stub_classifier(monkeypatch, **overrides):
    r = json.loads(json.dumps(OCR))
    r.update(overrides)
    monkeypatch.setattr(klr, "classify_document", lambda path: dict(r))
    return r


@pytest.fixture
def cab(env, monkeypatch):
    """Archive tree + a fast cabinet watcher pointed at it (env resets state)."""
    archive = os.path.join(str(env.tmp), "Files")
    ap = os.path.join(archive, "2026", "ACCOUNTS PAYABLE - INCOMING BILLS", "07. JULY")
    paid = os.path.join(archive, "2026", "TRUST", "TRUST PAID BILLS", "07. JULY")
    os.makedirs(ap, exist_ok=True)
    os.makedirs(paid, exist_ok=True)
    env.cfg.update({"archive_folder": archive})
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)
    monkeypatch.setattr(cabinet_watch, "_wait_until_stable", lambda p, timeout=30: True)
    import types
    return types.SimpleNamespace(archive=archive, ap=ap, paid=paid)


def _fresh_cache():
    cabinet_watch._entries_cache["at"] = 0.0


# ── register in place ─────────────────────────────────────────────────────────

def test_pending_register_in_place(cab, monkeypatch):
    _stub_classifier(monkeypatch)
    p = make_pdf(os.path.join(cab.ap, "07-28-26 TRUST1 - FRONTIER - $89.10 - T27.pdf"))
    cabinet_watch._process(p)
    e = database.get_all_entries()[0]
    assert e["status"] == "Pending"
    assert e["mail_type"] == "Hand-filed"
    assert e["processed_by"] == "Cabinet-Watch"
    assert e["filed_path"] == p and e["pdf_path"] == p
    assert os.path.exists(p)                      # never moved, never renamed
    assert e["amount"] == 89.10
    assert e["entity_code"] == "TRUST1"
    assert e["property_code"] == "T27"
    assert e["vendor"] == "Frontier Communications"   # OCR wins on same vendor
    assert e["date_received"] == "2026-07-28"         # filename date wins


def test_statement_in_incoming_bills_keeps_ocr_doc_type(cab, monkeypatch):
    # Joshua 2026-07-29: "some scans in the incoming bills are statements" —
    # the folder is a prior, the OCR read decides the type.
    _stub_classifier(monkeypatch, doc_type="Bank Statement", amount=None,
                     vendor="Montecito Bank & Trust")
    p = make_pdf(os.path.join(cab.ap, "07-28-26 TRUST1 - MONTECITO BANK.pdf"))
    cabinet_watch._process(p)
    e = database.get_all_entries()[0]
    assert e["doc_type"] == "Bank Statement"
    assert e["status"] == "Pending"


def test_paid_register_in_paid_tree(cab, monkeypatch):
    _stub_classifier(monkeypatch, vendor="Swan Cabinets", amount=1230.00)
    p = make_pdf(os.path.join(cab.paid,
                              "07-24-26 TRUST CK#3012 - SWAN CABINETS - $1,230.00 - T27.pdf"))
    cabinet_watch._process(p)
    e = database.get_all_entries()[0]
    assert e["status"] == "Paid"
    assert e["paid_date"] == "2026-07-24"
    assert e["paid_amount"] == 1230.00
    assert e["check_number"] == "3012"
    assert e["payment_method"] == "Check"
    assert e["filed_path"] == p and os.path.exists(p)


# ── archive backfill: old year trees (the 2026-08-05 flood) ───────────────────

def _no_ai(monkeypatch):
    def boom(path):
        raise AssertionError("archive backfill must not spend an AI read")
    monkeypatch.setattr(klr, "classify_document", boom)


def test_old_year_tree_registers_filed_without_ai(cab, monkeypatch):
    _no_ai(monkeypatch)
    old = os.path.join(cab.archive, "2024", "PP", "CASH FLOW STATEMENTS",
                       "FULL CASH FLOW STATEMENT WITH BILLS",
                       "PP16 - 105 REAL ROAD", "1. JANUARY")
    os.makedirs(old, exist_ok=True)
    p = make_pdf(os.path.join(old, "PP16 - 105 REAL ROAD CASH FLOW - JAN 2024.pdf"))
    cabinet_watch._process(p)
    e = database.get_all_entries()[0]
    assert e["status"] == "Filed"
    assert e["date_received"] == "2024-01-31"       # the period, at month-end
    assert e["doc_type"] == "Statement"             # folder taxonomy, no OCR
    assert e["mail_type"] == "Hand-filed"
    assert e["processed_by"] == "Cabinet-Watch"
    assert e["entity_code"] == "PP" and e["property_code"] == "PP16"
    assert e["filed_path"] == p and os.path.exists(p)
    assert list(klr.pending_scans.values()) == []   # no review hold either


def test_old_year_tree_month_folder_beats_missing_month_word(cab, monkeypatch):
    _no_ai(monkeypatch)
    old = os.path.join(cab.archive, "2023", "TRUST", "TRUST PAID BILLS",
                       "7. JULY")
    os.makedirs(old, exist_ok=True)
    p = make_pdf(os.path.join(old, "TRUST - ACME REPAIRS - $120.00.pdf"))
    cabinet_watch._process(p)
    e = database.get_all_entries()[0]
    assert e["status"] == "Filed"                   # NOT Paid: archive layer
    assert e["date_received"] == "2023-07-31"       # month folder + tree year
    assert e["doc_type"] == "Bill"                  # paid tree without OCR
    assert e["amount"] == 120.00


def test_old_year_tree_no_month_gets_year_end(cab, monkeypatch):
    _no_ai(monkeypatch)
    old = os.path.join(cab.archive, "2022", "KLR", "MISC")
    os.makedirs(old, exist_ok=True)
    p = make_pdf(os.path.join(old, "KLR SOMETHING KEPT.pdf"))
    cabinet_watch._process(p)
    e = database.get_all_entries()[0]
    assert e["status"] == "Filed"
    assert e["date_received"] == "2022-12-31"


def test_current_year_tree_still_walks_the_normal_ladder(cab, monkeypatch):
    # Sanity: the 2026 trees are untouched by the archive rule — same OCR
    # classify + Pending register as ever.
    _stub_classifier(monkeypatch)
    p = make_pdf(os.path.join(cab.ap, "07-28-26 TRUST1 - FRONTIER - $89.10 - T27.pdf"))
    cabinet_watch._process(p)
    e = database.get_all_entries()[0]
    assert e["status"] == "Pending"


def test_archive_recopy_of_registered_bytes_attaches_not_duplicates(cab, monkeypatch):
    # A SECOND bulk re-export of the same archive file must ride the byte-hash
    # rung (attach as copy), never mint a twin Filed record.
    _no_ai(monkeypatch)
    old = os.path.join(cab.archive, "2024", "PP", "CASH FLOW STATEMENTS",
                       "PP16 - 105 REAL ROAD", "1. JANUARY")
    os.makedirs(old, exist_ok=True)
    p = make_pdf(os.path.join(old, "PP16 CASH FLOW - JAN 2024.pdf"))
    cabinet_watch._process(p)
    assert len(database.get_all_entries()) == 1
    copy = os.path.join(old, "PP16 CASH FLOW - JAN 2024 (1).pdf")
    with open(p, "rb") as src, open(copy, "wb") as dst:
        dst.write(src.read())
    cabinet_watch._process(copy)
    entries = database.get_all_entries()
    assert len(entries) == 1                        # still one record
    atts = database.get_attachments(entries[0]["id"])
    assert any(a["file_path"] == copy for a in atts)


# ── conflicts hold for a human (the review queue) ─────────────────────────────

def test_amount_conflict_holds_for_review(cab, monkeypatch):
    _stub_classifier(monkeypatch, amount=123.45)
    p = make_pdf(os.path.join(cab.ap, "07-28-26 TRUST1 - FRONTIER - $500.00 - T27.pdf"))
    cabinet_watch._process(p)
    assert database.get_all_entries() == []
    items = list(klr.pending_scans.values())
    assert len(items) == 1
    assert items[0]["hand_filed"] is True
    assert items[0]["mail_type"] == "Hand-filed"
    assert "Filename says" in items[0]["notes"]
    assert os.path.exists(p)


def test_typoed_filename_year_holds_for_review(cab, monkeypatch):
    # Record #941: "06-30-256" (year typo for 26) parsed as the year 256 and a
    # June 2026 statement was registered as 0256-06-30. A date-shaped prefix
    # that can't be real must hold for a person — never register with a guess.
    # (Filename payee matches the OCR vendor here so no OTHER hold fires first.)
    _stub_classifier(monkeypatch, amount=None)
    p = make_pdf(os.path.join(cab.ap, "06-30-256 TRUST1 - FRONTIER - T27.pdf"))
    cabinet_watch._process(p)
    assert database.get_all_entries() == []
    items = list(klr.pending_scans.values())
    assert len(items) == 1
    assert "can't be real" in items[0]["notes"]
    assert os.path.exists(p)                      # left where the filer put it


def test_paid_near_match_veto_holds(cab, monkeypatch):
    # A blank-amount record with the same vendor can never exact-match; without
    # the veto, its paid filing would mint a Paid twin (harvest's lesson).
    database.insert_entry({"vendor": "Swan Cabinets", "amount": None,
                           "status": "Pending", "doc_type": "Bill",
                           "date_received": "2026-07-01", "entity_code": "TRUST1"})
    _fresh_cache()
    _stub_classifier(monkeypatch, vendor="Swan Cabinets", amount=1230.00)
    p = make_pdf(os.path.join(cab.paid,
                              "07-24-26 TRUST CK#3012 - SWAN CABINETS - $1,230.00.pdf"))
    cabinet_watch._process(p)
    assert len(database.get_all_entries()) == 1        # no new record
    items = list(klr.pending_scans.values())
    assert len(items) == 1 and items[0]["hand_filed"]


def test_payment_filing_matching_pending_record_attaches(cab, monkeypatch):
    eid = database.insert_entry({"vendor": "Swan Cabinets", "amount": 1230.00,
                                 "status": "Pending", "doc_type": "Bill",
                                 "date_received": "2026-07-20",
                                 "entity_code": "TRUST1"})
    _fresh_cache()
    _stub_classifier(monkeypatch, vendor="Swan Cabinets", amount=1230.00)
    p = make_pdf(os.path.join(cab.paid,
                              "07-24-26 TRUST CK#3012 - SWAN CABINETS - $1,230.00.pdf"))
    cabinet_watch._process(p)
    assert len(database.get_all_entries()) == 1        # attached, not registered
    atts = database.get_attachments(eid)
    assert len(atts) == 1 and atts[0]["file_path"] == p
    assert klr.pending_scans == {}


# ── re-linking (report #46, live) ─────────────────────────────────────────────

def test_hand_move_relinks_by_moved_from(cab):
    old = os.path.join(cab.ap, "somewhere", "old.pdf")
    eid = database.insert_entry({"vendor": "X", "status": "Pending",
                                 "doc_type": "Bill", "date_received": "2026-07-01",
                                 "filed_path": old})
    new = make_pdf(os.path.join(cab.paid, "old.pdf"))
    cabinet_watch._process(new, moved_from=old)
    assert database.get_entry(eid)["filed_path"] == new


def test_hand_move_relinks_by_hash(cab, monkeypatch):
    # A cross-volume hand move fires on_created only — the byte hash finds it.
    new = make_pdf(os.path.join(cab.ap, "found again.pdf"))
    h = klr._sha256_file(new)
    dead = os.path.join(cab.archive, "2026", "GONE", "was here.pdf")
    eid = database.insert_entry({"vendor": "X", "status": "Pending",
                                 "doc_type": "Bill", "date_received": "2026-07-01",
                                 "filed_path": dead, "file_hash": h})
    _fresh_cache()
    cabinet_watch._process(new)
    assert database.get_entry(eid)["filed_path"] == new
    assert len(database.get_all_entries()) == 1


def test_duplicate_of_live_record_attaches_as_copy(cab):
    live = make_pdf(os.path.join(cab.ap, "original.pdf"))
    h = klr._sha256_file(live)
    eid = database.insert_entry({"vendor": "X", "status": "Pending",
                                 "doc_type": "Bill", "date_received": "2026-07-01",
                                 "filed_path": live, "file_hash": h})
    copy = make_pdf(os.path.join(cab.paid, "a copy.pdf"))
    cabinet_watch._process(copy)
    atts = database.get_attachments(eid)
    assert len(atts) == 1 and atts[0]["file_path"] == copy
    assert len(database.get_all_entries()) == 1


def test_folder_move_relinks_prefixes(cab):
    old_dir = os.path.join(cab.ap, "06. JUNE")
    new_dir = os.path.join(cab.ap, "07. JULY RENAMED")
    eid = database.insert_entry({"vendor": "X", "status": "Pending",
                                 "doc_type": "Bill", "date_received": "2026-07-01",
                                 "filed_path": os.path.join(old_dir, "a.pdf")})
    database.add_attachment(eid, os.path.join(old_dir, "b.pdf"), "Copy", "b.pdf", "")
    cabinet_watch._relink_dir_move(old_dir, new_dir)
    assert database.get_entry(eid)["filed_path"] == os.path.join(new_dir, "a.pdf")
    assert database.get_attachments(eid)[0]["file_path"] == os.path.join(new_dir, "b.pdf")


# ── suppression and scope ─────────────────────────────────────────────────────

def test_app_announced_move_is_ignored(cab, monkeypatch):
    _stub_classifier(monkeypatch)
    p = make_pdf(os.path.join(cab.ap, "07-28-26 TRUST1 - FRONTIER - $89.10.pdf"))
    filing_ledger.announce(p)
    cabinet_watch._process(p)
    assert database.get_all_entries() == []
    assert klr.pending_scans == {}


def test_holding_folders_and_lock_files_are_skipped(cab, monkeypatch):
    _stub_classifier(monkeypatch)
    dup_dir = os.path.join(cab.ap, "_Duplicates")
    os.makedirs(dup_dir, exist_ok=True)
    cabinet_watch._process(make_pdf(os.path.join(dup_dir, "x.pdf")))
    lock = os.path.join(cab.ap, "~$lock.pdf")
    make_pdf(lock)
    cabinet_watch._process(lock)
    assert database.get_all_entries() == []


def test_nonbill_and_summary_are_skipped(cab, monkeypatch):
    _stub_classifier(monkeypatch)
    cabinet_watch._process(make_pdf(os.path.join(cab.paid,
                                                 "07-01-26 TRUST TRANSFER TO PP $500.pdf")))
    cabinet_watch._process(make_pdf(os.path.join(cab.paid,
                                                 "JUNE RECAP CK#3001-3050 $45,210.pdf")))
    assert database.get_all_entries() == []
    assert klr.pending_scans == {}


# ── held item confirms in place ───────────────────────────────────────────────

def test_held_item_confirms_in_place(cab, monkeypatch, staff_client):
    c, headers = staff_client
    _stub_classifier(monkeypatch, amount=123.45)   # conflicts with the filename
    p = make_pdf(os.path.join(cab.ap, "07-28-26 TRUST1 - FRONTIER - $500.00 - T27.pdf"))
    cabinet_watch._process(p)
    (sid, item), = list(klr.pending_scans.items())
    assert item["hand_filed"]
    # The review page must default the File-as panel to the file's own folder+name.
    r = c.get(f"/review/{sid}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert os.path.dirname(p) in body
    # Confirming without touching the filing fields keeps the file exactly
    # where (and what) the filer made it.
    r = c.post(f"/review/{sid}/confirm?vendor_ok=1&force=1", headers=headers,
               data={"date_received": "2026-07-28", "doc_type": "Bill",
                     "vendor": "Frontier Communications", "entity_code": "TRUST1",
                     "property_codes": ["T27"], "amount": "500.00", "date_ok": "1"})
    assert r.status_code == 302
    e = database.get_all_entries()[0]
    assert e["filed_path"] == p
    assert os.path.exists(p)                       # still exactly there
    assert klr.pending_scans == {}


# ── sweep ─────────────────────────────────────────────────────────────────────

def test_sweep_queues_recent_unknown_files_only(cab, monkeypatch):
    monkeypatch.setattr(cabinet_watch, "_SWEEP_MIN_AGE_SECS", 0)
    fresh = make_pdf(os.path.join(cab.ap, "07-28-26 TRUST1 - NEW THING.pdf"))
    known = make_pdf(os.path.join(cab.ap, "07-28-26 TRUST1 - KNOWN.pdf"))
    database.insert_entry({"vendor": "K", "status": "Pending", "doc_type": "Bill",
                           "date_received": "2026-07-01", "filed_path": known})
    out = cabinet_watch.sweep()
    assert out["started"]
    queued = []
    while not cabinet_watch._work_q.empty():
        queued.append(cabinet_watch._work_q.get_nowait()[1])
    assert fresh in queued
    assert known not in queued
