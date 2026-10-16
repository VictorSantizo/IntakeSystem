"""/mail — the all-mailbox screen: rendering, both filter directions, the
print page's Attachments certainty ('None' vs the list with paths), and the
put-it-at-the-back action."""
import email.message
import hashlib
import json

import database
import email_append
import mail_archive
from conftest import make_pdf

ACCT_LARRY = {"id": 2, "name": "Larry's Inbox"}
ACCT_KAREN = {"id": 3, "name": "Karen's Inbox"}


def _store(account, msgid, subject, body="body text", atts=()):
    m = email.message.EmailMessage()
    m["From"] = "billing@entrusted.com"
    m["To"] = "office@klacks.com"
    m["Subject"] = subject
    m["Date"] = "Mon, 03 Aug 2026 10:00:00 -0700"
    m["Message-ID"] = msgid
    m.set_content(body)
    for fname, payload in atts:
        m.add_attachment(payload, maintype="application", subtype="pdf",
                         filename=fname)
    return mail_archive.store_message(account, m)[0]


def test_mail_page_renders_empty_archive(staff_client):
    c, _h = staff_client
    r = c.get("/mail")
    assert r.status_code == 200
    assert b"Sync mailboxes" in r.data
    assert b"archive is empty" in r.data


def test_search_and_mailbox_filter_compose(staff_client):
    c, _h = staff_client
    _store(ACCT_LARRY, "<e1@x>", "Invoice from Entrusted Services")
    _store(ACCT_KAREN, "<e2@x>", "Entrusted Services statement")
    _store(ACCT_KAREN, "<o1@x>", "Weekly newsletter")

    page = c.get("/mail?q=entrusted+services").data.decode()
    assert "Invoice from Entrusted Services" in page
    assert "Entrusted Services statement" in page
    assert "Weekly newsletter" not in page

    # search + mailbox filter, both directions of the same querystring
    for url in ("/mail?q=entrusted+services&account=Larry%27s+Inbox",
                "/mail?account=Larry%27s+Inbox&q=entrusted+services"):
        page = c.get(url).data.decode()
        assert "Invoice from Entrusted Services" in page
        assert "Entrusted Services statement" not in page
        assert "narrowed by your filters" in page

    # every row says which mailbox it came from (Jinja escapes the apostrophe)
    page = c.get("/mail").data.decode()
    assert "Larry&#39;s Inbox" in page and "Karen&#39;s Inbox" in page


def test_doc_type_filter_via_matched_record(staff_client, env):
    c, _h = staff_client
    pdf = make_pdf(env.tmp / "INV.pdf")
    raw = open(pdf, "rb").read()
    database.insert_entry({"date_received": "2026-08-04", "doc_type": "Bill",
                           "vendor": "Entrusted Services", "filed_path": pdf,
                           "file_hash": hashlib.sha256(raw).hexdigest()})
    _store(ACCT_LARRY, "<b1@x>", "Bill attached", atts=[("INV.pdf", raw)])
    _store(ACCT_LARRY, "<c1@x>", "Just correspondence")

    page = c.get("/mail?doc_type=Bill").data.decode()
    assert "Bill attached" in page
    assert "Just correspondence" not in page


def test_print_page_attachments_none_and_list(staff_client, env):
    c, _h = staff_client
    none_id = _store(ACCT_KAREN, "<p1@x>", "No files here")
    r = c.get(f"/mail/{none_id}/print").data.decode()
    assert "Attachments: None" in r
    assert "Karen&#39;s Inbox" in r

    pdf = make_pdf(env.tmp / "INV.pdf")
    raw = open(pdf, "rb").read()
    eid = database.insert_entry({"date_received": "2026-08-04", "doc_type": "Bill",
                                 "vendor": "Entrusted Services", "filed_path": pdf,
                                 "file_hash": hashlib.sha256(raw).hexdigest()})
    has_id = _store(ACCT_LARRY, "<p2@x>", "Invoice attached",
                    atts=[("INV.pdf", raw)])
    r = c.get(f"/mail/{has_id}/print").data.decode()
    assert "Attachments:" in r and "Attachments: None" not in r
    assert "INV.pdf" in r
    assert "filed as" in r          # provenance line for the filed document
    assert f"/entry/{eid}/view" in r


