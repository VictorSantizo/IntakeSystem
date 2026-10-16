"""The duplicate comparison view: field-by-field same/different breakdown with
tiered signal strengths (_dup_comparison), the verdict it produces, the
received-date match key in find_duplicates, and the pages that render the
side-by-side (including the visual compare page)."""
import os

import app as klr
import database
from conftest import make_pdf

from test_dedup_triage import DOC_TEXT, _ingest_one, _confirm_first_item, _queue_bill


# ── _dup_comparison: the engine ────────────────────────────────────────────────

def test_comparison_weak_differences_count_zero_against():
    """Karen's rule: received date and source differ freely between two copies
    of the SAME bill — they're shown, but count zero against the match."""
    queued = {"vendor": "ACME Utilities", "amount": "147.48", "due_date": "2026-07-15",
              "received_date": "2026-07-06", "mail_type": "Email",
              "ai_confidence": "High"}
    logged = {"vendor": "acme utilities", "amount": 147.48, "due_date": "2026-07-15",
              "date_received": "2026-07-10", "mail_type": "Physical Mail"}
    cmp = klr._dup_comparison(queued, logged)
    by_label = {r["label"]: r["state"] for r in cmp["rows"]}
    assert by_label["Vendor"] == "same" and by_label["Amount"] == "same"
    assert by_label["Received date"] == "diff" and by_label["Source"] == "diff"
    assert cmp["diff_labels"] == []                       # nothing meaningful differs
    assert set(cmp["expected_diff_labels"]) == {"Received date", "Source"}
    assert cmp["same"] == 3 and cmp["diff"] == 0 and cmp["compared"] == 5
    # vendor(2) + amount(3) + due date(3) = 8 -> very likely
    assert cmp["verdict"]["level"] == "very_likely"


def test_comparison_normalizes_ids_and_properties():
    cmp = klr._dup_comparison(
        {"vendor": "V", "account_number": "12-3456", "property_codes": ["T27", "T3"]},
        {"vendor": "V", "account_number": "123 456", "property_code": "T3, T27"})
    by_label = {r["label"]: r["state"] for r in cmp["rows"]}
    assert by_label["Account #"] == "same"       # dashes/spaces ignored
    assert by_label["Property"] == "same"        # list vs string, order ignored


def test_comparison_blank_fields():
    cmp = klr._dup_comparison({"vendor": "V", "invoice_number": "INV-1"},
                              {"vendor": "V"})
    by_label = {r["label"]: r["state"] for r in cmp["rows"]}
    assert by_label["Invoice #"] == "blank"      # one side only: grey, not counted
    assert cmp["compared"] == 1                  # just Vendor
    assert "Amount" not in by_label              # blank on both sides: omitted


def test_identical_file_is_certain_and_paths_ride_along():
    same = klr._dup_comparison(
        {"vendor": "V", "file_hash": "a" * 64, "pdf_path": "F:\\Staging\\a.pdf"},
        {"vendor": "V", "file_hash": "a" * 64, "filed_path": "F:\\Filed\\b.pdf"})
    assert same["verdict"]["level"] == "certain"
    row = next(r for r in same["rows"] if r["label"] == "Document file")
    assert row["left_path"] == "F:\\Staging\\a.pdf"      # full server paths shown
    assert row["right_path"] == "F:\\Filed\\b.pdf"


def test_different_file_scores_zero_against():
    """No byte match is NOT a signal for 'not a duplicate' (Karen's rule)."""
    cmp = klr._dup_comparison(
        {"vendor": "V", "amount": 10.0, "due_date": "2026-08-01",
         "file_hash": "a" * 64, "ai_confidence": "High"},
        {"vendor": "V", "amount": 10.0, "due_date": "2026-08-01",
         "file_hash": "b" * 64})
    assert "Document file" not in cmp["diff_labels"]
    assert "Document file" in cmp["expected_diff_labels"]
    assert cmp["verdict"]["level"] == "very_likely"      # 8 points, file ignored
    assert "normal for a second copy" in cmp["verdict"]["reason"]


def test_strong_conflicts_make_verdict_weak():
    cmp = klr._dup_comparison(
        {"vendor": "V", "amount": 10.0, "due_date": "2026-08-01",
         "invoice_number": "A-1", "ai_confidence": "High"},
        {"vendor": "V", "amount": 99.0, "due_date": "2026-09-01",
         "invoice_number": "A-2"})
    # vendor(+2) - amount(3) - due(2) - invoice(3) = -6
    assert cmp["verdict"]["level"] == "weak"
    assert set(cmp["diff_labels"]) == {"Amount", "Due date", "Invoice #"}


