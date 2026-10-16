"""The email backfill (reports #55/#8/#26, Joshua's 8/6 go-ahead): mailboxes
re-read, source emails matched to email-less documents by exact file hash.

Safety contract under test: matches are hash-certain only; existing email
columns are never overwritten; record received dates are never rewritten
(only reported); queue items DO get the email's date (no record number yet);
and the module never contains a RETR call — TOP/PEEK only, so no mailbox is
ever marked read (the 2026-07-07 MagicMail lesson, re-ordered by Joshua 8/6).
"""
import hashlib
import json
import os
from email.message import EmailMessage

import pytest

import app as klr
import database
import email_backfill
import queue_state


PDF_BYTES = b"%PDF-1.4 fake water bill bytes"
PDF_SHA = hashlib.sha256(PDF_BYTES).hexdigest()


def _bill_email(subject="Water bill June", sent="Tue, 8 Jul 2026 09:00:00 -0700",
                payload=PDF_BYTES):
    m = EmailMessage()
    m["Message-ID"] = "<retro@water.example>"
    m["From"] = "City Water <billing@water.example>"
    m["To"] = "karen@klacks.com"
    m["Subject"] = subject
    m["Date"] = sent
    m.set_content("Please find your bill attached.")
    m.add_attachment(payload, maintype="application", subtype="pdf",
                     filename="water.pdf")
    return m


def _fake_cfg():
    return {"mail_accounts": [{"id": 1, "name": "Test Box", "protocol": "imap",
                               "host": "x", "username": "u", "password": "p",
                               "enabled": True}]}


def _feed(messages):
    """A stand-in for _scan_imap that feeds canned messages to on_message."""
    def scanner(account, since, on_message):
        for m in messages:
            on_message(m)
        return len(messages), None
    return scanner


# ── the building blocks ───────────────────────────────────────────────────────

def test_attachment_hashes_match_ingest_filter():
    m = _bill_email()
    assert email_backfill._attachment_hashes(m) == [("water.pdf", PDF_SHA)]
    # tiny inline images (signature logos) are skipped, like ingest skips them
    m2 = EmailMessage()
    m2.set_content("body")
    m2.add_attachment(b"x" * 100, maintype="image", subtype="png",
                      filename="logo.png")
    assert email_backfill._attachment_hashes(m2) == []


def test_meta_mirrors_ingest_sidecar():
    meta = email_backfill._meta_from_msg(_bill_email(), {"name": "Test Box"})
    assert meta["email_from"].startswith("City Water")
    assert meta["email_account"] == "Test Box"
    assert meta["received_iso"] == "2026-07-08"
    assert "attached" in meta["email_body"]


def test_full_fetch_only_for_attachment_shaped_mail():
    assert email_backfill._wants_full_fetch("multipart/mixed; boundary=x")
    assert not email_backfill._wants_full_fetch("text/plain")
    assert not email_backfill._wants_full_fetch("multipart/alternative")


def test_no_retr_ever():
    """Joshua (8/6): never mark a mailbox read again. RETR is how that
    happened; the backfill must not contain one (the poller keeps its own
    guarded fallback — this guards THIS module)."""
    src = open(os.path.join(os.path.dirname(email_backfill.__file__),
                            "email_backfill.py"), encoding="utf-8").read()
    assert ".retr(" not in src
    assert "readonly=True" in src and "BODY.PEEK" in src


# ── matching logged records ───────────────────────────────────────────────────

def _seed_record(**over):
    data = dict(date_received="2026-07-08", doc_type="Bill",
                vendor="City Water", amount=147.48, status="Pending",
                mail_type="Email", file_hash=PDF_SHA)
    data.update(over)
    return database.insert_entry(data)


def test_dry_run_matches_but_writes_nothing(env, monkeypatch, tmp_path):
    eid = _seed_record()
    monkeypatch.setattr(email_backfill, "_scan_imap", _feed([_bill_email()]))
    s = email_backfill.run(_fake_cfg(), apply=False, out_dir=str(tmp_path),
                           queue_items=[], progress=lambda *a: None)
    assert [m["entry_id"] for m in s["record_matches"]] == [eid]
    assert not database.get_entry(eid)["email_from"]       # nothing written
    assert (tmp_path / "EMAIL_BACKFILL.html").exists()     # artifact always


def test_apply_fills_email_columns(env, monkeypatch, tmp_path):
    eid = _seed_record()
    monkeypatch.setattr(email_backfill, "_scan_imap", _feed([_bill_email()]))
    email_backfill.run(_fake_cfg(), apply=True, out_dir=str(tmp_path),
                       queue_items=[], progress=lambda *a: None)
    e = database.get_entry(eid)
    assert e["email_from"].startswith("City Water")
    assert e["email_subject"] == "Water bill June"
    assert "attached" in e["email_body"]
    assert e["date_received"] == "2026-07-08"              # untouched


def test_apply_reports_date_mismatch_without_rewriting(env, monkeypatch, tmp_path):
    eid = _seed_record(date_received="2026-07-23")         # pull-date era bug
    monkeypatch.setattr(email_backfill, "_scan_imap", _feed([_bill_email()]))
    s = email_backfill.run(_fake_cfg(), apply=True, out_dir=str(tmp_path),
                           queue_items=[], progress=lambda *a: None)
    assert database.get_entry(eid)["date_received"] == "2026-07-23"  # kept
    assert s["date_mismatches"] == [dict(entry_id=eid, record="2026-07-23",
                                         email="2026-07-08")]


