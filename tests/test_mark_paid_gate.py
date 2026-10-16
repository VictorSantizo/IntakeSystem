"""Report #27 (Joshua, bounced 2026-08-04): only a Bill can be paid.

He bounced it with three live records — 260731-014 (a bank statement),
260722-002 (a receipt) and 260729-008 (a blank lease addendum) — all showing a
green "Mark Paid" button. His ask: "we should have a different type or 'all
done' button for things that are not bills."

So: Bill -> Mark Paid (the payment form, which captures date/amount/method).
Everything else -> Done, which files the record without inventing a payment.
"""
import database
from database import get_entry, insert_entry

# The three shapes Joshua bounced the report with.
NON_BILLS = [
    ("Bank Statement", "Bank Statement - Transaction Detail", None),
    ("Receipt", "So Cal Pest Control", 110.0),
    ("Legal", "Blank Lease Addendum - Pool Rules & Regulations", None),
]


def _record(doc_type, vendor, amount):
    return insert_entry({"vendor": vendor, "doc_type": doc_type, "amount": amount,
                         "date_received": "2026-07-31", "status": "Pending"})


def test_bill_still_offers_mark_paid(env, staff_client):
    c, _ = staff_client
    eid = _record("Bill", "ACME Utilities", 147.48)
    html = c.get(f"/entry/{eid}/view").get_data(as_text=True)
    assert "Mark Paid" in html
    assert 'id="payForm"' in html


def test_non_bills_offer_done_not_mark_paid(env, staff_client):
    c, _ = staff_client
    for doc_type, vendor, amount in NON_BILLS:
        eid = _record(doc_type, vendor, amount)
        html = c.get(f"/entry/{eid}/view").get_data(as_text=True)
        assert "Mark Paid" not in html, f"{doc_type} still offers Mark Paid"
        assert 'id="payForm"' not in html, f"{doc_type} still renders the payment form"
        assert f'action="/entry/{eid}/done"' in html, f"{doc_type} has no Done button"


def test_done_files_a_non_bill_without_a_payment(env, staff_client):
    c, headers = staff_client
    eid = _record("Bank Statement", "Bank Statement - Transaction Detail", None)
    r = c.post(f"/entry/{eid}/done", headers=headers)
    assert r.status_code in (301, 302)
    e = get_entry(eid)
    assert e["status"] == "Filed"
    assert not e["paid_date"] and not e["paid_amount"]
    actions = [a["action"] for a in database.get_activity_feed(limit=50)
               if a["entry_id"] == eid]
    assert "Marked done" in actions


def test_done_refuses_a_bill_and_sends_you_to_the_payment_form(env, staff_client):
    """A bill must never be closed without a paid date/amount/method — that is
    the whole reason a payment can be reconciled later."""
    c, headers = staff_client
    eid = _record("Bill", "ACME Utilities", 147.48)
    r = c.post(f"/entry/{eid}/done", headers=headers)
    assert r.status_code in (301, 302)
    assert "pay=1" in r.headers["Location"]
    assert get_entry(eid)["status"] == "Pending"


def test_a_misfiled_bill_can_still_be_turned_into_one(env, staff_client):
    """The escape hatch: if a non-Bill record really is a bill, Record Actions
    reclassifies it and Mark Paid comes back."""
    c, headers = staff_client
    eid = _record("Receipt", "So Cal Pest Control", 110.0)
    assert "Turn into bill" in c.get(f"/entry/{eid}/view").get_data(as_text=True)
    c.post(f"/entry/{eid}/turn_into_bill", headers=headers)
    assert "Mark Paid" in c.get(f"/entry/{eid}/view").get_data(as_text=True)
