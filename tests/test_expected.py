"""Recurring-bills radar: matching states, suggestions mining, routes."""
import sqlite3
from datetime import date

import app as klr
import database


def test_vendor_match_tolerance():
    assert klr._vendor_match("SoCal Gas", "SoCal Gas")
    assert klr._vendor_match("socal gas", "SoCal Gas - Southern California Gas Company")
    assert klr._vendor_match("SoCal Gas - Southern California Gas Company", "socal gas")
    assert not klr._vendor_match("SoCal Gas", "Frontier")
    assert not klr._vendor_match("", "Frontier")


def test_period_start_shapes():
    m = klr._period_start("monthly")
    q = klr._period_start("quarterly")
    y = klr._period_start("yearly")
    assert m.endswith("-01") and q.endswith("-01") and y.endswith("-01-01")
    assert y <= q <= m


def test_expected_states(env):
    database.add_expected_bill("SoCal Gas", account_number="", cadence="monthly")
    database.add_expected_bill("Frontier", cadence="monthly")
    database.add_expected_bill("Water District", cadence="monthly")
    # SoCal Gas arrived this month
    database.insert_entry({"date_received": date.today().isoformat(), "doc_type": "Bill",
                           "vendor": "SoCal Gas - So Cal Division", "status": "Pending"})
    # Frontier is sitting in the review queue
    klr._queue_add({"pdf_path": "x", "vendor": "Frontier Communications"})
    states = {s["vendor"]: s["state"] for s in klr._expected_statuses()}
    assert states == {"SoCal Gas": "arrived", "Frontier": "queue",
                      "Water District": "waiting"}


def test_expected_account_number_narrows_match(env):
    database.add_expected_bill("Edison", account_number="ACCT-1", cadence="monthly")
    database.insert_entry({"date_received": date.today().isoformat(), "doc_type": "Bill",
                           "vendor": "Edison", "account_number": "OTHER-9",
                           "status": "Pending"})
    assert klr._expected_statuses()[0]["state"] == "waiting"
    database.insert_entry({"date_received": date.today().isoformat(), "doc_type": "Bill",
                           "vendor": "Edison", "account_number": "acct-1",
                           "status": "Pending"})
    assert klr._expected_statuses()[0]["state"] == "arrived"


def test_old_bills_do_not_count(env):
    database.add_expected_bill("Edison", cadence="monthly")
    database.insert_entry({"date_received": "2020-01-15", "doc_type": "Bill",
                           "vendor": "Edison", "status": "Paid"})
    assert klr._expected_statuses()[0]["state"] == "waiting"


def test_pause_and_resume(env):
    eb = database.add_expected_bill("Edison")
    database.set_expected_bill_active(eb, False)
    assert klr._expected_statuses() == []
    database.set_expected_bill_active(eb, True)
    assert len(klr._expected_statuses()) == 1


def test_recurring_suggestions_mining(tmp_path):
    ops = tmp_path / "ops.sqlite"
    con = sqlite3.connect(str(ops))
    con.execute("CREATE TABLE docs (payee TEXT, doc_date TEXT, date_ok INT, "
                "amount REAL, entity TEXT)")
    y = date.today().year
    # monthly payee: one doc in each of 30 recent months
    for i in range(30):
        yy, mm = divmod((y * 12 + date.today().month - 1) - i, 12)
        con.execute("INSERT INTO docs VALUES (?,?,1,?,?)",
                    ("SoCal Edison", f"{yy:04d}-{mm+1:02d}-10", 120.0, "TRUST1"))
    # sparse payee: 3 docs
    for m in ("01", "05", "09"):
        con.execute("INSERT INTO docs VALUES (?,?,1,?,?)",
                    ("One-Off Plumber", f"{y-1}-{m}-10", 500.0, "PP"))
    con.commit(); con.close()
    out = klr._recurring_suggestions(db_path=str(ops))
    names = [s["vendor"] for s in out]
    assert "SoCal Edison" in names and "One-Off Plumber" not in names
    s = out[0]
    assert s["months"] >= 24 and s["avg_amount"] == 120.0 and s["entity"] == "TRUST1"


def test_expected_routes(env, staff_client, admin_client):
    c, headers = staff_client
    assert c.get("/expected").status_code == 200
    # staff cannot add
    r = c.post("/expected/add", data={"vendor": "X"}, headers=headers)
    assert r.status_code == 302 and "/expected" not in r.headers["Location"]
    assert database.get_expected_bills() == []
    # admin can
    a, aheaders = admin_client
    r = a.post("/expected/add", data={"vendor": "SoCal Gas", "cadence": "monthly",
                                      "typical_amount": "$1,234.56"}, headers=aheaders)
    assert r.status_code == 302
    rows = database.get_expected_bills()
    assert len(rows) == 1 and rows[0]["typical_amount"] == 1234.56
    # page renders the row
    assert b"SoCal Gas" in a.get("/expected").data
