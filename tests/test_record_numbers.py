"""Record numbers (YYMMDD-NNN): generation, backfill, the /goto jump box,
and the helper's dual-form lookup."""
import sqlite3
from datetime import datetime

import pytest

import database
import assistant


def _insert(**kw):
    base = {"date_received": "2026-07-17", "doc_type": "Bill", "vendor": "ACME",
            "amount": 10.0, "status": "Pending"}
    base.update(kw)
    return database.insert_entry(base)


def test_insert_assigns_sequential_numbers_per_received_day(env):
    a = _insert()
    b = _insert()
    c = _insert(date_received="2026-07-16")
    assert database.get_entry(a)["record_no"] == "260717-001"
    assert database.get_entry(b)["record_no"] == "260717-002"
    assert database.get_entry(c)["record_no"] == "260716-001"


def test_supplied_number_is_honored_and_collision_raises(env):
    a = _insert(record_no="991231-042")
    assert database.get_entry(a)["record_no"] == "991231-042"
    with pytest.raises(sqlite3.IntegrityError):
        _insert(record_no="991231-042")


def test_unparseable_received_date_falls_back_to_today(env):
    rid = _insert(date_received="not-a-date")
    rec = database.get_entry(rid)["record_no"]
    assert rec.startswith(datetime.now().strftime("%y%m%d") + "-")


def test_backfill_numbers_legacy_rows_in_received_order(env):
    ids = [_insert(), _insert(date_received="2026-07-15"), _insert()]
    # Simulate rows from before the feature existed (no numbers, no counters).
    with database.get_conn() as conn:
        conn.execute("UPDATE intake_log SET record_no=NULL")
        conn.execute("DELETE FROM record_no_counters")
        conn.commit()
    database.init_db()
    assert database.get_entry(ids[1])["record_no"] == "260715-001"
    assert database.get_entry(ids[0])["record_no"] == "260717-001"
    assert database.get_entry(ids[2])["record_no"] == "260717-002"
    # A second migration run must not renumber anything.
    database.init_db()
    assert database.get_entry(ids[0])["record_no"] == "260717-001"


def test_backfill_continues_after_existing_numbers(env):
    keep = _insert()                                   # 260717-001, keeps it
    legacy = _insert()                                 # gets blanked below
    with database.get_conn() as conn:
        conn.execute("UPDATE intake_log SET record_no=NULL WHERE id=?", (legacy,))
        conn.execute("DELETE FROM record_no_counters")
        conn.commit()
    database.init_db()
    assert database.get_entry(keep)["record_no"] == "260717-001"
    assert database.get_entry(legacy)["record_no"] == "260717-002"


def test_deleted_high_number_is_never_reused(env):
    _insert()                                          # 260717-001
    b = _insert()                                      # 260717-002
    database.delete_entry(b)
    c = _insert()
    assert database.get_entry(c)["record_no"] == "260717-003"


def test_goto_resolves_record_number_variants(staff_client):
    c, _ = staff_client
    rid = _insert()
    rec = database.get_entry(rid)["record_no"]        # 260717-001
    for q in (rec, rec.replace("-", ""), "#" + rec, "260717-1"):
        r = c.get("/goto", query_string={"q": q})
        assert r.status_code == 302, q
        assert r.headers["Location"].endswith(f"/entry/{rid}/view"), q


def test_goto_legacy_id_and_search_fallback(staff_client):
    c, _ = staff_client
    rid = _insert()
    r = c.get("/goto", query_string={"q": str(rid)})
    assert r.status_code == 302
    assert r.headers["Location"].endswith(f"/entry/{rid}/view")
    r = c.get("/goto", query_string={"q": "frontier"})
    assert r.status_code == 302
    assert "/bills" in r.headers["Location"]
    assert "q=frontier" in r.headers["Location"]


def test_helper_get_record_accepts_both_forms(env):
    rid = _insert()
    rec = database.get_entry(rid)["record_no"]
    by_no = assistant._tool_get_record({"record_id": rec})
    assert by_no["id"] == rid
    assert by_no["record_no"] == rec
    by_id = assistant._tool_get_record({"record_id": rid})
    assert by_id["record_no"] == rec
    assert "error" in assistant._tool_get_record({"record_id": "999999-999"})
