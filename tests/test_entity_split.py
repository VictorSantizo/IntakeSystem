"""Report #54: a bill can be split across entities from the edit form.

Nancy's words: "805 IT Guys (Edit Entry #719), these bills should be applied 50/50 to
KLR & LRC & there is no radio button of which to select with a combined KLR/LRC."

The split machinery already existed and already computed 50/50 correctly for that bill --
it was only reachable from the pay panel, so a bill that arrived already paid (as #719 did,
via harvest) never showed it. payment_splits had zero rows in production, ever. These pin
the split being settable and visible from the record itself.

Entity stays single-valued (it's whoever pays); the division lives in these rows.
Joshua's call, 2026-08-04.
"""
import database
import property_model


def _shared_bill(**kw):
    base = {"date_received": "2026-08-01", "doc_type": "Bill",
            "vendor": "805 IT Guys", "amount": 425.07,
            "property_code": "T27, PP16"}
    base.update(kw)
    return database.insert_entry(base)


def _edit(c, hdr, entry_id, **extra):
    data = {"date_received": "2026-08-01", "doc_type": "Bill", "status": "Pending",
            "vendor": "805 IT Guys", "amount": "425.07",
            "property_codes": ["T27", "PP16"], "date_ok": "1", "split_block": "1"}
    data.update(extra)
    return c.post(f"/entry/{entry_id}/edit", data=data, headers=hdr)


def test_split_saves_from_the_edit_form(admin_client):
    c, hdr = admin_client
    entry_id = _shared_bill()

    _edit(c, hdr, entry_id,
          split_entity=["TRUST1", "PP"], split_amount=["212.54", "212.53"])

    splits = database.get_payment_splits(entry_id)
    assert [(s["entity_code"], s["amount"]) for s in splits] == \
           [("TRUST1", 212.54), ("PP", 212.53)]


def test_split_survives_an_unrelated_edit(admin_client):
    """The block renders on every edit, so a later save must round-trip it, not drop it."""
    c, hdr = admin_client
    entry_id = _shared_bill()
    _edit(c, hdr, entry_id,
          split_entity=["TRUST1", "PP"], split_amount=["212.54", "212.53"])

    html = c.get(f"/entry/{entry_id}/edit").get_data(as_text=True)
    assert "212.54" in html and "212.53" in html


def test_a_form_without_the_block_does_not_wipe_the_split(admin_client):
    """Same guard the payment fields have: no marker submitted means the block never
    rendered, so its absence is not an instruction to clear the split."""
    c, hdr = admin_client
    entry_id = _shared_bill()
    database.save_payment_splits(entry_id, [("TRUST1", 212.54), ("PP", 212.53)])

    c.post(f"/entry/{entry_id}/edit", data={
        "date_received": "2026-08-01", "doc_type": "Bill", "status": "Pending",
        "vendor": "805 IT Guys", "amount": "425.07",
        "property_codes": ["T27", "PP16"], "date_ok": "1",   # no split_block
    }, headers=hdr)

    assert len(database.get_payment_splits(entry_id)) == 2


def test_clearing_the_rows_clears_the_split(admin_client):
    c, hdr = admin_client
    entry_id = _shared_bill()
    database.save_payment_splits(entry_id, [("TRUST1", 212.54), ("PP", 212.53)])

    _edit(c, hdr, entry_id, split_entity=[], split_amount=[])

    assert database.get_payment_splits(entry_id) == []


def test_half_filled_rows_are_dropped_not_saved(admin_client):
    c, hdr = admin_client
    entry_id = _shared_bill()

    _edit(c, hdr, entry_id,
          split_entity=["TRUST1", "", "PP"], split_amount=["212.54", "50.00", ""])

    assert [s["entity_code"] for s in database.get_payment_splits(entry_id)] == ["TRUST1"]


def test_split_shows_on_an_unpaid_record(admin_client):
    """The bug behind the report: the split used to render only inside the Payment
    Record block, so an unpaid shared bill displayed nothing."""
    c, hdr = admin_client
    entry_id = _shared_bill(status="Pending")
    database.save_payment_splits(entry_id, [("TRUST1", 212.54), ("PP", 212.53)])

    html = c.get(f"/entry/{entry_id}/view").get_data(as_text=True)
    assert "Split across entities" in html
    assert "212.54" in html


def test_view_flags_a_split_that_misses_the_bill_total(admin_client):
    c, hdr = admin_client
    entry_id = _shared_bill(amount=425.07)
    database.save_payment_splits(entry_id, [("TRUST1", 100.00), ("PP", 100.00)])

    html = c.get(f"/entry/{entry_id}/view").get_data(as_text=True)
    assert "match the bill amount" in html


def test_by_units_suggestion_offered_when_properties_span_entities(admin_client):
    """T27 belongs to TRUST1 and PP16 to PP, so this bill has a units suggestion.
    This is exactly the shape of Nancy's #719 (KLR-OF + LRC-OF)."""
    c, hdr = admin_client
    property_model.seed_property_model()
    entry_id = _shared_bill()

    html = c.get(f"/entry/{entry_id}/edit").get_data(as_text=True)
    assert 'id="alloc-units"' in html


def test_no_units_suggestion_for_a_single_entity_bill(admin_client):
    c, hdr = admin_client
    property_model.seed_property_model()
    entry_id = _shared_bill(property_code="T27")

    html = c.get(f"/entry/{entry_id}/edit").get_data(as_text=True)
    assert 'id="alloc-units"' not in html
    assert 'id="alloc-block"' in html      # manual rows still available


def test_paying_a_bill_does_not_wipe_a_split_set_on_the_edit_form(admin_client):
    """The pay panel only renders split inputs when the bill's properties span more
    than one entity. A split entered by hand on the edit form has to survive being
    marked paid through a pay form that carried no split rows."""
    c, hdr = admin_client
    entry_id = _shared_bill(property_code="T27")      # single entity -> no pay-panel rows
    _edit(c, hdr, entry_id, property_codes=["T27"],
          split_entity=["TRUST1", "PP"], split_amount=["212.54", "212.53"])
    assert len(database.get_payment_splits(entry_id)) == 2

    c.post(f"/entry/{entry_id}/pay", data={
        "paid_amount": "425.07", "payment_method": "Check", "check_number": "13386",
        "paid_date": "2026-08-02",
    }, headers=hdr)

    assert len(database.get_payment_splits(entry_id)) == 2


def test_pay_panel_split_still_saves(admin_client):
    """The original pay-time path keeps working -- it just no longer clears."""
    c, hdr = admin_client
    entry_id = _shared_bill()

    c.post(f"/entry/{entry_id}/pay", data={
        "paid_amount": "425.07", "payment_method": "ACH", "paid_date": "2026-08-02",
        "split_entity": ["TRUST1", "PP"], "split_amount": ["212.54", "212.53"],
    }, headers=hdr)

    assert [(s["entity_code"], s["amount"]) for s in database.get_payment_splits(entry_id)] == \
           [("TRUST1", 212.54), ("PP", 212.53)]


def test_new_entry_can_be_created_with_a_split(admin_client):
    c, hdr = admin_client

    c.post("/entry/new", data={
        "date_received": "2026-08-01", "doc_type": "Bill", "status": "Pending",
        "vendor": "805 IT Guys", "amount": "425.07",
        "property_codes": ["T27", "PP16"], "date_ok": "1", "split_block": "1",
        "split_entity": ["TRUST1", "PP"], "split_amount": ["212.54", "212.53"],
    }, headers=hdr)

    entry = database.get_all_entries()[0]
    assert len(database.get_payment_splits(entry["id"])) == 2
