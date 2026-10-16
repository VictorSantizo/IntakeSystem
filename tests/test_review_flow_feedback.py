"""The review loop tells you what just happened (training session 2026-08-03).

Confirm auto-advances to the next queued item; that silent jump read as "did
it save? where did it go?" — so the redirect now carries ?logged=<entry id>
and the next page shows a green Logged toast with a way back to the record.
And because Karen's backlog is mostly bills she already paid by hand, the
review form's "already paid" checkbox logs a bill straight to Paid through
the same _mark_paid core as every other payment path (bills only, #27).
"""
import app as klr
import database

from test_dedup_triage import _queue_bill


def _confirm_form(**over):
    form = {"date_received": "2026-07-08", "mail_type": "Email",
            "doc_type": "Bill", "vendor": "City Water", "amount": "147.48",
            "due_date": "2026-07-20", "date_ok": "1",
            "entity_code": "TRUST1", "property_codes": ["T27"]}
    form.update(over)
    return form


def test_confirm_redirect_carries_logged_id(env, staff_client):
    c, headers = staff_client
    first = _queue_bill(env, "one.pdf", "water bill text one")
    _queue_bill(env, "two.pdf", "different gas bill text",
                vendor="Gas Co", amount=52.0)
    r = c.post(f"/review/{first}/confirm", headers=headers,
               data=_confirm_form())
    assert r.status_code == 302
    e = next(x for x in database.get_all_entries() if x["vendor"] == "City Water")
    assert f"logged={e['id']}" in r.headers["Location"]


def test_next_page_shows_logged_toast(env, staff_client):
    c, headers = staff_client
    first = _queue_bill(env, "one.pdf", "water bill text one")
    second = _queue_bill(env, "two.pdf", "different gas bill text",
                         vendor="Gas Co", amount=52.0)
    r = c.post(f"/review/{first}/confirm", headers=headers,
               data=_confirm_form(), follow_redirects=True)
    html = r.get_data(as_text=True)
    e = next(x for x in database.get_all_entries() if x["vendor"] == "City Water")
    assert "Logged" in html
    assert (e["record_no"] or f"#{e['id']}") in html
    assert f"/entry/{e['id']}/view" in html                # the way back


def test_last_item_confirm_reports_on_queue_page(env, staff_client):
    c, headers = staff_client
    only = _queue_bill(env, "only.pdf", "water bill text")
    r = c.post(f"/review/{only}/confirm", headers=headers,
               data=_confirm_form(), follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "all caught up" in html


# ── "already paid" logs straight to Paid ──────────────────────────────────────

def test_already_paid_logs_bill_as_paid(env, staff_client):
    c, headers = staff_client
    sid = _queue_bill(env, "paidbill.pdf", "water bill text")
    r = c.post(f"/review/{sid}/confirm", headers=headers,
               data=_confirm_form(already_paid="1", paid_date="2026-07-10",
                                  paid_method="Check", check_number="4417"))
    assert r.status_code == 302
    e = next(x for x in database.get_all_entries() if x["vendor"] == "City Water")
    assert e["status"] == "Paid"
    assert e["paid_date"] == "2026-07-10"
    assert e["paid_amount"] == 147.48
    assert e["payment_method"] == "Check"
    assert e["check_number"] == "4417"


def test_already_paid_defaults_paid_date_to_today(env, staff_client):
    from datetime import date
    c, headers = staff_client
    sid = _queue_bill(env, "paidbill2.pdf", "water bill text")
    c.post(f"/review/{sid}/confirm", headers=headers,
           data=_confirm_form(already_paid="1"))
    e = next(x for x in database.get_all_entries() if x["vendor"] == "City Water")
    assert e["status"] == "Paid"
    assert e["paid_date"] == date.today().isoformat()


def test_already_paid_ignored_for_non_bills(env, staff_client):
    """Only bills get Paid (#27) — a stray checkbox on another type must not
    invent a payment."""
    c, headers = staff_client
    sid = _queue_bill(env, "letter.pdf", "correspondence text")
    c.post(f"/review/{sid}/confirm", headers=headers,
           data=_confirm_form(doc_type="Correspondence", already_paid="1",
                              vendor="City Water"))
    e = next(x for x in database.get_all_entries() if x["vendor"] == "City Water")
    assert e["status"] != "Paid"
    assert not e["paid_date"]


def test_review_form_offers_already_paid_for_bills(env, staff_client):
    c, headers = staff_client
    sid = _queue_bill(env, "form.pdf", "water bill text")
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "already paid this bill" in html
