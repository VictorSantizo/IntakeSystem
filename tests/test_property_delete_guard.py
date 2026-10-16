"""Reference-integrity guard for property deletion (2026-07-25 code review, P1-10):
delete_property had no check for records still pointing at the property, unlike its
sibling delete_entity. property_model.sync_property_deactivate now refuses (raises)
when property_reference_count(code) is nonzero, mirroring delete_entity's refuse-if-
referenced behavior as closely as this layer allows -- see property_model.py for the
route-level limitation this can't fully close without an app.py change.

The route-level half of the fix now lives in app.py's delete_property: it checks
property_reference_count(code) itself, BEFORE touching config.json, and refuses
with a flash error exactly like delete_entity does -- so config.json is never even
saved for a still-referenced property (sync_property_deactivate's own guard is a
defense in depth, not the only line of defense).
"""
import pytest

import app as klr
import database
import property_model


def test_delete_unreferenced_property_succeeds(env):
    property_model.sync_entity_upsert("TRUST9", "Nine")
    property_model.sync_property_upsert("T99", "9 Test St", "TRUST9")
    assert property_model.property_reference_count("T99") == 0

    property_model.sync_property_deactivate("T99")  # must not raise

    props = {p["code"]: p for p in property_model.get_properties()}
    assert props["T99"]["status"] == "inactive"


def test_delete_referenced_property_is_refused(env):
    property_model.sync_entity_upsert("TRUST9", "Nine")
    property_model.sync_property_upsert("T99", "9 Test St", "TRUST9")
    entry_id = database.insert_entry({
        "date_received": "2026-07-01",
        "property_code": "T99",
        "vendor": "ACME Utilities",
        "doc_type": "Bill",
    })
    assert property_model.property_reference_count("T99") == 1

    with pytest.raises(ValueError):
        property_model.sync_property_deactivate("T99")

    # Refused: both the relational row and the referencing record are untouched.
    props = {p["code"]: p for p in property_model.get_properties()}
    assert props["T99"]["status"] == "active"
    entry = database.get_entry(entry_id)
    assert entry["property_code"] == "T99"


def test_whole_token_matching_does_not_treat_prefix_as_a_match(env):
    """property_code is stored as a comma-separated string (e.g. "T27, PP16"), so a
    code that is a textual prefix of another (T2 inside T27) must never be counted
    as a reference to the shorter code."""
    property_model.sync_entity_upsert("TRUST9", "Nine")
    property_model.sync_property_upsert("T2", "2 Test St", "TRUST9")
    property_model.sync_property_upsert("T27", "27 Test St", "TRUST9")
    database.insert_entry({
        "date_received": "2026-07-01",
        "property_code": "T27, PP16",
        "vendor": "ACME Utilities",
        "doc_type": "Bill",
    })

    assert property_model.property_reference_count("T27") == 1
    assert property_model.property_reference_count("T2") == 0

    property_model.sync_property_deactivate("T2")  # unreferenced -- must succeed
    with pytest.raises(ValueError):
        property_model.sync_property_deactivate("T27")  # still referenced -- refused

    props = {p["code"]: p for p in property_model.get_properties()}
    assert props["T2"]["status"] == "inactive"
    assert props["T27"]["status"] == "active"


def test_delete_property_route_refuses_when_referenced(admin_client, env):
    """Route-level half of P1-10: /settings/properties/<code>/delete must refuse
    (and leave config.json untouched) when a record still references the property,
    the same way /settings/entities/<code>/delete already refuses for entities."""
    c, headers = admin_client
    database.insert_entry({
        "date_received": "2026-07-01",
        "property_code": "T27",
        "vendor": "ACME Utilities",
        "doc_type": "Bill",
    })

    r = c.post("/settings/properties/T27/delete", headers=headers, follow_redirects=True)
    assert r.status_code == 200
    assert b"still reference it" in r.data

    cfg = klr.load_config()
    assert any(p["code"] == "T27" for p in cfg["properties"])


def test_delete_property_route_succeeds_when_unreferenced(admin_client, env):
    c, headers = admin_client
    r = c.post("/settings/properties/PP16/delete", headers=headers, follow_redirects=True)
    assert r.status_code == 200
    assert b"Deleted property PP16" in r.data

    cfg = klr.load_config()
    assert not any(p["code"] == "PP16" for p in cfg["properties"])
