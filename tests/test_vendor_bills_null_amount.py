"""Report #21 (Nancy, 2026-07-22): /bills/vendor/<vendor> threw a 500 whenever
ANY unpaid bill for that vendor had no amount yet — e.g. Harvest-Bot
registrations that hadn't been AI-filled. vendor_bills.html summed the unpaid
list with Jinja's sum(attribute='amount'), and sum() can't add None to int.
The template now skips unknown amounts when totalling.
"""
import database
import app as klr  # noqa: F401  (fixtures import the app module)


def _bill(**over):
    base = {"date_received": "2026-07-10", "entity_code": "TRUST1",
            "property_code": "T27", "doc_type": "Bill", "vendor": "LINGO",
            "amount": None, "status": "Pending"}
    base.update(over)
    return database.insert_entry(base)


def test_vendor_page_renders_with_null_amount_unpaid_bill(env, staff_client):
    """The exact #21 shape: one unpaid bill with amount=None (plus a paid
    sibling) must render, not 500."""
    c, _ = staff_client
    _bill()                                              # amount unknown
    _bill(amount=34.49, status="Paid", paid_date="2026-07-22")
    r = c.get("/bills/vendor/LINGO")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "1 unpaid" in html
    assert "$0.00" in html        # unknown amount counts as $0, not a crash


def test_vendor_page_total_skips_only_unknown_amounts(env, staff_client):
    """Known amounts still add up; only the None is left out of the total."""
    c, _ = staff_client
    _bill()
    _bill(amount=25.50)
    _bill(amount=10.00)
    r = c.get("/bills/vendor/LINGO")
    assert r.status_code == 200
    assert "$35.50" in r.get_data(as_text=True)
