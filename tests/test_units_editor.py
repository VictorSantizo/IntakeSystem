"""The Properties & Units editor: units are edited as a list, and the count follows.

Before design question #7 this page edited one number per property. Now that units are
real rows the number is derived from them -- unit_count weights how a shared bill splits
across entities, so a hand-typed number that disagreed with the unit list would quietly
skew real allocations.

Also pins reconcile_unit_counts, which corrects the counts PROXY_UNIT_COUNTS got wrong
(it counted account numbers as units) WITHOUT overwriting a number Karen has curated.
"""
import database
import property_model


def test_reconcile_corrects_the_junk_inflated_seed(env):
    """T27 seeded as 34 from PROXY_UNIT_COUNTS, but only 31 of those tokens are units."""
    property_model.seed_property_model()

    props = {p["code"]: p for p in property_model.get_properties()}
    actual = len(property_model.get_units_for_property("T27"))
    assert props["T27"]["unit_count"] == actual
    assert actual != property_model.PROXY_UNIT_COUNTS["T27"]   # the seed was wrong


def test_reconcile_never_overwrites_a_curated_count(env):
    property_model.seed_property_model()
    property_model.update_property_units("T27", 99)   # Karen's own number

    property_model.reconcile_unit_counts()

    props = {p["code"]: p for p in property_model.get_properties()}
    assert props["T27"]["unit_count"] == 99


def test_adding_a_unit_raises_the_derived_count(env):
    property_model.seed_property_model()
    before = len(property_model.get_units_for_property("T27"))

    assert property_model.add_unit("T27", "31") is True

    labels = [u["label"] for u in property_model.get_units_for_property("T27")]
    assert "31" in labels
    props = {p["code"]: p for p in property_model.get_properties()}
    assert props["T27"]["unit_count"] == before + 1


def test_added_units_are_marked_as_staff_not_archive(env):
    property_model.seed_property_model()
    property_model.add_unit("T27", "31")

    u = {x["label"]: x for x in property_model.get_units_for_property("T27")}["31"]
    assert u["source"] == "staff"
    assert u["archive_hits"] == 0


def test_duplicate_unit_is_refused(env):
    property_model.seed_property_model()
    assert property_model.add_unit("T27", "5") is False       # T27-5 already seeded


def test_blank_unit_is_refused(env):
    property_model.seed_property_model()
    assert property_model.add_unit("T27", "   ") is False


def test_deleting_a_unit_lowers_the_derived_count(env):
    property_model.seed_property_model()
    units = {u["label"]: u for u in property_model.get_units_for_property("T27")}
    before = len(units)

    assert property_model.delete_unit(units["43"]["id"]) is True   # the one-sighting stray

    props = {p["code"]: p for p in property_model.get_properties()}
    assert props["T27"]["unit_count"] == before - 1
    assert "43" not in [u["label"] for u in property_model.get_units_for_property("T27")]


def test_renaming_a_unit_keeps_unit_code_in_step(env):
    property_model.seed_property_model()
    units = {u["label"]: u for u in property_model.get_units_for_property("T27")}

    assert property_model.rename_unit(units["43"]["id"], "31") is True

    renamed = {u["label"]: u for u in property_model.get_units_for_property("T27")}["31"]
    assert renamed["unit_code"] == "T27-31"


def test_unit_still_on_a_record_is_not_deletable(env):
    """Same reference-integrity rule properties already have: deleting a unit out from
    under a filed bill would orphan it."""
    property_model.seed_property_model()
    units = {u["label"]: u for u in property_model.get_units_for_property("T27")}
    entry_id = database.insert_entry({
        "date_received": "2026-08-01", "property_code": "T27",
        "vendor": "A-1 Appliance Repair", "doc_type": "Bill",
    })
    database.set_entry_units(entry_id, [units["5"]["id"]])

    assert property_model.unit_reference_count(units["5"]["id"]) == 1
    assert property_model.unit_reference_count(units["43"]["id"]) == 0


def test_editor_page_lists_units(admin_client):
    c, _ = admin_client
    property_model.seed_property_model()
    html = c.get("/settings/property-units").get_data(as_text=True)
    assert "T27" in html
    assert "from list" in html          # the derived-count label


def test_add_and_delete_unit_endpoints(admin_client):
    c, hdr = admin_client
    property_model.seed_property_model()

    r = c.post("/api/property/T27/unit", json={"label": "31"}, headers=hdr)
    assert r.status_code == 200 and r.get_json()["ok"] is True

    dup = c.post("/api/property/T27/unit", json={"label": "31"}, headers=hdr)
    assert dup.status_code == 400 and dup.get_json()["ok"] is False

    uid = {u["label"]: u for u in property_model.get_units_for_property("T27")}["31"]["id"]
    d = c.post(f"/api/unit/{uid}/delete", json={}, headers=hdr)
    assert d.status_code == 200 and d.get_json()["ok"] is True


def test_delete_endpoint_refuses_a_referenced_unit(admin_client):
    c, hdr = admin_client
    property_model.seed_property_model()
    units = {u["label"]: u for u in property_model.get_units_for_property("T27")}
    entry_id = database.insert_entry({
        "date_received": "2026-08-01", "property_code": "T27",
        "vendor": "A-1 Appliance Repair", "doc_type": "Bill",
    })
    database.set_entry_units(entry_id, [units["5"]["id"]])

    r = c.post(f"/api/unit/{units['5']['id']}/delete", json={}, headers=hdr)
    assert r.status_code == 409
    assert property_model.get_units_for_property("T27")  # still there