def test_append_action_from_mail_page(staff_client, env):
    c, h = staff_client
    pdf = make_pdf(env.tmp / "INV.pdf")
    raw = open(pdf, "rb").read()
    eid = database.insert_entry({"date_received": "2026-08-04", "doc_type": "Bill",
                                 "vendor": "Entrusted Services", "filed_path": pdf,
                                 "pdf_path": pdf,
                                 "file_hash": hashlib.sha256(raw).hexdigest()})
    mid = _store(ACCT_LARRY, "<a1@x>", "Invoice attached", atts=[("INV.pdf", raw)])

    view = c.get(f"/mail/{mid}").data.decode()
    assert "Put this email at the back of the filed PDF" in view

    r = c.post(f"/mail/{mid}/append/{eid}", headers=h)
    assert r.status_code == 302
    e = database.get_entry(eid)
    assert json.loads(e["email_appended"])
    assert e["filed_sha256"]

    view = c.get(f"/mail/{mid}").data.decode()
    assert "email at the back of the filed PDF" in view


def test_nav_carries_the_mail_link(staff_client):
    c, _h = staff_client
    page = c.get("/bills").data.decode()
    assert 'data-kk="nav-mail"' in page


def test_resend_bubble_links_to_the_carrying_emails(staff_client, env):
    """Joshua 2026-08-10: click the 'arrived under N separate emails' bubble
    and land on the Mail list showing exactly those emails."""
    c, _h = staff_client
    payload = b"%PDF the resent invoice"
    sha = hashlib.sha256(payload).hexdigest()
    _store(ACCT_LARRY, "<r1@x>", "Invoice", atts=[("inv.pdf", payload)])
    _store(ACCT_KAREN, "<r2@x>", "SECOND NOTICE", atts=[("inv.pdf", payload)])
    _store(ACCT_KAREN, "<other@x>", "Unrelated newsletter")

    page = c.get("/mail").data.decode()
    assert f'href="/mail?sha={sha}"' in page       # the bubble is a link

    narrowed = c.get(f"/mail?sha={sha}").data.decode()
    assert "Invoice" in narrowed and "SECOND NOTICE" in narrowed
    assert "Unrelated newsletter" not in narrowed
    assert "inv.pdf" in narrowed                   # the banner names the file
    assert "Show all mail" in narrowed


def test_unsaved_attachment_serves_the_shared_copy(staff_client, env):
    c, _h = staff_client
    payload = b"PNG-ish photo bytes not ingested as a document"
    sha = hashlib.sha256(payload).hexdigest()
    mid = _store(ACCT_LARRY, "<b1@x>", "Photo attached",
                 atts=[("photo.png", payload)])

    view = c.get(f"/mail/{mid}").data.decode()
    assert f"/mail/attachment/{sha}" in view       # clickable now

    r = c.get(f"/mail/attachment/{sha}")
    assert r.status_code == 200 and r.data == payload
    assert c.get("/mail/attachment/" + "0" * 64).status_code == 404
    assert c.get("/mail/attachment/not-a-sha").status_code == 404


def test_resend_and_important_flags_show_in_the_mailbox(staff_client, env):
    """Joshua 2026-08-10: the flags belong in the new mailbox too. A re-sent
    attachment wears a red arrived-again badge; a flagged matched record puts
    the orange flag on the email's row."""
    c, _h = staff_client
    pdf = make_pdf(env.tmp / "INV.pdf")
    raw = open(pdf, "rb").read()
    database.insert_entry({"date_received": "2026-06-27", "doc_type": "Bill",
                           "vendor": "Blue Iris Management", "filed_path": pdf,
                           "file_hash": hashlib.sha256(raw).hexdigest(),
                           "important": 1,
                           "important_reason": "Blue Iris re-sent this bill"})
    _store(ACCT_LARRY, "<s1@x>", "Invoice attached", atts=[("INV.pdf", raw)])
    mid2 = _store(ACCT_KAREN, "<s2@x>", "SECOND NOTICE", atts=[("INV.pdf", raw)])

    page = c.get("/mail").data.decode()
    assert "arrived under 2 separate emails" in page      # row badge title
    assert "Blue Iris re-sent this bill" in page          # flag tooltip

    view = c.get(f"/mail/{mid2}").data.decode()
    assert "arrived under 2 separate emails" in view

    printed = c.get(f"/mail/{mid2}/print").data.decode()
    assert "arrived under 2 separate emails" in printed