def test_low_ocr_confidence_softens_differences():
    """A mismatch on a Low-confidence read may be a misread, so it's weighed
    lightly (Karen's rule)."""
    left_hi = {"vendor": "V", "amount": 10.0, "due_date": "2026-08-01",
               "invoice_number": "A-1", "ai_confidence": "High"}
    left_lo = dict(left_hi, ai_confidence="Low")
    right = {"vendor": "V", "amount": 10.0, "due_date": "2026-08-01",
             "invoice_number": "A-2"}
    hi = klr._dup_comparison(left_hi, right)     # 2+3+3 - 3   = 5   -> likely
    lo = klr._dup_comparison(left_lo, right)     # 2+3+3 - 1.5 = 6.5 -> very likely
    assert hi["verdict"]["level"] == "likely"
    assert lo["verdict"]["level"] == "very_likely"
    assert "weighed lightly" in lo["verdict"]["reason"]


# ── find_duplicates: received-date key ─────────────────────────────────────────

def test_find_duplicates_same_received_date(env, staff_client):
    _ingest_one(env, "bill.pdf")
    entry = _confirm_first_item(env, staff_client)     # date_received 2026-07-06
    hits = database.find_duplicates("ACME Utilities", 147.48,
                                    received_date="2026-07-06")
    assert [h["id"] for h in hits] == [entry["id"]]
    assert not database.find_duplicates("ACME Utilities", 147.48,
                                        received_date="2026-07-07")


# ── the pages ──────────────────────────────────────────────────────────────────

def test_review_page_shows_comparison_and_compare_link(env, staff_client):
    c, headers = staff_client
    _ingest_one(env, "bill.pdf")
    entry = _confirm_first_item(env, staff_client)
    sid = _queue_bill(env, "again.pdf", DOC_TEXT + " second copy",
                      due_date="2026-07-15")
    r = c.get(f"/review/{sid}")
    assert r.status_code == 200
    assert b"details match" in r.data or b"detail match" in r.data
    rec_label = database.get_entry(entry["id"])["record_no"] or f"#{entry['id']}"
    assert f"Record {rec_label}".encode() in r.data
    assert f"/review/{sid}/compare/{entry['id']}".encode() in r.data
    assert b"FROM EMAIL" in r.data           # source badge on the doc preview


def test_compare_page_renders_both_documents(env, staff_client):
    c, headers = staff_client
    _ingest_one(env, "bill.pdf")
    entry = _confirm_first_item(env, staff_client)
    sid = _queue_bill(env, "again.pdf", DOC_TEXT + " second copy",
                      due_date="2026-07-15")
    r = c.get(f"/review/{sid}/compare/{entry['id']}")
    assert r.status_code == 200
    assert b"Side-by-Side Duplicate Check" in r.data
    # both full server file paths are on the page (manual-process access)
    d = klr.pending_scans[sid]
    assert d["pdf_path"].encode() in r.data
    assert entry["filed_path"].encode() in r.data
    # the two panes the client script fills with boxed page images
    assert b"pane-scan" in r.data and b"pane-entry" in r.data
    assert b"sync-scroll" in r.data                      # scroll-together toggle
    # loud source badges: queued copy came by email, the record is a paper scan
    assert b"FROM EMAIL" in r.data and b"SCANNED PAPER" in r.data
    # a stale/bogus pair redirects instead of erroring
    r = c.get(f"/review/999999/compare/{entry['id']}")
    assert r.status_code == 302


def test_triage_probable_and_near_rows_show_verdict(env, staff_client):
    c, headers = staff_client
    _ingest_one(env, "bill.pdf")
    _confirm_first_item(env, staff_client)
    _queue_bill(env, "again.pdf", DOC_TEXT + " second copy")     # probable
    _queue_bill(env, "new1.pdf", "different bill", vendor="Other Vendor",
                amount=9.99, due_date="2026-08-01")              # near-queue pair
    _queue_bill(env, "new2.pdf", "different bill resent", vendor="Other Vendor",
                amount=9.99, due_date="2026-08-01")
    certain, queue_dups, probable, near_queue = klr._triage_groups()
    assert probable and probable[0]["cmp"]["verdict"]["level"] in (
        "very_likely", "likely", "possible")
    assert len(near_queue) == 1
    extra_cmp = near_queue[0]["extras"][0]["cmp"]
    assert extra_cmp["diff_labels"] == []                        # file diff is expected
    assert "Document file" in extra_cmp["expected_diff_labels"]
    r = c.get("/review/triage")
    assert r.status_code == 200
    assert b"the same bill" in r.data                            # verdict label
    assert b"no meaningful differences" in r.data                # near-queue note
    assert b"/compare/" in r.data                                # compare links
