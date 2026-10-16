"""Year-sanity guard on Confirm & Log and manual entry (record #815, 2026-07-24):
a degraded scan's misread year ("JUN 1 2025" on a fresh 2026 document) sailed
through review and became permanent record_no 250629-001 plus a 2025 filing
suggestion. Out-of-range dates now need an explicit date_ok before the UI logs
them. Harvest/backfill inserts (database.insert_entry, no UI) stay unguarded
for any date a human could actually mean — that test pins the exemption — but
since record #941 (2026-08-06, a "06-30-256" filename typo stored as the year
0256) an IMPOSSIBLE year (outside 20xx) is swapped for a sane date and flagged,
never stored raw (KAREN_FILENAME_SPEC §3.1). Bulk confirm's blanket exemption
ended 2026-07-31 (Joshua's call, skip-not-block): clean-dated items still
complete the batch, but a flagged item is left in the queue for individual
review, where the single-confirm form's warning + date_ok checkbox apply.
"""
import os
from datetime import date, timedelta

import database
import app as klr

from conftest import make_pdf

TODAY = date.today()


def _iso(days_from_today):
    return (TODAY + timedelta(days=days_from_today)).isoformat()


def _queue_scan(env, name="doc.pdf", **data):
    pdf = os.path.join(env.scan_folder, name)
    make_pdf(pdf)
    data.setdefault("pdf_path", pdf)
    return klr._queue_add(data)


# ── The rule itself ────────────────────────────────────────────────────────────

def test_incident_case_warns_year_old_received():
    # Record #815 exactly: fresh scan on 2026-07-24, AI read 2025-06-29.
    w = klr._date_sanity_warnings("2025-06-29", "", today=date(2026, 7, 24))
    assert len(w) == 1
    assert "June 2025" in w[0]
    assert "year-old" in w[0]
    assert "misread" in w[0]


def test_received_date_bounds():
    ok = klr._date_sanity_warnings
    assert ok(_iso(0), "") == []                     # today
    assert ok(_iso(-59), "") == []                   # recent backlog is normal
    assert ok(_iso(-61), "")                         # months old → warn
    assert ok(_iso(1), "")                           # ANY future → warn
    assert "future" in ok(_iso(1), "")[0]


def test_due_date_bounds():
    ok = klr._date_sanity_warnings
    assert ok(_iso(0), _iso(30)) == []               # normal bill, due next month
    assert ok(_iso(0), _iso(170)) == []              # semi-annual bill, still fine
    assert ok(_iso(0), _iso(200))                    # half a year out → warn
    assert ok(_iso(0), _iso(-365))                   # year-old due date → warn
    assert "year-old" in ok(_iso(0), _iso(-365))[0]


def test_blank_and_garbage_dates_never_warn():
    ok = klr._date_sanity_warnings
    assert ok("", "") == []
    assert ok(None, None) == []
    assert ok("None", "not-a-date") == []            # the 'None' round-trip strings
    assert ok("06/15/2026", "") == []                # non-ISO regex fallback


# ── Review confirm path ────────────────────────────────────────────────────────

def test_confirm_blocks_year_old_date_without_date_ok(env, staff_client):
    c, headers = staff_client
    sid = _queue_scan(env)
    r = c.post(f"/review/{sid}/confirm", headers=headers, data={
        "date_received": _iso(-365), "doc_type": "Bill", "vendor": "",
    })
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "usually a misread" in body
    assert 'name="date_ok"' in body
    assert database.get_all_entries() == []          # nothing logged
    assert len(klr._queue_items()) == 1              # scan still in the queue


def test_confirm_proceeds_with_date_ok(env, staff_client):
    c, headers = staff_client
    sid = _queue_scan(env)
    r = c.post(f"/review/{sid}/confirm", headers=headers, data={
        "date_received": _iso(-365), "doc_type": "Bill", "vendor": "",
        "date_ok": "1",
    })
    assert r.status_code == 302
    e = database.get_all_entries()[0]
    assert e["date_received"] == _iso(-365)          # kept as entered
    assert klr._queue_items() == []


def test_confirm_normal_dates_need_no_checkbox(env, staff_client):
    c, headers = staff_client
    sid = _queue_scan(env)
    r = c.post(f"/review/{sid}/confirm", headers=headers, data={
        "date_received": _iso(0), "doc_type": "Bill", "vendor": "",
        "due_date": _iso(30),                        # ordinary future due date
    })
    assert r.status_code == 302
    assert len(database.get_all_entries()) == 1


