"""Two ways a document ends up wearing a date that isn't its own — Karen's
report #107 and Joshua's #111, both filed 2026-08-07, both about record 1194.

#107: "07-01-23 PEYTON_SCAPES - JUNE - INV# 621 - TL222-16.pdf" was hand-filed
into Files\\2026\\ACCOUNTS PAYABLE - INCOMING BILLS\\07. JULY and the "23" in
its name was taken literally. Record #1194 logged as received 2023-07-01:
three years past due on arrival, first in a pay-by-received-date stack, and
stamped with a permanent record number (230701-001) that says 2023 forever.
The folder is the witness the parser never asked — the year tree AND the month
folder both say July 2026.

#111: queue item 1194 (a different 1194 — the review queue's) was a QuickBooks
payroll-error email flying a red "past due" flag. It was not a bill, and its
"due date" was a sentence in the body ("you should receive this payment by
08/28/2025") almost a year older than the email itself.
"""
import datetime
import json
import os
from datetime import date, timedelta

import app as klr
import cabinet_watch
import database
import doc_types
import nameparse

from conftest import make_pdf
from test_cabinet_watch import _stub_classifier, cab  # noqa: F401  (fixture)
from test_dedup_triage import _queue_bill


# ── the rule: the tree owns the year, the filename owns month and day ─────────

def _priors(year=2026, month=7):
    return {"year": year, "month": month}


def test_karens_case_the_month_folder_convicts_the_year():
    # Report #107 exactly: 07-01-23 sitting in 2026\07. JULY.
    d, note = nameparse.received_from_filing(date(2023, 7, 1), _priors(),
                                             today=date(2026, 8, 7))
    assert d == date(2026, 7, 1)
    assert "2023-07-01" in note and "2026-07-01" in note and "typo" in note


def test_agreeing_year_is_left_alone_and_says_nothing():
    d, note = nameparse.received_from_filing(date(2026, 7, 28), _priors(),
                                             today=date(2026, 8, 7))
    assert d == date(2026, 7, 28)
    assert note is None


def test_different_month_is_reported_not_corrected():
    # A December bill filed into January's folder is ordinary bookkeeping —
    # correcting it would invent a date. Say so instead.
    d, note = nameparse.received_from_filing(date(2025, 12, 28),
                                             _priors(2026, 1),
                                             today=date(2026, 8, 7))
    assert d == date(2025, 12, 28)
    assert note and "left as named" in note


def test_impossible_year_never_becomes_a_date():
    # Record #941: "06-30-256 NOTE TO CINDY WITH STATEMENTS.pdf" was logged as
    # received in the year 256 — its folder is ...\2026\...\6. JUNE.
    d, note = nameparse.received_from_filing(date(256, 6, 30), _priors(2026, 6),
                                             today=date(2026, 8, 7))
    assert d == date(2026, 6, 30) and note
    # And with no month folder to corroborate, a year that isn't a year still
    # cannot stand — the tree year is the only defensible answer.
    d, note = nameparse.received_from_filing(date(256, 6, 30),
                                             _priors(2026, None),
                                             today=date(2026, 8, 7))
    assert d == date(2026, 6, 30)
    assert note and "not a real year" in note


def test_impossible_year_with_no_year_folder_yields_nothing():
    d, note = nameparse.received_from_filing(date(256, 6, 30), {"year": None},
                                             today=date(2026, 8, 7))
    assert d is None
    assert note and "not a real date" in note


def test_no_year_folder_leaves_a_real_date_alone():
    d, note = nameparse.received_from_filing(date(2026, 3, 4), {"year": None},
                                             today=date(2026, 8, 7))
    assert d == date(2026, 3, 4) and note is None


def test_no_date_at_all_is_not_an_error():
    assert nameparse.received_from_filing(None, _priors()) == (None, None)


def test_leap_day_lands_on_a_real_day():
    d, _n = nameparse.received_from_filing(date(2024, 2, 29), _priors(2026, 2),
                                           today=date(2026, 8, 7))
    assert d == date(2026, 2, 28)


