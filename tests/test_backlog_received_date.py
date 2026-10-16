"""Report #90 follow-on, the 08. AUGUST incident: four stranded June scans
(file dates 6/24, 6/29) confirmed on 8/5 got date_received = confirm-day and
were filed into 08. AUGUST as records #999–#1002 — the files' own June mtimes
were available evidence the whole time.

Fix contract (FOLDER_NAMING_STANDARDS.md, approved 8/6): a backlog scan's
received date defaults to the AI-read date printed on the document, else the
scan file's own date — NEVER the ingest/confirm day. The month folder follows
it (filing.suggest_filed_dir already keys off date_received). The result stays
marked assumed so the review form's provenance chip and the year-misread guard
still make a person look; note the guard alone would NOT have caught the
incident (June→August is only ~42 days, under the 60-day threshold), which is
exactly why the default has to be right.
"""
import os
import time
from datetime import date, timedelta

import app as klr
import queue_state
from conftest import make_pdf
from test_dedup_triage import _queue_bill


# ── the decision function ─────────────────────────────────────────────────────

def test_backlog_prefers_the_documents_own_date():
    assert klr._backlog_received_date("2026-06-20", date(2026, 6, 24), "2026-08-05") \
        == ("2026-06-20", "document")


def test_backlog_falls_back_to_the_file_date():
    assert klr._backlog_received_date(None, date(2026, 6, 29), "2026-08-05") \
        == ("2026-06-29", "file-date")
    assert klr._backlog_received_date("not a date", date(2026, 6, 29), "2026-08-05") \
        == ("2026-06-29", "file-date")


def test_backlog_distrusts_a_document_date_after_the_scan():
    # A document can't be dated after the file that captured it — misread year.
    assert klr._backlog_received_date("2026-07-15", date(2026, 6, 24), "2026-08-05") \
        == ("2026-06-24", "file-date")


def test_backlog_distrusts_a_document_date_a_year_back():
    # 2025-for-2026 year misread: plausible-looking (before the file date) but
    # would file the record a year back — bounded to ~a year of the file date.
    assert klr._backlog_received_date("2025-06-20", date(2026, 6, 24), "2026-08-05") \
        == ("2026-06-24", "file-date")
    # A genuinely months-old document inside the bound still wins.
    assert klr._backlog_received_date("2026-01-15", date(2026, 6, 24), "2026-08-05") \
        == ("2026-01-15", "document")


def test_fresh_scan_keeps_the_batch_date():
    # Scanning always postdates receipt, so file date >= batch day = fresh.
    assert klr._backlog_received_date("2026-08-01", date(2026, 8, 5), "2026-08-05") is None
    assert klr._backlog_received_date(None, date(2026, 8, 5), "2026-08-05") is None
    assert klr._backlog_received_date(None, date(2026, 8, 6), "2026-08-05") is None
    assert klr._backlog_received_date(None, None, "2026-08-05") is None


# ── the confirm-time fallback for legacy queue rows ───────────────────────────

def _age_file(path, days):
    ts = time.mktime((date.today() - timedelta(days=days)).timetuple())
    os.utime(path, (ts, ts))


def test_fallback_uses_an_old_files_date_never_today(tmp_path):
    p = tmp_path / "june.pdf"
    p.write_bytes(b"x")
    _age_file(str(p), 40)
    assert klr._fallback_received_date(str(p)) == \
        (date.today() - timedelta(days=40)).isoformat()


def test_fallback_is_today_for_fresh_or_missing_files(tmp_path):
    p = tmp_path / "fresh.pdf"
    p.write_bytes(b"x")
    assert klr._fallback_received_date(str(p)) == date.today().isoformat()
    assert klr._fallback_received_date(None) == date.today().isoformat()
    assert klr._fallback_received_date(str(tmp_path / "missing.pdf")) == \
        date.today().isoformat()


# ── ingest end-to-end ─────────────────────────────────────────────────────────

