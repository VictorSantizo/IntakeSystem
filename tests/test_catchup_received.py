"""Catch Up in received order (2026-07-07): Karen pays bills in the order the
mail arrived, so that's the page's default — oldest mail first, with due-date
urgency still marked in red. ?order=due keeps the urgency buckets (the
dashboard's overdue alert links straight there), and Prev/Next + the CSV
export follow whichever order the page showed.
"""
from datetime import date, timedelta

import database
import app as klr

# Far enough out to stay in the "Upcoming" (non-overdue) bucket no matter when
# the suite runs, and far apart enough that the ordering between the two never
# flips relative to today's date (P3 cleanup — these were hardcoded 2027 dates
# that would eventually roll into "overdue" and break the assertions below).
_FAR_LATER = (date.today() + timedelta(days=400)).isoformat()
_FAR_SOONER = (date.today() + timedelta(days=375)).isoformat()


def _bill(vendor, received, due, **overrides):
    e = {"date_received": received, "due_date": due, "doc_type": "Bill",
         "vendor": vendor, "amount": 100.0, "status": "Pending",
         "processed_by": "TestUser"}
    e.update(overrides)
    return database.insert_entry(e)


def _seed_pair():
    # A arrived first but is due later; B arrived later but is due sooner.
    # Received order: A, B.  Due order: B, A.
    a = _bill("Alpha Plumbing", "2026-07-01", _FAR_LATER)
    b = _bill("Bravo Electric", "2026-07-05", _FAR_SOONER)
    return a, b


def test_default_order_is_received(env, staff_client):
    c, _ = staff_client
    _seed_pair()
    html = c.get("/catchup").get_data(as_text=True)
    assert "oldest mail first" in html.lower()
    assert html.index("Alpha Plumbing") < html.index("Bravo Electric")


def test_due_order_still_available(env, staff_client):
    c, _ = staff_client
    _seed_pair()
    html = c.get("/catchup?order=due").get_data(as_text=True)
    assert "Upcoming" in html                       # the urgency buckets render
    assert html.index("Bravo Electric") < html.index("Alpha Plumbing")


def test_received_view_still_marks_overdue(env, staff_client):
    c, _ = staff_client
    _bill("Late Vendor", "2026-06-01", "2026-06-15")   # long overdue
    html = c.get("/catchup").get_data(as_text=True)
    assert "days late" in html
    assert "Late Vendor" in html


def test_missing_received_date_sinks_to_bottom(env, staff_client):
    c, _ = staff_client
    _bill("No Date Vendor", "", "2026-07-08")
    _bill("Dated Vendor", "2026-07-01", _FAR_LATER)
    html = c.get("/catchup").get_data(as_text=True)
    assert html.index("Dated Vendor") < html.index("No Date Vendor")


def test_nav_context_follows_received_order(env, staff_client):
    c, _ = staff_client
    a, b = _seed_pair()
    # Received order is A then B, so B's Prev is A…
    html = c.get(f"/entry/{b}/view?ctx=catchup").get_data(as_text=True)
    assert f'/entry/{a}/view?ctx=catchup"' in html
    # …while in due order (B first), A's Prev is B.
    html = c.get(f"/entry/{a}/view?ctx=catchup&order=due").get_data(as_text=True)
    assert f'/entry/{b}/view?ctx=catchup&amp;order=due' in html


def test_csv_export_matches_page_order(env, staff_client):
    c, _ = staff_client
    _seed_pair()
    body = c.get("/bills/export.csv").get_data(as_text=True)
    assert body.splitlines()[0].startswith("Received,Due Date")
    assert body.index("Alpha Plumbing") < body.index("Bravo Electric")
    body = c.get("/bills/export.csv?order=due").get_data(as_text=True)
    assert body.index("Bravo Electric") < body.index("Alpha Plumbing")


def test_dashboard_overdue_alert_links_to_due_order(env, staff_client):
    c, _ = staff_client
    _bill("Late Vendor", "2026-06-01", "2026-06-15")
    html = c.get("/").get_data(as_text=True)
    assert "/catchup?order=due" in html


def test_all_caught_up_in_received_view(env, staff_client):
    c, _ = staff_client
    html = c.get("/catchup").get_data(as_text=True)
    assert "All caught up" in html


def test_pay_button_present_in_both_orders(env, staff_client):
    c, _ = staff_client
    _seed_pair()
    for url in ("/catchup", "/catchup?order=due"):
        html = c.get(url).get_data(as_text=True)
        assert "Mark selected paid" in html
        assert "/pay" in html                        # per-row ✓ Paid form
