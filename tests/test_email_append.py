"""email_append.py — the permanent append engine: idempotence, the
filed_sha256 contract, multi-mailbox copies, the re-send attention flag, and
the after-sync pass."""
import email.message
import hashlib
import json
import os

import pytest

import database
import email_append
import mail_archive
from conftest import make_pdf

fitz = pytest.importorskip("fitz")

ACCT_LARRY = {"id": 2, "name": "Larry's Inbox"}
ACCT_KAREN = {"id": 3, "name": "Karen's Inbox"}


def _entry_with_pdf(tmp, with_email=True, doc_type="Bill", name="INVOICE.pdf"):
    path = make_pdf(tmp / name)
    with open(path, "rb") as f:
        raw = f.read()
    data = {
        "date_received": "2026-08-04", "mail_type": "Email",
        "doc_type": doc_type, "vendor": "Entrusted Services",
        "amount": 147.48, "status": "Pending",
        "filed_path": path, "pdf_path": path,
        "file_hash": hashlib.sha256(raw).hexdigest(),
    }
    if with_email:
        data.update(email_from="billing@entrusted.com", email_to="larry@silcom.com",
                    email_account="Larry's Inbox",
                    email_subject="Invoices attached",
                    email_date="Mon, 03 Aug 2026 10:00:00 -0700",
                    email_body="Please see the attached invoices.")
    eid = database.insert_entry(data)
    return eid, path, raw


def _pages(path):
    with fitz.open(path) as d:
        return d.page_count


def _archive_msg(account, msgid, payload, subject="Invoices attached",
                 date="Mon, 03 Aug 2026 10:00:00 -0700"):
    m = email.message.EmailMessage()
    m["From"] = "billing@entrusted.com"
    m["To"] = "larry@silcom.com; karen@klacks.com"
    m["Subject"] = subject
    m["Date"] = date
    m["Message-ID"] = msgid
    m.set_content("Please see the attached invoices.")
    m.add_attachment(payload, maintype="application", subtype="pdf",
                     filename="INVOICE.pdf")
    return mail_archive.store_message(account, m)


def test_append_own_columns_once_and_only_once(env):
    eid, path, raw = _entry_with_pdf(env.tmp)
    before = _pages(path)

    res = email_append.append_to_entry(eid)
    assert res == {"appended": 1}
    assert _pages(path) > before

    e = database.get_entry(eid)
    assert "record:own" in json.loads(e["email_appended"])
    on_disk = hashlib.sha256(open(path, "rb").read()).hexdigest()
    assert e["filed_sha256"] == on_disk
    assert e["file_hash"] == hashlib.sha256(raw).hexdigest() != on_disk

    # Idempotent: a second call must not grow the file again.
    pages_after = _pages(path)
    assert email_append.append_to_entry(eid) == {"appended": 0}
    assert _pages(path) == pages_after


def test_filed_sha256_keeps_hash_lookup_alive(env):
    """After appending, the bytes on disk no longer match file_hash — the
    dedup/heal lookup must find the record by filed_sha256 instead."""
    eid, path, _raw = _entry_with_pdf(env.tmp)
    email_append.append_to_entry(eid)
    on_disk = hashlib.sha256(open(path, "rb").read()).hexdigest()
    assert [e["id"] for e in database.get_entries_by_hash(on_disk)] == [eid]


def test_append_every_mailbox_copy(env):
    eid, path, raw = _entry_with_pdf(env.tmp, with_email=False)
    _archive_msg(ACCT_LARRY, "<send1@x>", raw)   # same send, two inboxes
    _archive_msg(ACCT_KAREN, "<send1@x>", raw)

    res = email_append.append_to_entry(eid)
    assert res == {"appended": 2}                 # one page-set per mailbox
    keys = json.loads(database.get_entry(eid)["email_appended"])
    assert len(keys) == 2

    # A genuine re-send later: only the NEW message is appended.
    _archive_msg(ACCT_KAREN, "<send2@x>", raw, subject="SECOND NOTICE",
                 date="Sun, 09 Aug 2026 08:00:00 -0700")
    assert email_append.append_to_entry(eid) == {"appended": 1}
    assert len(json.loads(database.get_entry(eid)["email_appended"])) == 3


def test_own_columns_copy_not_duplicated_by_its_archive_twin(env):
    """Filed before the archive knew the message (own-columns append), then
    the archive catches up with the SAME email: no double append."""
    eid, path, raw = _entry_with_pdf(env.tmp)          # email_account=Larry's
    assert email_append.append_to_entry(eid) == {"appended": 1}
    _archive_msg(ACCT_LARRY, "<send1@x>", raw)         # the same send arrives
    assert email_append.append_to_entry(eid) == {"appended": 0}
    # ...but Karen's copy of that send is a NEW fact and does get appended.
    _archive_msg(ACCT_KAREN, "<send1@x>", raw)
    assert email_append.append_to_entry(eid) == {"appended": 1}


def _make_docx(path, text="INVOICE from Entrusted Services, amount due $147.48"):
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml",
                   f"<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r>"
                   f"</w:p></w:body></w:document>")
    return str(path)


def _entry_for_file(path, raw):
    return database.insert_entry({
        "date_received": "2026-08-04", "doc_type": "Bill", "vendor": "V",
        "filed_path": str(path), "pdf_path": str(path),
        "file_hash": hashlib.sha256(raw).hexdigest(),
        "email_from": "a@b.c", "email_subject": "s", "email_body": "b",
    })


