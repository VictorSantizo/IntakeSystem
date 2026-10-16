"""Paper views, second wave (2026-07-14): /desk (landing — piles + stickies),
/cabinet (archive by entity+month), /rolodex (vendor directory + card), and
/intray (review queue one sheet at a time). /intray's stamp and set-aside
actions delegate to the EXISTING /review/<id>/confirm and /review/<id>/skip
routes via a return_to=intray form field — this file only checks that the
round trip lands back on /intray and actually clears the queue; the routes'
own duplicate/finalize behavior is covered in test_dedup_triage.py.
"""
import os
from datetime import date, timedelta

import database
import app as klr
from conftest import make_pdf


TODAY = date.today()


def _day(n: int) -> str:
    """ISO date for day n of the current month (clamped to 28 so short
    months like February never overflow)."""
    return TODAY.replace(day=min(max(n, 1), 28)).isoformat()


def _day_in_month(ym: str, n: int) -> str:
    first = date.fromisoformat(ym + "-01")
    return first.replace(day=min(max(n, 1), 28)).isoformat()


def _prev_month_ym() -> str:
    last_day_prev_month = TODAY.replace(day=1) - timedelta(days=1)
    return last_day_prev_month.strftime("%Y-%m")


def _bill(**overrides):
    e = {"date_received": TODAY.isoformat(), "doc_type": "Bill", "vendor": "DESK STACK VENDOR",
         "amount": 88.50, "due_date": _day(20), "status": "Pending",
         "processed_by": "TestUser"}
    e.update(overrides)
    return database.insert_entry(e)


def _queue_item(env, filename, **overrides):
    """A minimal queued-scan dict backed by a real temp PDF in the scan
    folder, so confirm/skip's file-move has something real to move."""
    path = make_pdf(os.path.join(env.scan_folder, filename))
    d = {"vendor": "QUEUE VENDOR CO", "amount": 120.0, "due_date": _day(20),
         "entity_code": "TRUST1", "property_code": "T27", "doc_type": "Bill",
         "ai_confidence": "High", "batch_number": "BATCH-1",
         "received_date": TODAY.isoformat(), "scanned_date": TODAY.isoformat(),
         "pdf_path": path}
    d.update(overrides)
    return d


# ── Auth gate ───────────────────────────────────────────────────────────────

def test_anonymous_new_paper_views_redirect_to_login(client):
    for path in ("/desk", "/cabinet", "/rolodex", "/intray"):
        r = client.get(path)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]


# ── /desk ────────────────────────────────────────────────────────────────────

def test_desk_shows_batch_and_top_bill_vendor(env, staff_client):
    c, headers = staff_client
    _bill(vendor="DESK TOP BILL CO", due_date=_day(20))
    klr.pending_scans[1] = _queue_item(env, "desk1.pdf", batch_number="BATCH-DESK-9001")
    r = c.get("/desk")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "DESK TOP BILL CO" in html
    # desk.html renders the cover sheet's count/received_label, not the raw
    # batch_number string — this is the actual rendered text for a 1-item batch.
    assert "1 piece under this sheet" in html


def test_desk_sticky_for_expected_bill_not_arrived(env, staff_client):
    c, headers = staff_client
    database.add_expected_bill(vendor="GHOST DESK VENDOR", cadence="monthly")
    r = c.get("/desk")
    assert r.status_code == 200
    assert "see the radar" in r.get_data(as_text=True)


# ── /cabinet ─────────────────────────────────────────────────────────────────

def test_cabinet_shows_doc_for_entity_with_paid_chip(env, staff_client):
    c, headers = staff_client
    _bill(vendor="CABINET PAID CO", entity_code="TRUST1", status="Paid",
          paid_date=TODAY.isoformat())
    r = c.get("/cabinet?entity=TRUST1")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "CABINET PAID CO" in html
    # NOTE: cabinet.html's <style> block defines --chip-paid-bg/--chip-paid-edge,
    # so a bare "paid" in html.lower() is always true regardless of doc status.
    # ">paid<" is the chip's actual rendered text content, not the CSS var names.
    assert ">paid<" in html


def test_cabinet_month_scoping_hides_other_month_by_default(env, staff_client):
    c, headers = staff_client
    _bill(vendor="CABINET THIS MONTH CO", entity_code="TRUST1",
          date_received=TODAY.isoformat())
    prev_ym = _prev_month_ym()
    _bill(vendor="CABINET LAST MONTH CO", entity_code="TRUST1",
          date_received=_day_in_month(prev_ym, 10))

    r = c.get("/cabinet?entity=TRUST1")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "CABINET THIS MONTH CO" in html
    assert "CABINET LAST MONTH CO" not in html

    r2 = c.get(f"/cabinet?entity=TRUST1&month={prev_ym}")
    assert r2.status_code == 200
    assert "CABINET LAST MONTH CO" in r2.get_data(as_text=True)


