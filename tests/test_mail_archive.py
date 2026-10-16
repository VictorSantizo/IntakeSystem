"""mail_archive.py — the local all-mailbox archive: storage, per-mailbox
dedup, search+filter composition, and (most important) the read-only mailbox
contract. The source-grep test is the same guard test_email_backfill carries:
the fetch verb that marks mail read must never reappear in this module."""
import email.message
import hashlib
import os

import mail_archive

ACCT_LARRY = {"id": 2, "name": "Larry's Inbox", "username": "larry@silcom.com"}
ACCT_KAREN = {"id": 3, "name": "Karen's Inbox", "username": "karen@klacks.com"}


def _msg(subject="Invoice from Entrusted Services", frm="billing@entrusted.com",
         to="larry@silcom.com", date="Mon, 03 Aug 2026 10:00:00 -0700",
         body="Copies of the invoices are attached.",
         msgid="<abc123@entrusted.com>", atts=()):
    m = email.message.EmailMessage()
    m["From"] = frm
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = date
    if msgid:
        m["Message-ID"] = msgid
    m.set_content(body)
    for fname, payload in atts:
        m.add_attachment(payload, maintype="application", subtype="pdf",
                         filename=fname)
    return m


def test_same_email_stored_once_per_mailbox(env):
    payload = b"%PDF-1.4 fake invoice bytes"
    msg = _msg(atts=[("invoice1.pdf", payload)])
    id1, new1 = mail_archive.store_message(ACCT_LARRY, msg)
    id2, new2 = mail_archive.store_message(ACCT_KAREN, msg)
    again, new3 = mail_archive.store_message(ACCT_LARRY, msg)
    assert new1 and new2 and not new3
    assert again == id1 and id1 != id2

    rows = mail_archive.query()
    assert len(rows) == 2
    accounts = {r["account_name"] for r in rows}
    assert accounts == {"Larry's Inbox", "Karen's Inbox"}
    for r in rows:
        assert r["attachment_count"] == 1
        assert r["attachments"][0]["filename"] == "invoice1.pdf"
        assert r["attachments"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
        assert "invoices are attached" in r["body"]


def test_message_without_message_id_is_kept_not_dropped(env):
    msg = _msg(msgid=None)
    _, new1 = mail_archive.store_message(ACCT_LARRY, msg)
    _, new2 = mail_archive.store_message(ACCT_LARRY, msg)   # same synthetic key
    assert new1 and not new2
    assert len(mail_archive.query()) == 1


def test_search_and_mailbox_filter_compose_both_ways(env):
    mail_archive.store_message(ACCT_LARRY, _msg(msgid="<e1@x>"))
    mail_archive.store_message(ACCT_KAREN, _msg(msgid="<e2@x>",
                                                subject="Entrusted Services statement"))
    mail_archive.store_message(ACCT_KAREN, _msg(msgid="<o1@x>",
                                                frm="news@other.com",
                                                subject="Weekly newsletter",
                                                body="nothing relevant"))
    # search alone: both Entrusted emails, across mailboxes
    hits = mail_archive.query(q="entrusted services")
    assert {h["message_key"] for h in hits} == {"<e1@x>", "<e2@x>"}
    # search then filter — and filter then search is the same call shape,
    # so the composed result must be the single Larry hit either way
    both = mail_archive.query(q="entrusted services", account="Larry's Inbox")
    assert [h["message_key"] for h in both] == ["<e1@x>"]
    # filter alone
    assert len(mail_archive.query(account="Karen's Inbox")) == 2


def test_date_and_attachment_filters(env):
    mail_archive.store_message(ACCT_LARRY, _msg(
        msgid="<old@x>", date="Wed, 01 Jul 2026 09:00:00 -0700"))
    mail_archive.store_message(ACCT_LARRY, _msg(
        msgid="<new@x>", date="Fri, 07 Aug 2026 09:00:00 -0700",
        atts=[("bill.pdf", b"%PDF bill")]))
    assert [m["message_key"] for m in
            mail_archive.query(date_from="2026-08-01")] == ["<new@x>"]
    assert [m["message_key"] for m in
            mail_archive.query(date_to="2026-07-31")] == ["<old@x>"]
    assert [m["message_key"] for m in
            mail_archive.query(has_att="yes")] == ["<new@x>"]
    assert [m["message_key"] for m in
            mail_archive.query(has_att="no")] == ["<old@x>"]


def test_messages_for_hash_and_distinct_sends(env):
    payload = b"%PDF-1.4 the same invoice"
    sha = hashlib.sha256(payload).hexdigest()
    one_send = _msg(msgid="<send1@x>", atts=[("inv.pdf", payload)])
    mail_archive.store_message(ACCT_LARRY, one_send)      # same message,
    mail_archive.store_message(ACCT_KAREN, one_send)      # two inboxes
    assert len(mail_archive.messages_for_hash(sha)) == 2
    assert mail_archive.distinct_sends_for_hash(sha) == 1  # NOT a re-send

    resend = _msg(msgid="<send2@x>", subject="SECOND NOTICE",
                  date="Sun, 09 Aug 2026 08:00:00 -0700",
                  atts=[("inv.pdf", payload)])
    mail_archive.store_message(ACCT_KAREN, resend)
    assert len(mail_archive.messages_for_hash(sha)) == 3
    assert mail_archive.distinct_sends_for_hash(sha) == 2  # a real re-send


def test_identical_attachments_share_one_stored_copy(env):
    """Joshua 2026-08-10: 'if the identical attachments are in each email, can
    we just link them to the same file?' — content-addressed: one blob per
    unique sha256, shared by every carrying email."""
    import os
    payload = b"PNG-ish signature image bytes"
    sha = hashlib.sha256(payload).hexdigest()
    msg = _msg(atts=[("image.png", payload)])
    mail_archive.store_message(ACCT_LARRY, msg)
    mail_archive.store_message(ACCT_KAREN, msg)

    b = mail_archive.blob_for(sha)
    assert b and os.path.exists(b["blob_path"])
    assert open(b["blob_path"], "rb").read() == payload
    blob_files = os.listdir(os.path.dirname(b["blob_path"]))
    assert len(blob_files) == 1                    # ONE file for both emails
    assert mail_archive.blobless_count() == 0


def test_rehydrate_backfills_missing_copies(env):
    """Mail archived before the copy store: attach_blobs restores the copy
    from a re-fetched message and repoints every row with those bytes."""
    import os
    import sqlite3
    payload = b"%PDF older archived attachment"
    sha = hashlib.sha256(payload).hexdigest()
    msg = _msg(atts=[("inv.pdf", payload)])
    mail_archive.store_message(ACCT_LARRY, msg)
    # simulate the pre-copy-store archive: no blob on disk, row unswept
    old = mail_archive.blob_for(sha)
    os.remove(old["blob_path"])
    conn = sqlite3.connect(mail_archive.archive_path())
    conn.execute("UPDATE message_attachments SET blob_path='', blob_tried=0")
    conn.commit()
    conn.close()
    assert mail_archive.blob_for(sha) is None
    assert mail_archive.blobless_count() == 1

    assert mail_archive.attach_blobs(msg) == 1
    b = mail_archive.blob_for(sha)
    assert b and open(b["blob_path"], "rb").read() == payload
    assert mail_archive.blobless_count() == 0


def test_source_never_uses_the_flag_risky_fetch_verbs():
    """The MagicMail lesson, pinned the same way test_email_backfill pins it:
    no RETR (POP3) and no STORE (IMAP flag writes) anywhere in the module;
    IMAP selects readonly and fetches with PEEK; POP3 never DELEs."""
    src_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "mail_archive.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    assert ".retr(" not in src
    assert ".dele(" not in src
    assert ".store(" not in src          # imaplib flag writes
    assert "readonly=True" in src
    assert "BODY.PEEK" in src