def test_confirm_blocks_future_received_and_far_due(env, staff_client):
    c, headers = staff_client
    sid = _queue_scan(env)
    r = c.post(f"/review/{sid}/confirm", headers=headers, data={
        "date_received": _iso(2), "doc_type": "Bill", "vendor": "",
        "due_date": _iso(-365),
    })
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "future" in body                          # received-date warning
    assert "Due date reads as" in body               # due-date warning too
    assert database.get_all_entries() == []


def test_interstitial_preserves_form_and_fixing_date_clears_it(env, staff_client):
    c, headers = staff_client
    sid = _queue_scan(env)
    data = {"date_received": _iso(-400), "doc_type": "Bill",
            "vendor": "Swan Cabinets", "amount": "1230.00"}
    r = c.post(f"/review/{sid}/confirm", headers=headers, data=data)
    body = r.get_data(as_text=True)
    assert 'value="Swan Cabinets"' in body           # hidden field carried
    assert 'value="1230.00"' in body
    # Fixing the date (no checkbox) sails through on resubmit.
    r = c.post(f"/review/{sid}/confirm", headers=headers,
               data=dict(data, date_received=_iso(0)))
    assert r.status_code == 302
    e = database.get_all_entries()[0]
    assert e["vendor"] == "Swan Cabinets"
    assert e["date_received"] == _iso(0)


# ── Manual entry path ──────────────────────────────────────────────────────────

def test_manual_entry_blocks_then_keeps(env, staff_client):
    c, headers = staff_client
    data = {"date_received": _iso(-90), "doc_type": "Bill", "vendor": "ACME"}
    r = c.post("/entry/new", headers=headers, data=data)
    assert r.status_code == 200
    assert "usually a misread" in r.get_data(as_text=True)
    assert database.get_all_entries() == []
    r = c.post("/entry/new", headers=headers, data=dict(data, date_ok="1"))
    assert r.status_code == 302
    assert database.get_all_entries()[0]["date_received"] == _iso(-90)


def test_manual_entry_fresh_date_passes(env, staff_client):
    c, headers = staff_client
    r = c.post("/entry/new", headers=headers, data={
        "date_received": _iso(0), "doc_type": "Bill", "vendor": "ACME",
        "due_date": _iso(21),
    })
    assert r.status_code == 302
    assert len(database.get_all_entries()) == 1


# ── Bulk confirm: skip-not-block (2026-07-31) ─────────────────────────────────

def test_bulk_confirm_mixed_batch_skips_only_the_flagged_date(env, staff_client):
    """The approved design: a far-off date must neither block the whole batch
    nor silently slide through. Two clean items complete; the year-old one is
    excluded and stays in the queue for individual review."""
    c, headers = staff_client
    clean1 = _queue_scan(env, name="a.pdf", received_date=_iso(0),
                         doc_type="Bill", vendor="Alpha Plumbing")
    flagged = _queue_scan(env, name="b.pdf", received_date=_iso(-365),
                          doc_type="Bill", vendor="Bravo Electric")
    clean2 = _queue_scan(env, name="c.pdf", received_date=_iso(-3),
                         doc_type="Bill", vendor="Charlie Water")
    r = c.post("/review/bulk_confirm", headers=headers,
               data={"scan_ids": [str(clean1), str(flagged), str(clean2)]})
    assert r.status_code == 302
    assert "suspicious%20date" in r.headers["Location"] \
        or "suspicious+date" in r.headers["Location"]

    logged = {e["vendor"]: e for e in database.get_all_entries()}
    assert set(logged) == {"Alpha Plumbing", "Charlie Water"}    # clean ones landed
    # The flagged one is untouched and still waiting in the queue.
    remaining = dict(klr._queue_items())
    assert flagged in remaining
    assert clean1 not in remaining and clean2 not in remaining
    assert remaining[flagged]["received_date"] == _iso(-365)


def test_bulk_confirm_clean_dates_all_complete(env, staff_client):
    # No flagged dates -> behaves exactly as before the change.
    c, headers = staff_client
    # Distinct vendor names — near-identical ones would trip the separate
    # vendor-spelling-drift skip and cloud what this test measures.
    vendors = ["Alpha Plumbing", "Goleta Water", "Southern California Edison"]
    sids = [_queue_scan(env, name=f"{i}.pdf", received_date=_iso(-i),
                        doc_type="Bill", vendor=v) for i, v in enumerate(vendors)]
    r = c.post("/review/bulk_confirm", headers=headers,
               data={"scan_ids": [str(s) for s in sids]})
    assert r.status_code == 302
    assert len(database.get_all_entries()) == 3
    assert dict(klr._queue_items()) == {}


