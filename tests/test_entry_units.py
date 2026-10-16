"""Report #20: a bill can name the unit it's about.

Nancy's words: "Edit Entry doesn't provide a line for the apartment # for TL222, so I
added it to Account # TL222-5." Account # means the vendor's ongoing account number, so
that made the bill look like it had an account it didn't -- and made the unit invisible
to anything that queries by unit. These pin the real field: it saves, it round-trips,
and it can't keep a unit whose property the record no longer names.
"""
import database
import property_model


def _seed(cfg_props=True):
    property_model.seed_property_model()
    return {u["label"]: u for u in property_model.get_units_for_property("T27")}


def test_new_entry_saves_its_units(admin_client):
    c, hdr = admin_client
    units = _seed()

    c.post("/entry/new", data={
        "date_received": "2026-08-01", "doc_type": "Bill", "status": "Pending",
        "vendor": "A-1 Appliance Repair", "property_codes": ["T27"],
        "unit_ids": [str(units["5"]["id"])], "date_ok": "1",
    }, headers=hdr)

    entry = database.get_all_entries()[0]
    assert [u["unit_code"] for u in database.get_entry_units(entry["id"])] == ["T27-5"]


def test_edit_entry_saves_and_replaces_units(admin_client):
    c, hdr = admin_client
    units = _seed()
    entry_id = database.insert_entry({
        "date_received": "2026-08-01", "property_code": "T27",
        "vendor": "A-1 Appliance Repair", "doc_type": "Bill",
    })

    def edit(unit_ids):
        c.post(f"/entry/{entry_id}/edit", data={
            "date_received": "2026-08-01", "doc_type": "Bill", "status": "Pending",
            "vendor": "A-1 Appliance Repair", "property_codes": ["T27"],
            "unit_ids": unit_ids, "date_ok": "1",
        }, headers=hdr)

    edit([str(units["5"]["id"]), str(units["7"]["id"])])
    assert {u["label"] for u in database.get_entry_units(entry_id)} == {"5", "7"}

    # A re-save replaces wholesale rather than accumulating.
    edit([str(units["7"]["id"])])
    assert {u["label"] for u in database.get_entry_units(entry_id)} == {"7"}

    edit([])
    assert database.get_entry_units(entry_id) == []


def test_one_bill_can_cover_several_units(admin_client):
    """The roofer-bills-3-7-and-12 case that made this a link table, not a column."""
    c, hdr = admin_client
    units = _seed()

    c.post("/entry/new", data={
        "date_received": "2026-08-01", "doc_type": "Bill", "status": "Pending",
        "vendor": "Roofer", "property_codes": ["T27"],
        "unit_ids": [str(units[n]["id"]) for n in ("3", "7", "12")], "date_ok": "1",
    }, headers=hdr)

    entry = database.get_all_entries()[0]
    assert {u["label"] for u in database.get_entry_units(entry["id"])} == {"3", "7", "12"}


def test_units_of_an_unticked_property_are_dropped(admin_client):
    """The form renders every property's units and hides the irrelevant ones, so a
    stale tick can be submitted for a property the record no longer names."""
    c, hdr = admin_client
    units = _seed()
    pp16 = property_model.get_units_for_property("PP16")[0]

    c.post("/entry/new", data={
        "date_received": "2026-08-01", "doc_type": "Bill", "status": "Pending",
        "vendor": "A-1 Appliance Repair",
        "property_codes": ["T27"],                     # PP16 NOT selected
        "unit_ids": [str(units["5"]["id"]), str(pp16["id"])], "date_ok": "1",
    }, headers=hdr)

    entry = database.get_all_entries()[0]
    assert [u["unit_code"] for u in database.get_entry_units(entry["id"])] == ["T27-5"]


def test_edit_form_prechecks_the_saved_units(admin_client):
    c, hdr = admin_client
    units = _seed()
    entry_id = database.insert_entry({
        "date_received": "2026-08-01", "property_code": "T27",
        "vendor": "A-1 Appliance Repair", "doc_type": "Bill",
    })
    database.set_entry_units(entry_id, [units["5"]["id"]])

    html = c.get(f"/entry/{entry_id}/edit").get_data(as_text=True)
    assert f'value="{units["5"]["id"]}"' in html
    assert "which unit is this about" in html


def test_unit_choices_show_the_full_property_unit_code(admin_client):
    """Report #78.1 (Joshua): "The unit codes should be PROP-CODE + Unit # otherwise,
    I'm not sure what to pick here." A bill can name several properties at once, so a
    bare "5" is ambiguous between T27-5 and PP16-5."""
    c, _ = admin_client
    _seed()

    html = c.get("/entry/new").get_data(as_text=True)
    assert "<code style=\"font-size:.8rem;\">T27-5</code>" in html
    assert "<code style=\"font-size:.8rem;\">PP16-58</code>" in html


def test_record_view_shows_the_unit(admin_client):
    c, hdr = admin_client
    units = _seed()
    entry_id = database.insert_entry({
        "date_received": "2026-08-01", "property_code": "T27",
        "vendor": "A-1 Appliance Repair", "doc_type": "Bill",
    })
    database.set_entry_units(entry_id, [units["5"]["id"]])

    html = c.get(f"/entry/{entry_id}/view").get_data(as_text=True)
    assert "T27-5" in html


def test_a_record_with_no_units_is_unaffected(admin_client):
    """Most records have no unit; the field must simply not appear."""
    c, hdr = admin_client
    _seed()
    entry_id = database.insert_entry({
        "date_received": "2026-08-01", "property_code": "T27",
        "vendor": "Montecito Water", "doc_type": "Bill",
    })

    assert database.get_entry_units(entry_id) == []
    assert c.get(f"/entry/{entry_id}/view").status_code == 200


def test_entry_units_map_batches_the_same_answer(env):
    _seed()
    units = {u["label"]: u for u in property_model.get_units_for_property("T27")}
    a = database.insert_entry({"date_received": "2026-08-01", "property_code": "T27",
                               "vendor": "V1", "doc_type": "Bill"})
    b = database.insert_entry({"date_received": "2026-08-01", "property_code": "T27",
                               "vendor": "V2", "doc_type": "Bill"})
    database.set_entry_units(a, [units["5"]["id"]])
    database.set_entry_units(b, [units["7"]["id"], units["3"]["id"]])

    m = database.get_entry_units_map([a, b])
    assert [u["label"] for u in m[a]] == ["5"]
    assert [u["label"] for u in m[b]] == ["3", "7"]     # mailbox order
    assert database.get_entry_units_map([]) == {}