# ── /rolodex ─────────────────────────────────────────────────────────────────

def test_rolodex_shows_vendor_and_last_payment(env, staff_client):
    c, headers = staff_client
    _bill(vendor="QUASAR POOL SERVICE", amount=123.45, status="Paid",
          paid_date=TODAY.isoformat())
    r = c.get("/rolodex")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "QUASAR POOL SERVICE" in html
    assert "$123.45" in html


def test_rolodex_letter_filter_scopes_vendor_list(env, staff_client):
    c, headers = staff_client
    _bill(vendor="APPLE ORCHARD LLC")
    _bill(vendor="ZEBRA STRIPES CO")

    default_html = c.get("/rolodex").get_data(as_text=True)
    assert "ZEBRA STRIPES CO" not in default_html

    z_html = c.get("/rolodex?letter=Z").get_data(as_text=True)
    assert "ZEBRA STRIPES CO" in z_html
    assert "APPLE ORCHARD LLC" not in z_html


# ── /intray ──────────────────────────────────────────────────────────────────

def test_intray_empty_state(env, staff_client):
    c, headers = staff_client
    r = c.get("/intray")
    assert r.status_code == 200
    assert "empty" in r.get_data(as_text=True).lower()


def test_intray_shows_vendor_position_and_confidence(env, staff_client):
    c, headers = staff_client
    klr.pending_scans[1] = _queue_item(env, "intray1.pdf", vendor="INTRAY SHEET VENDOR",
                                       ai_confidence="High")
    r = c.get("/intray")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "INTRAY SHEET VENDOR" in html
    assert "1" in html                     # sheet 1 of 1
    # intray.html pipes ai_confidence through |lower ("{{ ... |lower }} confidence"),
    # so the rendered text is "high confidence", not "High".
    assert "high confidence" in html.lower()


def test_intray_stamp_confirms_through_review_confirm_and_returns_to_intray(env, staff_client):
    c, headers = staff_client
    klr.pending_scans[1] = _queue_item(env, "stamp1.pdf", vendor="INTRAY STAMP VENDOR")
    r = c.post("/review/1/confirm", headers=headers, data={
        "entity_code": "TRUST1", "property_codes": ["T27"],
        "vendor": "INTRAY STAMP VENDOR", "amount": "250.00",
        "due_date": _day(20), "doc_type": "Bill",
        "date_received": TODAY.isoformat(), "return_to": "intray",
    })
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/intray")
    entries = database.get_all_entries()
    assert any(e["vendor"] == "INTRAY STAMP VENDOR" for e in entries)
    assert not klr._queue_items()


def test_intray_set_aside_skips_through_review_skip_and_returns_to_intray(env, staff_client):
    c, headers = staff_client
    klr.pending_scans[1] = _queue_item(env, "setaside1.pdf", vendor="INTRAY SKIP VENDOR")
    r = c.post("/review/1/skip", headers=headers, data={"return_to": "intray"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/intray")
    assert not klr._queue_items()


def test_intray_missing_file_disables_stamp_and_says_so(env, staff_client):
    """A queued sheet whose scan file vanished from disk (the 6/30 stragglers):
    the page must say the file is missing and must NOT offer the stamp, even
    when the AI reading is complete — confirming would log a record whose
    paper can't be found. Set-aside stays available as the recovery path."""
    c, headers = staff_client
    item = _queue_item(env, "ghostfile1.pdf", vendor="PHANTOM FILE VENDOR")
    os.remove(item["pdf_path"])              # the file is gone, the queue row isn't
    klr.pending_scans[1] = item
    r = c.get("/intray")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "missing from disk" in html
    assert item["pdf_path"] in html          # the dead path is shown for investigation
    assert "Stamp: approve" not in html      # no one-click filing of a phantom
    assert "Set aside" in html               # recovery path still offered


# ── _prior_payment_match ─────────────────────────────────────────────────────

def test_prior_payment_match_within_tolerance_and_account_mismatch(env):
    _bill(vendor="QUASAR POOL SERVICE", amount=100.00, status="Paid",
          paid_date=TODAY.isoformat(), account_number="A-1")

    match = klr._prior_payment_match("QUASAR POOL SERVICE", 95.00, "")
    assert match is not None
    assert match["amount"] == 100.00

    no_match = klr._prior_payment_match("QUASAR POOL SERVICE", 300.00, "B-9")
    assert no_match is None
