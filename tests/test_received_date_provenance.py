"""Reports #56/#49/#43 — the received date says where it came from.

Karen's rule (training session 2026-08-03): "the most important thing we have
in paying the bills is the date we receive them" — the email's own date for
email documents, the stamp on the back for physical mail. The review form now
keeps Date Received in the always-visible main grid with a provenance chip,
and for email documents offers the email's own date as a one-click fix when
the stamped value disagrees.
"""
import app as klr
import routes_review

from test_dedup_triage import _queue_bill

EMAIL_DATE_RAW = "Tue, 8 Jul 2026 09:00:00 -0700"


# ── the header-date parser ────────────────────────────────────────────────────

def test_email_header_date_parses_to_iso():
    assert routes_review._email_header_date_iso(EMAIL_DATE_RAW) == "2026-07-08"


def test_email_header_date_garbage_is_empty():
    assert routes_review._email_header_date_iso("") == ""
    assert routes_review._email_header_date_iso(None) == ""
    assert routes_review._email_header_date_iso("not a date at all") == ""


# ── the chips on the review form ──────────────────────────────────────────────

def test_email_item_matching_date_shows_green_chip(env, staff_client):
    c, _headers = staff_client
    sid = _queue_bill(env, "match.pdf", "bill text",
                      received_date="2026-07-08", email_date=EMAIL_DATE_RAW)
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "matches the email" in html
    assert "Use email date" not in html


def test_email_item_mismatch_offers_one_click_fix(env, staff_client):
    c, _headers = staff_client
    sid = _queue_bill(env, "mismatch.pdf", "bill text",
                      received_date="2026-07-23", email_date=EMAIL_DATE_RAW)
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "the email itself says" in html and "2026-07-08" in html
    assert "Use email date" in html


def test_email_item_without_email_date_warns(env, staff_client):
    c, _headers = staff_client
    sid = _queue_bill(env, "nodate.pdf", "bill text",
                      received_date="2026-07-23")   # mail_type Email, no header
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "email date unknown" in html


def test_stamped_date_shows_stamp_chip(env, staff_client):
    c, _headers = staff_client
    sid = _queue_bill(env, "stamped.pdf", "bill text",
                      mail_type="Physical Mail", received_date="2026-07-20",
                      stamped_received_date="2026-07-20")
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "received stamp on the document" in html


def test_assumed_date_warns_check_the_stamp(env, staff_client):
    c, _headers = staff_client
    sid = _queue_bill(env, "assumed.pdf", "bill text",
                      mail_type="Physical Mail", received_date="2026-07-20",
                      received_date_assumed=True)
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "check the stamp on the back" in html


def test_received_date_is_in_the_main_grid_not_more_details(env, staff_client):
    """The field must be on screen without a disclosure click: it sits BEFORE
    the More-details toggle in the form, not inside the collapsed box."""
    c, _headers = staff_client
    sid = _queue_bill(env, "grid.pdf", "bill text")
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert html.index('name="date_received"') < html.index('id="more-details"')
