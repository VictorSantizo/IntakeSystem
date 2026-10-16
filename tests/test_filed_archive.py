"""The Filed status as the archive layer (2026-08-05 cash-flow flood).

Three behaviors:
  * /log hides Filed by default — the archive is search/Files-view material,
    not the daily chronology; "Filed" in the dropdown shows just them and
    "all" shows everything.
  * A bare database.get_all_entries() still returns Filed rows — the cabinet
    watcher's twin-matching depends on seeing them.
  * The archive-filed-convert console one-shot: that day's mis-registered
    Pending records flip to Filed with period dates (record numbers KEPT —
    the number is a label), matching review holds register-and-clear, and
    everything else is left alone.
"""
import datetime
import json
import os

import app as klr
import database
import nameparse
from conftest import make_pdf

TODAY = datetime.date.today().isoformat()


def _entry(**over):
    base = {"date_received": "2026-08-05", "entity_code": "PP",
            "property_code": "PP16", "doc_type": "Bank Statement",
            "vendor": "Cash Flow Statement - 105 Real Road (PP16)",
            "amount": None, "status": "Pending"}
    base.update(over)
    return database.insert_entry(base)


# ── /log default hides Filed ──────────────────────────────────────────────────

def test_log_hides_filed_by_default_shows_on_request(env, staff_client):
    c, _h = staff_client
    _entry(vendor="A Pending Thing")
    _entry(vendor="An Archived Thing", status="Filed")
    html = c.get("/log").get_data(as_text=True)
    assert "A Pending Thing" in html
    assert "An Archived Thing" not in html
    html = c.get("/log?status=Filed").get_data(as_text=True)
    assert "An Archived Thing" in html
    assert "A Pending Thing" not in html
    html = c.get("/log?status=all").get_data(as_text=True)
    assert "A Pending Thing" in html and "An Archived Thing" in html


def test_bare_get_all_entries_keeps_filed_rows(env):
    _entry(status="Filed")
    assert len(database.get_all_entries()) == 1          # watcher contract
    assert database.get_all_entries({"status_not": "Filed"}) == []
    # An explicit status filter beats the exclusion (never both).
    rows = database.get_all_entries({"status": "Filed", "status_not": "Filed"})
    assert len(rows) == 1


# ── month/year filename hints ─────────────────────────────────────────────────

def test_month_year_hints():
    assert nameparse.month_year("PP16 CASH FLOW - JAN 2024") == (2024, 1)
    assert nameparse.month_year("SEPT 2023 OWNER STATEMENT") == (2023, 9)
    assert nameparse.month_year("PP44 CASH FLOW OCTOBER") == (None, 10)
    assert nameparse.month_year("NO HINTS HERE") == (None, None)
    # A month word inside a longer word must not read as a month.
    assert nameparse.month_year("MARCHETTI PLUMBING INVOICE")[1] is None


def test_single_digit_month_folder_parses():
    p = r"F:\Files\2024\PP\CASH FLOW STATEMENTS\PP16\1. JANUARY\x.pdf"
    pri = nameparse.path_priors(p, {})
    assert pri["year"] == 2024 and pri["month"] == 1


# ── the conversion one-shot ───────────────────────────────────────────────────

def _flood_setup(env):
    """A mini 2026-08-05 flood: two mis-registered archive records, one
    legitimate current-year pickup, one archive review hold."""
    tree = os.path.join(str(env.tmp), "Files", "2024", "PP",
                        "CASH FLOW STATEMENTS", "PP16 - 105 REAL ROAD",
                        "1. JANUARY")
    os.makedirs(tree, exist_ok=True)
    p1 = make_pdf(os.path.join(tree, "PP16 - 105 REAL ROAD CASH FLOW - JAN 2024.pdf"))
    p2 = make_pdf(os.path.join(tree, "PP16 - 105 REAL ROAD CASH FLOW - FEB 2024.pdf"))
    r1 = _entry(processed_by="Cabinet-Watch", pdf_path=p1, filed_path=p1)
    r2 = _entry(processed_by="Cabinet-Watch", pdf_path=p2, filed_path=p2)
    keep = _entry(processed_by="Cabinet-Watch", vendor="Montecito Bank & Trust",
                  date_received="2026-07-31",
                  pdf_path=os.path.join(str(env.tmp), "Files", "2026",
                                        "TRUST BANK STATEMENTS", "x.pdf"))
    p3 = make_pdf(os.path.join(tree, "PP16 - 105 REAL ROAD CASH FLOW - MAR 2024.pdf"))
    sid = klr._queue_add({"hand_filed": True, "mail_type": "Hand-filed",
                          "pdf_path": p3, "file_hash": klr._sha256_file(p3),
                          "doc_type": "Bank Statement",
                          "notes": "[Hand-filed, needs a decision] wild date"})
    prefix = os.path.join(str(env.tmp), "Files", "2024")
    return p1, p2, r1, r2, keep, sid, prefix


def test_convert_dry_run_changes_nothing(env, capsys):
    _p1, _p2, r1, _r2, keep, sid, prefix = _flood_setup(env)
    klr._archive_filed_convert_cli([TODAY, prefix])
    out = capsys.readouterr().out
    assert "Would convert 2 record(s)" in out
    assert database.get_entry(r1)["status"] == "Pending"       # untouched
    assert klr.pending_scans.get(sid) is not None
    assert len(database.get_all_entries()) == 3


def test_convert_apply_files_the_flood_and_keeps_the_real_pickups(env, capsys):
    p1, _p2, r1, r2, keep, sid, prefix = _flood_setup(env)
    old_no = database.get_entry(r1)["record_no"]
    klr._archive_filed_convert_cli([TODAY, prefix, "apply"])
    out = capsys.readouterr().out
    assert "Converted 2 record(s)" in out and "RESTART" in out

    e1 = database.get_entry(r1)
    assert e1["status"] == "Filed"
    assert e1["date_received"] == "2024-01-31"
    assert e1["doc_type"] == "Statement"
    assert e1["record_no"] == old_no                 # the number is a label
    assert "[CONVERTED]" in e1["notes"]
    assert database.get_entry(r2)["status"] == "Filed"

    k = database.get_entry(keep)                     # the 7/31 bank statement
    assert k["status"] == "Pending" and k["date_received"] == "2026-07-31"

    # The hold became a Filed record and left the queue (memory AND db).
    assert klr.pending_scans.get(sid) is None
    assert (sid, ) not in [(s, ) for s, _ in database.get_all_pending_scans()]
    filed = [e for e in database.get_all_entries()
             if e["status"] == "Filed" and (e.get("pdf_path") or "").endswith("MAR 2024.pdf")]
    assert len(filed) == 1 and filed[0]["date_received"] == "2024-03-31"


def test_convert_leaves_missing_file_holds_for_a_person(env, capsys):
    tree = os.path.join(str(env.tmp), "Files", "2024", "PP")
    os.makedirs(tree, exist_ok=True)
    ghost = os.path.join(tree, "RENAMED SINCE THE HOLD.pdf")
    sid = klr._queue_add({"hand_filed": True, "pdf_path": ghost,
                          "file_hash": "", "notes": "hold"})
    klr._archive_filed_convert_cli(
        [TODAY, os.path.join(str(env.tmp), "Files", "2024"), "apply"])
    out = capsys.readouterr().out
    assert "LEFT IN QUEUE" in out
    assert klr.pending_scans.get(sid) is not None    # a person decides