def _ingest(env, monkeypatch, name, mtime_days_ago, **overrides):
    path = os.path.join(env.scan_folder, name)
    # Unique text per file — byte-identical PDFs are set aside by the dedup
    # guard before they ever reach the queue.
    make_pdf(path, f"bill text {name} " + "words " * 30)
    if mtime_days_ago:
        _age_file(path, mtime_days_ago)
    fake = {"is_cover_sheet": False, "received_date": None,
            "stamped_received_date": None, "document_date": None,
            "doc_type": "Bill", "ai_confidence": "Medium",
            "vendor": "ACME Utilities", "amount": 10.0, "due_date": None,
            "entity_code": None, "entity_name": None,
            "property_codes": [], "property_addresses": [],
            "bill_type_code": None, "invoice_number": None,
            "account_number": None, "notes": "", "important": False,
            "important_reason": "",
            "extracted_text": "bill text " + "words " * 30}
    fake.update(overrides)
    monkeypatch.setattr(klr, "classify_document", lambda p: dict(fake))
    monkeypatch.setattr(klr, "split_if_needed", lambda p, k: [p])
    before = {sid for sid, _ in klr._queue_items()}
    klr.enqueue_scan(path, "Physical Mail")
    new = [(sid, d) for sid, d in klr._queue_items() if sid not in before]
    assert new, "scan should have queued"
    return new[-1][1]


def test_stranded_old_scan_never_gets_ingest_day(env, monkeypatch):
    d = _ingest(env, monkeypatch, "stranded.pdf", 40)
    assert d["received_date"] == (date.today() - timedelta(days=40)).isoformat()
    assert d["received_date_assumed"] is True
    assert d["received_date_source"] == "file-date"


def test_stranded_scan_prefers_the_documents_printed_date(env, monkeypatch):
    doc = (date.today() - timedelta(days=45)).isoformat()
    d = _ingest(env, monkeypatch, "dated.pdf", 40, document_date=doc)
    assert d["received_date"] == doc
    assert d["received_date_source"] == "document"
    assert d["received_date_assumed"] is True


def test_fresh_scan_still_defaults_to_today(env, monkeypatch):
    d = _ingest(env, monkeypatch, "fresh.pdf", 0)
    assert d["received_date"] == date.today().isoformat()
    assert d.get("received_date_source") is None
    assert d["received_date_assumed"] is True    # no cover sheet — unchanged


def test_received_stamp_still_wins_over_backlog(env, monkeypatch):
    d = _ingest(env, monkeypatch, "stamped.pdf", 40,
                stamped_received_date="2026-07-20")
    assert d["received_date"] == "2026-07-20"
    assert not d.get("received_date_assumed")


def test_old_file_swept_into_a_cover_sheet_batch_keeps_its_own_date(env, monkeypatch):
    batch_day = (date.today() - timedelta(days=2)).isoformat()
    queue_state.start_batch(batch_day)
    d = _ingest(env, monkeypatch, "swept.pdf", 30)
    assert d["received_date"] == (date.today() - timedelta(days=30)).isoformat()
    # …while a genuinely fresh scan in the same batch adopts the cover sheet's date.
    d2 = _ingest(env, monkeypatch, "fresh2.pdf", 0)
    assert d2["received_date"] == batch_day
    assert not d2.get("received_date_assumed")


# ── the review form ───────────────────────────────────────────────────────────

def test_review_chip_says_backlog_file_date(env, staff_client):
    c, _headers = staff_client
    sid = _queue_bill(env, "b.pdf", "bill text", mail_type="Physical Mail",
                      received_date="2026-06-24", received_date_assumed=True,
                      received_date_source="file-date")
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "backlog scan" in html and "check the stamp" in html


def test_review_chip_says_backlog_document_date(env, staff_client):
    c, _headers = staff_client
    sid = _queue_bill(env, "d.pdf", "bill text", mail_type="Physical Mail",
                      received_date="2026-06-20", received_date_assumed=True,
                      received_date_source="document")
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "date printed on the document" in html


def test_legacy_queue_row_backfills_from_the_file_not_today(env, staff_client):
    # A row queued before ingest stamped received dates: the form used to
    # default the date field to TODAY — the exact incident path.
    c, _headers = staff_client
    sid = _queue_bill(env, "legacy.pdf", "bill text", mail_type="Physical Mail")
    data = queue_state.get_scan(sid)
    assert not data.get("received_date")
    _age_file(data["pdf_path"], 40)
    old = (date.today() - timedelta(days=40)).isoformat()
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert f'value="{old}"' in html
    assert "backlog scan" in html


# ── the year-misread guard keeps working on backlog dates ─────────────────────

def test_year_guard_still_fires_on_months_old_dates():
    w = klr._date_sanity_warnings("2026-04-01", "", today=date(2026, 8, 6))
    assert w and "received date" in w[0]
