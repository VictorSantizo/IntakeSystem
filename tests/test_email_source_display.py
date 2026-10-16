"""Report #8: the source email visible on the review page and record view —
including who it was TO / which mailbox pulled it — plus Reply / Forward links
that open the staff member's own mail program (the app never sends mail)."""
import json
import os
from email.message import EmailMessage

import app as klr
import database
import email_poller

from test_dedup_triage import _queue_bill


# ── the poller captures To + which account fetched it ─────────────────────────

def test_save_attachments_meta_has_to_and_account(tmp_path):
    m = EmailMessage()
    m["Message-ID"] = "<m@x>"
    m["From"] = "City Water <billing@water.example>"
    m["To"] = "karen@klacks.com"
    m["Subject"] = "Water bill"
    m.set_content("attached")
    m.add_attachment(b"%PDF-1.4 fake", maintype="application",
                     subtype="pdf", filename="water.pdf")
    account = {"id": 2, "name": "Karen's Inbox"}
    assert email_poller._save_attachments(m, str(tmp_path), account) == 1
    meta = json.load(open(str(tmp_path / "water.pdf.meta.json"), encoding="utf-8"))
    assert meta["to"] == "karen@klacks.com"
    assert meta["account"] == "Karen's Inbox"


# ── mailto helper ──────────────────────────────────────────────────────────────

def test_email_mailto_reply_and_forward():
    e = {"email_from": "billing@water.example", "email_to": "karen@klacks.com",
         "email_subject": "Water bill", "email_date": "Tue, 8 Jul 2026 09:00:00",
         "email_body": "Your bill is attached."}
    reply = klr.email_mailto(e, "reply")
    assert reply.startswith("mailto:billing@water.example?")
    assert "Re%3A%20Water%20bill" in reply
    assert "Original%20message" in reply and "attached" in reply
    fwd = klr.email_mailto(e, "forward")
    assert fwd.startswith("mailto:?")                      # forward: recipient blank
    assert "Fwd%3A%20Water%20bill" in fwd
    # an already-Re: subject isn't double-prefixed on reply
    e2 = dict(e, email_subject="Re: Water bill")
    assert "Re%3A%20Re%3A" not in klr.email_mailto(e2, "reply")


# ── review page shows the panel ────────────────────────────────────────────────

def _email_fields():
    return dict(email_from="City Water <billing@water.example>",
                email_to="karen@klacks.com", email_account="Karen's Inbox",
                email_subject="Water bill June", email_date="Tue, 8 Jul 2026",
                email_body="Amount due $147.48. Thank you.")


def test_review_page_shows_source_email_panel(env, staff_client):
    c, headers = staff_client
    sid = _queue_bill(env, "wb.pdf", "water bill text", **_email_fields())
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "Source email" in html
    assert "karen@klacks.com" in html                      # the To line
    assert "Karen&#39;s Inbox" in html or "Karen's Inbox" in html
    assert "mailto:billing@water.example" in html          # Reply link
    assert "Fwd%3A" in html                                # Forward link
    assert "Amount due $147.48" in html                    # body toggle content


def test_review_page_no_panel_for_paper_mail(env, staff_client):
    c, headers = staff_client
    sid = _queue_bill(env, "paper.pdf", "paper bill", mail_type="Physical Mail")
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "Source email" not in html


# ── confirm persists To/account; record view shows them ───────────────────────

def test_confirm_persists_and_record_view_shows(env, staff_client):
    c, headers = staff_client
    sid = _queue_bill(env, "wb2.pdf", "water bill text", **_email_fields())
    r = c.post(f"/review/{sid}/confirm", headers=headers, data={
        "date_received": "2026-07-08", "mail_type": "Email", "doc_type": "Bill",
        "vendor": "City Water", "amount": "147.48", "due_date": "2026-07-20",
        "date_ok": "1",   # bypass the year-sanity guard as the fixed dates age
        "entity_code": "TRUST1", "property_codes": ["T27"],
    })
    assert r.status_code == 302
    e = next(x for x in database.get_all_entries() if x["vendor"] == "City Water")
    assert e["email_to"] == "karen@klacks.com"
    assert e["email_account"] == "Karen's Inbox"
    html = c.get(f"/entry/{e['id']}/view").get_data(as_text=True)
    assert "karen@klacks.com" in html                      # To shown on the record
    assert "arrived in" in html                            # mailbox name shown
    assert "mailto:billing@water.example" in html          # Reply from the record too