def test_word_doc_converts_to_pdf_and_gets_the_email(env):
    """Joshua 2026-08-10: don't skip non-PDFs — print them to PDF, ESPECIALLY
    Word docs. The PDF becomes the filed primary; the original stays on the
    record as a copy."""
    docx = _make_docx(env.tmp / "LEASE.docx")
    raw = open(docx, "rb").read()
    eid = _entry_for_file(docx, raw)
    res = email_append.append_to_entry(eid)
    assert res == {"appended": 1}
    e = database.get_entry(eid)
    assert e["filed_path"].lower().endswith(".pdf")
    assert os.path.exists(e["filed_path"])
    assert _pages(e["filed_path"]) >= 2          # content page(s) + email page
    assert os.path.exists(docx)                  # original never deleted
    atts = database.get_attachments(eid)
    assert [a["source"] for a in atts] == ["Original file"]
    assert atts[0]["file_path"] == str(docx)
    on_disk = hashlib.sha256(open(e["filed_path"], "rb").read()).hexdigest()
    assert e["filed_sha256"] == on_disk
    assert e["file_hash"] == hashlib.sha256(raw).hexdigest()   # original bytes


def test_image_scan_converts_and_gets_the_email(env):
    from PIL import Image
    jpg = env.tmp / "SCAN.jpg"
    Image.new("RGB", (200, 280), "white").save(str(jpg))
    raw = open(jpg, "rb").read()
    eid = _entry_for_file(jpg, raw)
    assert email_append.append_to_entry(eid) == {"appended": 1}
    e = database.get_entry(eid)
    assert e["filed_path"].lower().endswith(".pdf")
    assert _pages(e["filed_path"]) >= 2
    assert os.path.exists(jpg)


def test_non_pdf_dry_run_reports_conversion_without_converting(env):
    docx = _make_docx(env.tmp / "LEASE.docx")
    eid = _entry_for_file(docx, open(docx, "rb").read())
    res = email_append.append_to_entry(eid, dry=True)
    assert res.get("would_convert") == "LEASE.docx"
    assert database.get_entry(eid)["filed_path"] == str(docx)   # untouched


def test_unconvertible_file_skips_with_a_reason(env):
    bad = env.tmp / "NOTES.docx"
    bad.write_bytes(b"not really a docx")
    eid = _entry_for_file(bad, b"not really a docx")
    res = email_append.append_to_entry(eid)
    assert "could not convert" in res.get("skipped", "")
    assert database.get_entry(eid)["filed_path"] == str(bad)    # untouched


def test_flag_resend_payables_only_and_idempotent(env):
    eid, _path, _raw = _entry_with_pdf(env.tmp)
    entry = database.get_entry(eid)
    assert email_append.flag_resend(entry, "another copy arrived by email on 2026-08-10")
    e = database.get_entry(eid)
    assert e["important"] == 1
    assert "re-sent this bill" in e["important_reason"]
    assert e["flagged_at"]
    # same note twice -> no second write
    assert not email_append.flag_resend(e, "another copy arrived by email on 2026-08-10")

    other = database.insert_entry({"date_received": "2026-08-04",
                                   "doc_type": "Correspondence", "vendor": "V"})
    assert not email_append.flag_resend(database.get_entry(other), "x")
    assert database.get_entry(other)["important"] in (0, None)


def test_process_new_messages_appends_and_flags(env):
    eid, path, raw = _entry_with_pdf(env.tmp, with_email=False)
    _mid1, _ = _archive_msg(ACCT_LARRY, "<send1@x>", raw)
    mid1b, _ = _archive_msg(ACCT_KAREN, "<send1@x>", raw)
    out = email_append.process_new_messages([mid1b])
    assert out["appended"] >= 1
    assert out["flagged"] == 0                    # one send, two inboxes

    mid2, _ = _archive_msg(ACCT_KAREN, "<send2@x>", raw, subject="SECOND NOTICE",
                           date="Sun, 09 Aug 2026 08:00:00 -0700")
    out = email_append.process_new_messages([mid2])
    assert out["flagged"] == 1                    # a real re-send
    assert database.get_entry(eid)["important"] == 1


def test_retro_append_flags_resends_too(env):
    """The live after-sync pass flags re-sends as mail arrives — but mail
    archived while the app was mid-restart never got that pass. The retro
    carries the flags for the backlog."""
    eid, _path, raw = _entry_with_pdf(env.tmp, with_email=False)
    _archive_msg(ACCT_LARRY, "<send1@x>", raw)
    _archive_msg(ACCT_KAREN, "<send2@x>", raw, subject="SECOND NOTICE",
                 date="Sun, 09 Aug 2026 08:00:00 -0700")
    summary = email_append.retro_append(apply=True, out_dir=str(env.tmp / "ops"))
    assert summary["flagged"] == 1
    e = database.get_entry(eid)
    assert e["important"] == 1 and "re-sent" in e["important_reason"]


def test_retro_append_dry_run_writes_nothing(env):
    eid, path, _raw = _entry_with_pdf(env.tmp)
    before = _pages(path)
    summary = email_append.retro_append(apply=False, out_dir=str(env.tmp / "ops"))
    assert not summary["apply"]
    assert any(r["entry_id"] == eid for r in summary["appended"])
    assert _pages(path) == before
    assert not database.get_entry(eid)["email_appended"]
    assert (env.tmp / "ops" / "EMAIL_APPEND.html").exists()

    applied = email_append.retro_append(apply=True, out_dir=str(env.tmp / "ops"))
    assert any(r["entry_id"] == eid for r in applied["appended"])
    assert _pages(path) > before