def test_records_with_stored_email_are_not_targets(env, monkeypatch, tmp_path):
    eid = _seed_record(email_from="someone@else.example", email_body="hi")
    monkeypatch.setattr(email_backfill, "_scan_imap", _feed([_bill_email()]))
    s = email_backfill.run(_fake_cfg(), apply=True, out_dir=str(tmp_path),
                           queue_items=[], progress=lambda *a: None)
    assert s["record_matches"] == []                       # never overwritten
    assert database.get_entry(eid)["email_from"] == "someone@else.example"


# ── matching queue items ──────────────────────────────────────────────────────

def test_queue_item_gets_email_and_corrected_date(env, monkeypatch, tmp_path):
    sid = klr._queue_add({"pdf_path": "x.pdf", "mail_type": "Email",
                          "vendor": "City Water", "file_hash": PDF_SHA,
                          "received_date": "2026-07-23",   # pull-date fallback
                          "received_date_assumed": True})
    monkeypatch.setattr(email_backfill, "_scan_imap", _feed([_bill_email()]))
    s = email_backfill.run(_fake_cfg(), apply=True, out_dir=str(tmp_path),
                           queue_items=queue_state.queue_items(),
                           progress=lambda *a: None)
    d = queue_state.get_scan(sid)
    assert d["email_subject"] == "Water bill June"
    assert d["received_date"] == "2026-07-08"              # the email's own date
    assert "received_date_assumed" not in d
    assert s["queue_date_fixes"] == 1
    # and the change survived to the DB row the app reloads from
    raw = dict(database.get_all_pending_scans())[sid]
    assert json.loads(raw)["received_date"] == "2026-07-08"


# ── the settings button ───────────────────────────────────────────────────────

def test_settings_button_kicks_off_background_run(env, admin_client, monkeypatch):
    import threading
    c, headers = admin_client
    ran = threading.Event()
    monkeypatch.setattr(email_backfill, "run",
                        lambda *a, **kw: ran.set())
    r = c.post("/settings/email_backfill", headers=headers)
    assert r.status_code == 302
    assert ran.wait(5)
    for _ in range(50):                                    # worker clears its job flag
        if not queue_state.job_running("email_backfill"):
            break
        import time; time.sleep(0.1)
    assert not queue_state.job_running("email_backfill")


def test_settings_button_is_admin_only(env, staff_client):
    c, headers = staff_client
    r = c.post("/settings/email_backfill", headers=headers)
    assert r.status_code in (302, 403)
    if r.status_code == 302:                               # bounced to login/home
        assert "/settings/email_backfill" not in r.headers["Location"]


# ── the artifact must always land, somewhere real (the 2026-08-07 lesson) ─────

def test_default_out_dir_is_derived_not_a_desk_drive():
    """_OUT_DIR used to be the literal T:\\_OPS INDEX — real on the desks,
    absent for the server app, so the 08-07 APPLIED run wrote its 94 records
    and then silently failed to write this artifact; /ops-reports kept showing
    the 08-06 dry run. The default must be derived beside the app folder,
    exactly like routes_ops_reports.DEFAULT_OPS_DIR."""
    expected = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(email_backfill.__file__))), "_OPS INDEX")
    assert email_backfill._OUT_DIR == expected


def test_config_ops_index_dir_overrides_artifact_home(env, monkeypatch, tmp_path):
    _seed_record()
    monkeypatch.setattr(email_backfill, "_scan_imap", _feed([_bill_email()]))
    cfg = _fake_cfg()
    cfg["ops_index_dir"] = str(tmp_path / "ops")
    email_backfill.run(cfg, apply=False, queue_items=[], progress=lambda *a: None)
    assert (tmp_path / "ops" / "EMAIL_BACKFILL.html").exists()


def test_artifact_written_even_when_a_scan_crashes(env, monkeypatch, tmp_path):
    _seed_record()

    def boom(account, since, on_message):
        raise RuntimeError("mid-scan crash")

    monkeypatch.setattr(email_backfill, "_scan_imap", boom)
    with pytest.raises(RuntimeError):
        email_backfill.run(_fake_cfg(), apply=True, out_dir=str(tmp_path),
                           queue_items=[], progress=lambda *a: None)
    page = (tmp_path / "EMAIL_BACKFILL.html").read_text(encoding="utf-8")
    assert "stopped early" in page and "mid-scan crash" in page
    assert "APPLIED" in page                 # the mode is the run's real mode


def test_artifact_write_failure_is_loud(env, monkeypatch, tmp_path, capsys):
    _seed_record()
    monkeypatch.setattr(email_backfill, "_scan_imap", _feed([_bill_email()]))
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("a file where the ops dir should be")
    email_backfill.run(_fake_cfg(), apply=False, out_dir=str(blocker / "sub"),
                       queue_items=[], progress=lambda *a: None)
    assert "COULD NOT WRITE" in capsys.readouterr().out