# ── Harvest path stays unguarded for dates a human could mean ─────────────────

def test_direct_insert_not_blocked(env):
    # Harvest-Bot writes through database.insert_entry — no UI, no guard.
    eid = database.insert_entry({"date_received": "2025-06-29",
                                 "doc_type": "Bill", "vendor": "Swan Cabinets"})
    assert database.get_entry(eid)["date_received"] == "2025-06-29"


# ── ...but an IMPOSSIBLE year never enters silently (record #941) ─────────────

def test_direct_insert_swaps_impossible_year_and_flags(env):
    # Record #941 exactly: "06-30-256" parsed to 0256-06-30 and sailed into the
    # log, minting record_no 560630-001. The net lands on the scan date, keeps
    # the raw value in the flag, and mints the number from a real day.
    eid = database.insert_entry({"date_received": "0256-06-30",
                                 "scanned_date": "2026-07-30",
                                 "doc_type": "Bank Statement", "vendor": "X"})
    e = database.get_entry(eid)
    assert e["date_received"] == "2026-07-30"
    assert e["important"] == 1
    assert "0256-06-30" in e["important_reason"]
    assert e["record_no"].startswith("260730-")
    acts = [a for a in database.get_activity_feed(20) if a["entry_id"] == eid]
    assert any(a["action"] == "Date guard" and "0256-06-30" in a["detail"]
               for a in acts)


def test_direct_insert_impossible_year_without_scan_date_lands_today(env):
    eid = database.insert_entry({"date_received": "6543-01-02", "doc_type": "Bill"})
    e = database.get_entry(eid)
    assert e["date_received"] == TODAY.isoformat()
    assert "6543-01-02" in e["important_reason"]


def test_direct_insert_impossible_due_year_cleared(env):
    eid = database.insert_entry({"date_received": _iso(0), "due_date": "0203-08-15",
                                 "doc_type": "Bill", "vendor": "ACME"})
    e = database.get_entry(eid)
    assert e["due_date"] == ""
    assert e["important"] == 1
    assert "0203-08-15" in e["important_reason"]
    assert e["date_received"] == _iso(0)                 # untouched


def test_direct_insert_flag_appends_to_ai_reason(env):
    # An AI-set important_reason must survive the net's addition, not be lost.
    eid = database.insert_entry({"date_received": "0256-06-30",
                                 "doc_type": "Bill", "important": 1,
                                 "important_reason": "Shutoff notice enclosed"})
    reason = database.get_entry(eid)["important_reason"]
    assert "Shutoff notice enclosed" in reason and "0256-06-30" in reason


def test_startup_repair_fixes_record_941_once(env):
    # The one-off in init_db: pins id AND the bad value, keeps the record_no,
    # logs the correction, and does nothing on later startups.
    with database.get_conn() as conn:
        conn.execute("INSERT INTO intake_log (id, date_received, doc_type, vendor, "
                     "record_no) VALUES (941, '0256-06-30', 'Bank Statement', "
                     "'Cash Flow Statement', '560630-001')")
        conn.commit()
    database.init_db()                                   # what server startup runs
    e = database.get_entry(941)
    assert e["date_received"] == "2026-06-30"
    assert e["record_no"] == "560630-001"                # never renumbered
    edits = [a for a in database.get_activity_feed(20)
             if a["entry_id"] == 941 and a["action"] == "Edited"]
    assert len(edits) == 1 and "0256-06-30" in edits[0]["detail"]
    database.init_db()                                   # idempotent
    assert database.get_entry(941)["date_received"] == "2026-06-30"
    edits = [a for a in database.get_activity_feed(20)
             if a["entry_id"] == 941 and a["action"] == "Edited"]
    assert len(edits) == 1


# ── The forms carry the guard ──────────────────────────────────────────────────

def test_review_form_renders_guard(env, staff_client):
    c, headers = staff_client
    sid = _queue_scan(env)
    body = c.get(f"/review/{sid}").get_data(as_text=True)
    assert 'id="date-guard"' in body
    assert 'name="date_ok"' in body


def test_entry_forms_render_guard(env, staff_client):
    c, headers = staff_client
    assert 'id="date-guard"' in c.get("/entry/new").get_data(as_text=True)
    eid = database.insert_entry({"date_received": "2026-01-05",
                                 "doc_type": "Bill", "vendor": "ACME"})
    body = c.get(f"/entry/{eid}/edit").get_data(as_text=True)
    assert 'id="date-guard"' in body
    # Edit mode questions only CHANGED dates — a January record being edited in
    # July must not demand the checkbox just for existing.
    assert "var checkOnLoad = false" in body
