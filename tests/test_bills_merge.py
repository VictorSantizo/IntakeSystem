"""Bills consolidation (2026-07-07): /bills gained entity/property tabs,
/owed/<group> now redirects there instead of rendering its own page.
"""
import database


def _bill(**over):
    base = {"date_received": "2026-07-07", "entity_code": "TRUST1",
            "property_code": "T27", "doc_type": "Bill", "vendor": "Frontier",
            "amount": 250.0, "due_date": "2026-07-20", "status": "Pending"}
    base.update(over)
    return database.insert_entry(base)


def test_bills_default_and_all_views_render(env, staff_client):
    c, headers = staff_client
    _bill()
    for view in (None, "all", "vendor", "due", "entity", "property"):
        url = "/bills" + (f"?view={view}" if view else "")
        r = c.get(url)
        assert r.status_code == 200, url


def test_bills_views_survive_null_amount_and_due_date(env, staff_client):
    """Harvested bills often have a blank amount or due date; no view may 500 on
    them. Regression for the amount-sum and the overdue date filter."""
    c, headers = staff_client
    database.insert_entry({"date_received": "2026-06-08", "doc_type": "Bill",
                           "vendor": "EDISON", "entity_code": "TRUST1",
                           "property_code": "T131", "status": "Pending",
                           "amount": None, "due_date": None})
    _bill(amount=250.0, due_date="2026-06-01")   # a genuinely overdue one
    for view in (None, "all", "due", "vendor", "entity", "property"):
        url = "/bills" + (f"?view={view}" if view else "")
        assert c.get(url).status_code == 200, url
    # All Bills totals the real amount null-safely; the due view flags overdue
    assert "250.00" in c.get("/bills").get_data(as_text=True)
    assert "overdue bill" in c.get("/bills?view=due").get_data(as_text=True)


def test_vendor_summary_ignores_blank_due_date(env):
    # An empty-string due_date (not NULL — harvested/manually-entered bills
    # can have either) used to sort as the smallest possible value in
    # MIN(due_date), making a vendor with no real due date look like the most
    # urgent one. Regression for the get_entity_summary/get_vendor_summary drift.
    _bill(vendor="Blankco", due_date="", amount=10.0)
    _bill(vendor="Blankco", due_date="2026-08-01", amount=20.0)
    row = next(r for r in database.get_vendor_summary() if r["vendor"] == "Blankco")
    assert row["next_due"] == "2026-08-01"


def test_bills_universal_search(env, staff_client):
    """The Bills search box finds any logged record — any doc type, paid or not —
    by keyword across vendor, invoice/account #, property, notes, filename."""
    c, headers = staff_client
    database.insert_entry({"date_received": "2026-06-08", "doc_type": "Bill",
        "vendor": "SOUTHERN CALIF. EDISON", "entity_code": "TRUST1",
        "property_code": "T131", "status": "Pending", "amount": None,
        "account_number": "6573", "notes": "past due notice"})
    database.insert_entry({"date_received": "2026-05-01", "doc_type": "Receipt",
        "vendor": "HOME DEPOT", "entity_code": "TRUST1", "status": "Filed",
        "amount": 44.10})
    # by account number
    out = database.search_documents("6573")
    assert len(out) == 1 and out[0]["vendor"].startswith("SOUTHERN")
    # by property code
    assert any(r["property_code"] == "T131" for r in database.search_documents("T131"))
    # finds a Filed receipt too — not just unpaid bills
    assert database.search_documents("home depot")[0]["doc_type"] == "Receipt"
    # blank query is empty, not a full-table dump
    assert database.search_documents("   ") == []
    # the page renders results and the search view takes over
    r = c.get("/bills?q=6573")
    assert r.status_code == 200 and b"Vendor / Title" in r.data and b"EDISON" in r.data
    # a miss is friendly, not blank
    r = c.get("/bills?q=zzznope")
    assert r.status_code == 200 and b"No records match" in r.data


def test_bills_default_view_is_all_bills(env, staff_client):
    """/bills (and any unknown view) lands on the foundational All Bills table —
    every bill, paid and unpaid, with the filter bar."""
    c, headers = staff_client
    _bill(vendor="Frontier", amount=250.0)                      # unpaid
    paid = _bill(vendor="AT&T Paid", amount=90.0)
    database.update_entry(paid, {"status": "Paid", "paid_date": "2026-07-01"})
    for url in ("/bills", "/bills?view=nonsense"):
        r = c.get(url)
        assert r.status_code == 200, url
        assert b"outstanding" in r.data and b"paid" in r.data, url   # summary line
        assert b"Frontier" in r.data and b"AT&amp;T Paid" in r.data, url  # both shown
    # the status filter narrows to just the paid one
    r = c.get("/bills?status=Paid")
    assert b"AT&amp;T Paid" in r.data and b"Frontier" not in r.data


def test_bills_entity_and_property_show_real_data(env, staff_client):
    c, headers = staff_client
    _bill(entity_code="TRUST1", property_code="T27", amount=300.0)
    r = c.get("/bills?view=entity")
    assert b"TRUST1" in r.data and b"300.00" in r.data
    r = c.get("/bills?view=property")
    assert b"T27" in r.data and b"300.00" in r.data


def test_owed_redirects_to_bills(env, staff_client):
    c, headers = staff_client
    for group in ("vendor", "entity", "property"):
        r = c.get(f"/owed/{group}")
        assert r.status_code == 302
        assert r.headers["Location"] == f"/bills?view={group}"
    r = c.get("/owed/nonsense")
    assert r.status_code == 302 and r.headers["Location"] == "/"


def test_dashboard_no_longer_links_owed(env, staff_client):
    c, headers = staff_client
    r = c.get("/")
    assert b"/owed/" not in r.data
