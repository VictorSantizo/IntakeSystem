"""Search and filter compose on the Bills page — report #93 (Joshua).

"unable to search then filter or vice versa. i tried to filter for 'paid' then
search for a vendor. the filter worked fine. type 'forster' into the search and
after clicking search for the vendor i got all results for that vendor
including not paid."

Two causes, both fixed: the search box was its own GET form that submitted only
`q` (so the filters never reached the server), and the route treated a search
as a takeover that skipped the filters entirely.
"""
import bill_filters
from database import get_all_entries, insert_entry


def _bill(vendor, status="Pending", **kw):
    return insert_entry({"vendor": vendor, "doc_type": "Bill",
                         "amount": kw.get("amount", 100.0),
                         "status": status,
                         "mail_type": kw.get("mail_type", "Physical Mail"),
                         "entity_code": kw.get("entity_code", "TRUST1"),
                         "date_received": kw.get("date_received", "2026-08-01")})


# ── the reported bug, end to end ─────────────────────────────────────────────

def test_searching_after_filtering_keeps_the_filter(env, staff_client):
    c, _ = staff_client
    _bill("Forster Plumbing", status="Paid")
    _bill("Forster Plumbing", status="Pending", amount=222.0)

    html = c.get("/bills?q=Forster&status=Paid").get_data(as_text=True)
    assert "Forster Plumbing" in html
    assert "222.00" not in html, "the unpaid Forster bill came back despite status=Paid"
    assert "search everything instead" in html, "the page should say the filter is still on"


def test_the_search_box_carries_the_filters_with_it(env, staff_client):
    """The form itself — the half of the bug that dropped the state client-side."""
    c, _ = staff_client
    _bill("Somebody")
    html = c.get("/bills?view=all&status=Paid&mail_type=Email").get_data(as_text=True)
    form = html.split('role="search"', 1)[1].split("</form>", 1)[0]
    assert '<input type="hidden" name="status" value="Paid">' in form
    assert '<input type="hidden" name="mail_type" value="Email">' in form


def test_an_unfiltered_search_still_searches_everything(env, staff_client):
    c, _ = staff_client
    _bill("Forster Plumbing", status="Paid")
    _bill("Forster Plumbing", status="Pending", amount=222.0)
    html = c.get("/bills?q=Forster").get_data(as_text=True)
    assert "222.00" in html
    assert "search everything instead" not in html


# ── the predicate, against the SQL it mirrors ────────────────────────────────

def test_predicate_agrees_with_get_all_entries(env):
    _bill("A Vendor", status="Paid", mail_type="Email", date_received="2026-07-01")
    _bill("B Vendor", status="Pending", mail_type="Email", date_received="2026-08-01")
    _bill("C Vendor", status="Paid", mail_type="Physical Mail", date_received="2026-08-20")
    everything = get_all_entries({"doc_type": "Bill"})
    for f in ({"status": "Paid"},
              {"mail_type": "Email"},
              {"status": "Paid", "mail_type": "Email"},
              {"date_from": "2026-07-15"},
              {"date_to": "2026-08-05"},
              {"date_from": "2026-07-15", "date_to": "2026-08-05"},
              {"entity_code": "TRUST1"},
              {"status": "Paid,Pending"}):
        by_sql = {r["id"] for r in get_all_entries({"doc_type": "Bill", **f})}
        by_python = {r["id"] for r in bill_filters.apply_filters(everything, f)}
        assert by_sql == by_python, f"{f} means something different in Python"


def test_no_filters_means_the_rows_untouched():
    rows = [{"id": 1, "status": "Paid"}]
    assert bill_filters.apply_filters(rows, {}) is rows
    assert bill_filters.apply_filters(rows, {"status": "", "date_to": ""}) is rows
    assert bill_filters.active({"status": "", "mail_type": "Email"}) == {"mail_type": "Email"}


def test_a_row_with_no_received_date_is_not_swept_into_a_date_range():
    row = {"id": 1, "status": "Paid", "date_received": ""}
    assert not bill_filters.matches(row, {"date_from": "2026-01-01"})
    assert not bill_filters.matches(row, {"date_to": "2026-12-31"})
    assert bill_filters.matches(row, {"status": "Paid"})