# ── the live watcher applies it ───────────────────────────────────────────────

def test_hand_filed_typo_year_registers_in_the_folders_year(cab, monkeypatch):
    # #107's own file, dropped into the tree the way Karen files it.
    _stub_classifier(monkeypatch, vendor="Peyton Scapes", amount=None,
                     due_date="", entity_code="TRUST2")
    p = make_pdf(os.path.join(
        cab.ap, "07-01-23 PEYTON_SCAPES - JUNE - INV# 621 - TL222-16.pdf"))
    cabinet_watch._process(p)
    entries = database.get_all_entries()
    assert len(entries) == 1, "the typo'd year must not hold this for a human"
    e = entries[0]
    assert e["date_received"] == "2026-07-01"
    # The correction is on the record, not hidden: a person can see what moved.
    assert "07-01-23" in (e["notes"] or "") or "2023-07-01" in (e["notes"] or "")


def test_a_genuinely_stale_filing_is_still_held(cab, monkeypatch):
    # Same wrong year, but filed into a month folder that does NOT corroborate
    # it — nothing to correct from, so it goes to a human as it always did.
    _stub_classifier(monkeypatch, vendor="Peyton Scapes", amount=None,
                     due_date="")
    p = make_pdf(os.path.join(cab.ap, "02-01-23 PEYTON_SCAPES - INV# 621.pdf"))
    cabinet_watch._process(p)
    assert database.get_all_entries() == []
    assert len(klr._queue_items()) == 1


# ── #111: what can be past due ────────────────────────────────────────────────

def _iso(days):
    return (date.today() + timedelta(days=days)).isoformat()


def test_only_a_bill_can_be_past_due():
    # Queue item 1194: Correspondence, with a due date lifted from body text.
    assert not klr._scan_is_overdue({"doc_type": "Correspondence",
                                     "due_date": _iso(-3),
                                     "received_date": _iso(0)})
    assert not klr._scan_is_overdue({"doc_type": "Statement",
                                     "due_date": _iso(-3),
                                     "received_date": _iso(0)})
    assert klr._scan_is_overdue({"doc_type": "Bill", "due_date": _iso(-3),
                                 "received_date": _iso(0)})


def test_a_due_date_older_than_the_document_is_a_misread():
    # #111's real numbers: an email that arrived 2026-08-03 carrying "due"
    # 2025-08-28. Even called a Bill, that is not a bill 340 days late.
    assert not klr._scan_is_overdue({"doc_type": "Bill",
                                     "due_date": "2025-08-28",
                                     "received_date": "2026-08-03"})


def test_a_bill_that_really_did_arrive_late_still_flags():
    assert klr._scan_is_overdue({"doc_type": "Bill", "due_date": _iso(-20),
                                 "received_date": _iso(-2)})
    # ...and one with no received date to check against is taken at its word.
    assert klr._scan_is_overdue({"doc_type": "Bill", "due_date": _iso(-400)})


def test_blank_and_unparseable_due_dates_are_not_urgent():
    assert not klr._scan_is_overdue({"doc_type": "Bill", "due_date": ""})
    assert not klr._scan_is_overdue({"doc_type": "Bill", "due_date": "Aug 28"})


def test_payable_is_one_answer_shared_with_the_queue_split(env, staff_client):
    # The bills section and the past-due flag must never disagree about what a
    # bill is — both go through doc_types.is_payable.
    assert doc_types.is_payable("Bill") and doc_types.is_payable("bill")
    assert not doc_types.is_payable("Receipt")
    assert not doc_types.is_payable("")
    c, _h = staff_client
    _queue_bill(env, "note.pdf", "payroll error text", doc_type="Correspondence",
                vendor="QuickBooks Payroll", due_date=_iso(-300),
                received_date=_iso(0))
    html = c.get("/review").get_data(as_text=True)
    assert "0 bills" in html and "1 housekeeping" in html
    assert "past due" not in html
