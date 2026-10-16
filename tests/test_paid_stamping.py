"""Edit-form status flips to Paid must stamp paid metadata. Regression for the
2026-07 deep-dive finding: records #33/#34 were flipped to Paid via the edit
form (whose payment block only renders for already-Paid records), leaving
paid_date/paid_by/paid_amount NULL — invisible to every paid-date report."""

import database


def _make_pending_bill(**over):
    data = dict(date_received="2026-07-01", mail_type="Physical Mail",
                entity_code="KLR", doc_type="Bill", vendor="Acme Water",
                amount=123.45, status="Pending")
    data.update(over)
    return database.insert_entry(data)


def test_edit_flip_to_paid_stamps_metadata(staff_client):
    c, headers = staff_client
    eid = _make_pending_bill()
    # The Pending form has no payment block, so no paid_* fields in the POST.
    r = c.post(f"/entry/{eid}/edit", headers=headers, data={
        "date_received": "2026-07-01", "mail_type": "Physical Mail",
        "entity_code": "KLR", "doc_type": "Bill", "vendor": "Acme Water",
        "amount": "123.45", "status": "Paid", "notes": "",
    })
    assert r.status_code == 302
    e = database.get_entry(eid)
    assert e["status"] == "Paid"
    assert e["paid_date"], "paid_date must be stamped on the flip"
    assert e["paid_by"] == "TestUser"
    assert e["paid_amount"] == 123.45


def test_edit_with_payment_block_still_wins(staff_client):
    c, headers = staff_client
    eid = _make_pending_bill()
    # A form that DOES carry the payment block keeps its explicit values.
    r = c.post(f"/entry/{eid}/edit", headers=headers, data={
        "date_received": "2026-07-01", "mail_type": "Physical Mail",
        "entity_code": "KLR", "doc_type": "Bill", "vendor": "Acme Water",
        "amount": "123.45", "status": "Paid", "notes": "",
        "paid_date": "2026-07-10", "paid_amount": "120.00",
        "payment_method": "Check", "check_number": "9999",
    })
    assert r.status_code == 302
    e = database.get_entry(eid)
    assert e["paid_date"] == "2026-07-10"
    assert e["paid_amount"] == 120.00
    assert e["check_number"] == "9999"


def test_non_paid_edit_does_not_stamp(staff_client):
    c, headers = staff_client
    eid = _make_pending_bill()
    c.post(f"/entry/{eid}/edit", headers=headers, data={
        "date_received": "2026-07-01", "mail_type": "Physical Mail",
        "entity_code": "KLR", "doc_type": "Bill", "vendor": "Acme Water",
        "amount": "123.45", "status": "Pending", "notes": "updated",
    })
    e = database.get_entry(eid)
    assert e["status"] == "Pending"
    assert not e["paid_date"]
