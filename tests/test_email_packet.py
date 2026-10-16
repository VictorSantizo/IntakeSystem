"""Reports #55/#57 (+#8/#26): the source email prints WITH the bill.

email_pdf.py typesets the stored email columns into PDF pages and appends
them behind the document — one printable packet, the order Karen staples
paper. Routes exist at both stages: /review/<sid>/packet.pdf for a queued
item, /entry/<id>/packet.pdf for a logged record. No stored email → the
routes fall back to the bare document instead of failing.
"""
import io
import os

from pypdf import PdfReader

import app as klr
import database
import email_pdf

from conftest import make_pdf
from test_dedup_triage import _queue_bill


def _email_fields():
    return dict(email_from="City Water <billing@water.example>",
                email_to="karen@klacks.com", email_account="Karen's Inbox",
                email_subject="Water bill June",
                email_date="Tue, 8 Jul 2026 09:00:00 -0700",
                email_body="Amount due $147.48.\nThank you for your business.")


# ── the module itself ─────────────────────────────────────────────────────────

def test_email_pdf_bytes_renders_header_and_body():
    data = email_pdf.email_pdf_bytes(_email_fields())
    assert data
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
    assert "SOURCE EMAIL" in text
    assert "billing@water.example" in text
    assert "Water bill June" in text
    assert "Amount due $147.48." in text


def test_email_pdf_bytes_none_without_stored_email():
    assert email_pdf.email_pdf_bytes({}) is None
    # the polling account alone is not email content
    assert email_pdf.email_pdf_bytes({"email_account": "Karen's Inbox"}) is None


def test_email_pdf_long_body_flows_over_pages():
    rec = dict(_email_fields(), email_body="A line of the email body.\n" * 400)
    data = email_pdf.email_pdf_bytes(rec)
    assert len(PdfReader(io.BytesIO(data)).pages) > 1


def test_packet_appends_email_behind_document(tmp_path):
    doc = str(tmp_path / "bill.pdf")
    make_pdf(doc, "INVOICE 470.00 pest control")
    packet = email_pdf.packet_pdf_bytes(doc, _email_fields())
    reader = PdfReader(io.BytesIO(packet))
    bill_pages = len(PdfReader(doc).pages)
    assert len(reader.pages) > bill_pages                  # email rides behind
    assert "INVOICE" in reader.pages[0].extract_text()     # document first
    assert "SOURCE EMAIL" in reader.pages[-1].extract_text()


def test_packet_none_without_email_or_document(tmp_path):
    doc = str(tmp_path / "bill.pdf")
    make_pdf(doc, "INVOICE")
    assert email_pdf.packet_pdf_bytes(doc, {}) is None                  # no email
    assert email_pdf.packet_pdf_bytes(str(tmp_path / "gone.pdf"),
                                      _email_fields()) is None          # no file


# ── the review-stage route ────────────────────────────────────────────────────

def test_review_packet_route_serves_packet(env, staff_client):
    c, _headers = staff_client
    sid = _queue_bill(env, "wb.pdf", "water bill text", **_email_fields())
    r = c.get(f"/review/{sid}/packet.pdf")
    assert r.status_code == 200
    text = "".join(p.extract_text()
                   for p in PdfReader(io.BytesIO(r.data)).pages)
    assert "SOURCE EMAIL" in text and "water bill" in text


def test_review_packet_route_falls_back_without_email(env, staff_client):
    c, _headers = staff_client
    sid = _queue_bill(env, "paper.pdf", "paper bill", mail_type="Physical Mail")
    r = c.get(f"/review/{sid}/packet.pdf")
    assert r.status_code == 200
    text = PdfReader(io.BytesIO(r.data)).pages[0].extract_text()
    assert "SOURCE EMAIL" not in (text or "")              # bare document served


def test_review_page_offers_print_with_email(env, staff_client):
    c, _headers = staff_client
    sid = _queue_bill(env, "wb2.pdf", "water bill text", **_email_fields())
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert f"/review/{sid}/packet.pdf" in html


# ── the record-stage route ────────────────────────────────────────────────────

def _logged_email_bill(env, tmp_name="logged.pdf"):
    path = os.path.join(env.scan_folder, tmp_name)
    make_pdf(path, "INVOICE from City Water $147.48")
    eid = database.insert_entry(dict(
        date_received="2026-07-08", doc_type="Bill", vendor="City Water",
        amount=147.48, status="Pending", mail_type="Email", pdf_path=path,
        **_email_fields()))
    return eid


def test_entry_packet_route_serves_packet(env, staff_client):
    c, _headers = staff_client
    eid = _logged_email_bill(env)
    r = c.get(f"/entry/{eid}/packet.pdf")
    assert r.status_code == 200
    text = "".join(p.extract_text()
                   for p in PdfReader(io.BytesIO(r.data)).pages)
    assert "SOURCE EMAIL" in text and "City Water" in text


def test_entry_packet_route_falls_back_without_email(env, staff_client):
    c, _headers = staff_client
    path = os.path.join(env.scan_folder, "plain.pdf")
    make_pdf(path, "INVOICE plain")
    eid = database.insert_entry(dict(
        date_received="2026-07-08", doc_type="Bill", vendor="Plain Vendor",
        amount=10.0, status="Pending", mail_type="Email", pdf_path=path))
    r = c.get(f"/entry/{eid}/packet.pdf", follow_redirects=False)
    assert r.status_code == 302                            # → bare /pdf route
    assert r.headers["Location"].endswith(f"/entry/{eid}/pdf")


# ── the email panel shows whenever an email is stored, any mail_type (#26) ────

def test_hand_filed_record_with_email_shows_panel(env, staff_client):
    c, _headers = staff_client
    path = os.path.join(env.scan_folder, "hand.pdf")
    make_pdf(path, "INVOICE hand-filed")
    eid = database.insert_entry(dict(
        date_received="2026-07-08", doc_type="Bill", vendor="Hand Vendor",
        amount=12.0, status="Pending", mail_type="Hand-filed", pdf_path=path,
        **_email_fields()))
    html = c.get(f"/entry/{eid}/view").get_data(as_text=True)
    assert "Source Email" in html
    assert f"/entry/{eid}/packet.pdf" in html
