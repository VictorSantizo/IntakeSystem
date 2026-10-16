"""`migrate-unit-accounts`: move unit numbers out of Account # (report #20 cleanup).

Before units existed, typing "TL222-5" into Account # was the only way to record which
unit a bill was about. This moves those into the real unit link -- but Account # is
mostly full of genuine vendor account numbers ("20-5192-00", "1060-3793-12", "###-###-8100"),
which are hyphenated too. So the rule is deliberately narrow, and these tests exist mainly
to pin what it must NOT touch: on the live archive it matches exactly 1 record out of 441.
"""
import app as klr
import database
import property_model


def _bill(**kw):
    base = {"date_received": "2026-08-01", "doc_type": "Bill", "vendor": "Someone"}
    base.update(kw)
    return database.insert_entry(base)


def test_moves_a_unit_number_out_of_account_number(env):
    property_model.seed_property_model()
    entry_id = _bill(property_code="T27", account_number="T27-5",
                     vendor="A-1 Appliance Repair")

    klr._cli_reports(["migrate-unit-accounts", "apply"])

    assert database.get_entry(entry_id)["account_number"] is None
    assert [u["unit_code"] for u in database.get_entry_units(entry_id)] == ["T27-5"]


def test_dry_run_changes_nothing(env):
    property_model.seed_property_model()
    entry_id = _bill(property_code="T27", account_number="T27-5")

    klr._cli_reports(["migrate-unit-accounts"])          # no "apply"

    assert database.get_entry(entry_id)["account_number"] == "T27-5"
    assert database.get_entry_units(entry_id) == []


def test_real_account_numbers_are_left_alone(env):
    """The reason the rule is narrow rather than 'anything with a hyphen'."""
    property_model.seed_property_model()
    keep = ["20-5192-00", "1060-3793-12", "###-###-8100", "037-061-002",
            "26-170", "LAWRRAM-01", "5836047277-1"]
    ids = [_bill(property_code="T27", account_number=a) for a in keep]

    klr._cli_reports(["migrate-unit-accounts", "apply"])

    for entry_id, original in zip(ids, keep):
        assert database.get_entry(entry_id)["account_number"] == original
        assert database.get_entry_units(entry_id) == []


def test_unit_of_a_property_the_record_does_not_name_is_left_alone(env):
    """"PP16-58" on a record filed under T27 is not this record's unit -- it's more
    likely a genuine account number that happens to look like one."""
    property_model.seed_property_model()
    entry_id = _bill(property_code="T27", account_number="PP16-58")

    klr._cli_reports(["migrate-unit-accounts", "apply"])

    assert database.get_entry(entry_id)["account_number"] == "PP16-58"


def test_unknown_unit_is_left_alone(env):
    """Right shape, but T27 has no unit 999 -- don't invent one."""
    property_model.seed_property_model()
    entry_id = _bill(property_code="T27", account_number="T27-999")

    klr._cli_reports(["migrate-unit-accounts", "apply"])

    assert database.get_entry(entry_id)["account_number"] == "T27-999"


def test_migration_is_idempotent(env):
    property_model.seed_property_model()
    entry_id = _bill(property_code="T27", account_number="T27-5")

    klr._cli_reports(["migrate-unit-accounts", "apply"])
    klr._cli_reports(["migrate-unit-accounts", "apply"])

    assert [u["unit_code"] for u in database.get_entry_units(entry_id)] == ["T27-5"]


def test_migration_is_logged_against_the_record(env):
    property_model.seed_property_model()
    entry_id = _bill(property_code="T27", account_number="T27-5")

    klr._cli_reports(["migrate-unit-accounts", "apply"])

    details = [a["detail"] for a in database.get_activity_feed(50)
               if a["entry_id"] == entry_id]
    assert any("moved out of Account #" in (d or "") for d in details)
